"""Booth Mic, the voice chain: pure builders (settings → Booth Mic's chain; the Scarlett's mic record → its source
selector) and their two services, each `pipewire -c <config>` (installed by install.sh), so Booth Mic lives on with
Booth closed:
  booth-voice.service       booth.mic.input (hidden internal sink) → filter-chain stages → Booth Mic (booth_mic, a mono
                            Audio/Source). Never restarted by a mic change: Booth Mic and a call's link to it stay.
  booth-mic-select.service  Scarlett capture_FR → booth.mic.select (loopback) → booth.mic.input. The only thing a new mic
                            channel or Scarlett node restarts. BindsTo booth-voice.service.
The selector takes only the mic's channel, by position, never a mixdown; it is passive, so the Scarlett is opened only
while an app reads Booth Mic. It is pinned to the Scarlett (target.object + node.dont-reconnect): with the Scarlett gone
WirePlumber destroys it instead of moving it to another mic, Booth Mic carries silence and the page says it is waiting;
heal() restarts it once the Scarlett is back. With the Scarlett there but the mic on no capture channel (rerouted to the
computer's playback or a mix) the selector is stopped and its config removed: Booth Mic never carries what the mic's old
channel carries now. heal() reads the Scarlett's routing and node every 10 s, so it follows a reroute, a renamed node
(a fast replug, a profile switch) and a login race with only the background Booth running. #20's fallback changes only
the selector's target and positions.
Every stage and the master switch is a dry/wet `mixer`, set live with `pw-cli set-param` on booth.mic.input: a switch
or a knob changes only control values, so it never restarts anything; a new shape restarts both.
The plugin stages (STAGES) are in the graph exactly when their plugin verified (booth.plugins) as the chain was built
for its start: booth-voice.service's ExecStartPre runs `--write`, so a plugin changed since is never loaded. A change in
what verifies alone (a changed file, a read that fails for a moment, an install's new plugins) never restarts the
running chain, which mapped verified code: a stage whose plugin stopped verifying goes dry live, one that verifies only
since waits for the next start, and the page says both are missing (loaded()). A new shape (new code) restarts both
services, never starting one the user stopped.
    python3 -m booth.voice            write the configs from the saved settings and the Scarlett now, start the services
    python3 -m booth.voice --write [voice.conf [plugins folder]]
                                      only voice.conf, from the saved settings and the plugins verified now (ExecStartPre)
    python3 -m booth.voice --reload   restart Booth Mic, if it runs, when its plugins aren't the ones verified now
                                      (install.sh, once no old Booth runs)"""
import copy
import json
import sys
import threading
import time
from pathlib import Path

from booth import devices, plugins, scarlett, service, store

UNIT, SELECT_UNIT = "booth-voice.service", "booth-mic-select.service"
# a new chain: the selector re-links to its new input; try-: a unit the user stopped stays stopped
RESTART = ["systemctl", "--user", "try-restart", UNIT, SELECT_UNIT]
ACTIVE = ["systemctl", "--user", "is-active", UNIT]  # the selector's restart starts it too (BindsTo): only if it runs
START = ["systemctl", "--user", "start", UNIT, SELECT_UNIT]
RESTART_SELECT = ["systemctl", "--user", "restart", SELECT_UNIT]
STOP_SELECT = ["systemctl", "--user", "stop", SELECT_UNIT]
NODE = "booth_mic"
INPUT = "booth.mic.input"
SELECTOR = "booth.mic.select"
SELECT_CONF = "voice-select.conf"
CHANNEL = "booth.mic.channel"  # the selector's capture channel, from the ALSA routing: checked without pw-dump
RUMBLE_HZ = 80.0
WAITING = "Booth Mic is waiting for the Scarlett"
NOT_ROUTED = "Booth Mic has no input: the mic isn't routed to a capture channel"
STALE = "Booth Mic may still be using the old channel: couldn't stop it"
_LOCK = threading.RLock()  # the page's poll, the service's start and heal may run at once
HEAL_BACKOFF_S = 60
_HEALED = {}  # (selector config, Scarlett node object.serial) → when heal last restarted the selector for it


def path():
    return store.path().parent / "voice.conf"


def _dump():
    return devices._run("pw-dump")


def _mix(name, wet):
    """A dry/wet bypass: In 1 the processed signal, In 2 the dry one."""
    return {"type": "builtin", "name": name, "label": "mixer",
            "control": {"Gain 1": 1.0 if wet else 0.0, "Gain 2": 0.0 if wet else 1.0}}


def _config(module, args):
    return json.dumps({"context.properties": {"log.level": 0},
                       "context.spa-libs": {"audio.convert.*": "audioconvert/libspa-audioconvert",
                                            "support.*": "support/libspa-support"},
                       "context.modules": [{"name": "libpipewire-module-rt", "args": {}, "flags": ["ifexists", "nofail"]},
                                           {"name": "libpipewire-module-protocol-native"},
                                           {"name": "libpipewire-module-client-node"},
                                           {"name": "libpipewire-module-adapter"},
                                           {"name": module, "args": args}]}, indent=2)


def strength(knob):
    """Noise suppression's Strength, 0..100 → RNNoise's controls. RNNoise always removes noise; Strength is how sure it
    must be that a 10 ms frame is voice before letting it through (VAD Threshold 0..95 %, linear, a whole percent: the
    port is an integer): higher mutes the room between words harder, but too high clips soft word ends, so it stops at
    werman's 95 % ceiling. The 200 ms grace keeps word tails; no retroactive grace, which would add that much latency."""
    return {"VAD Threshold (%)": float(round(knob * 0.95)), "VAD Grace Period (ms)": 200.0,
            "Retroactive VAD Grace (ms)": 0.0}


def amount(knob):
    """Leveling's Amount, 0..100 → sc4m's controls. 0 is no compression (threshold 0 dB, 1:1); 100 is threshold −30 dB
    at 6:1, both linear in between. Makeup gain gives back half of what a peak at 0 dB loses, −threshold × (1 − 1/ratio)
    / 2, so turning it up evens the voice out without making it quieter. RMS sensing, 10 ms attack, 150 ms release and a
    5 dB knee are fixed: smooth for speech."""
    f = knob / 100
    threshold, ratio = 0.0 - 30.0 * f, 1.0 + 5.0 * f
    return {"RMS/peak": 0.0, "Attack time (ms)": 10.0, "Release time (ms)": 150.0, "Knee radius (dB)": 5.0,
            "Threshold level (dB)": threshold, "Ratio (1:n)": ratio,
            "Makeup gain (dB)": round(-threshold * (1 - 1 / ratio) / 2, 2)}


def ceiling(db):
    """The limiter's Ceiling in dB (−20..0, default −1) → the limiter's Limit, as it is; no input gain, 50 ms release."""
    return {"Input gain (dB)": 0.0, "Limit (dB)": float(db), "Release time (s)": 0.05}


# The plugin stages after the rumble filter, in chain order: (settings key = node name, plugin file, LADSPA label,
# audio inputs, audio output, the knob's settings key, its default, its mapping). The names are #14's fixture's exactly.
# The limiter is stereo: the mono chain feeds both its inputs and takes Output 1.
# #17's tone stage (builtin bq_* filters, no plugin) goes between "noise" and "leveling".
STAGES = (("noise", "librnnoise_ladspa.so", "noise_suppressor_mono", ("Input",), "Output",
           "noise_strength", 50, strength),
          ("leveling", "sc4m_1916.so", "sc4m", ("Input",), "Output", "leveling_amount", 50, amount),
          ("limiter", "fast_lookahead_limiter_1913.so", "fastLookaheadLimiter", ("Input 1", "Input 2"), "Output 1",
           "limiter_ceiling", -1.0, ceiling))


def verified(where=None, manifest=plugins.MANIFEST):
    """{plugin file: its path} for each plugin that verifies now (plugins.verify): the only paths a config may hold."""
    found = {}
    for name in manifest:
        try:
            found[name] = str(plugins.verify(name, where, manifest))
        except (plugins.Refused, OSError):
            pass
    return found


def _plugins(cfg):
    """{plugin file: path} of the plugins a chain config (any JSON on disk) loads; None when it isn't a config."""
    try:
        return {n["plugin"].rsplit("/", 1)[-1]: n["plugin"] for n in _graph_nodes(cfg) if n.get("type") == "ladspa"}
    except (AttributeError, KeyError, TypeError):
        return None


def loaded(file=None):
    """verified() narrowed to the plugins in voice.conf, the running chain: the stages that process the voice now."""
    kept = _plugins(_load(file or path())) or {}
    return {k: v for k, v in verified().items() if kept.get(k) == v}


def missing(found):
    """The stages whose plugin isn't in `found` (verified): left out of the chain, so the mic passes them dry."""
    return [s[0] for s in STAGES if s[1] not in found]


def build(settings, found=None):
    """The settings → Booth Mic's chain config text (booth-voice.service), whatever the Scarlett does. Plugin stages
    only from `found` (verified() now when not given): every stage that verifies is in the graph, on or off."""
    v = settings.get("voice") or {}
    found = verified() if found is None else found
    nodes = [{"type": "builtin", "name": "in", "label": "copy"},
             {"type": "builtin", "name": "rumble", "label": "bq_highpass", "control": {"Freq": RUMBLE_HZ, "Q": 0.707}},
             _mix("rumble_mix", v.get("rumble", True))]
    links = [{"output": "in:Out", "input": "rumble:In"},
             {"output": "rumble:Out", "input": "rumble_mix:In 1"},
             {"output": "in:Out", "input": "rumble_mix:In 2"}]
    last = "rumble_mix:Out"
    for key, file, label, ins, out, knob, default, mapping in STAGES:
        if file not in found:
            continue
        nodes += [{"type": "ladspa", "name": key, "plugin": found[file], "label": label,
                   "control": mapping(v.get(knob, default))},
                  _mix(f"{key}_mix", v.get(key, True))]
        links += [{"output": last, "input": f"{key}:{port}"} for port in ins]
        links += [{"output": f"{key}:{out}", "input": f"{key}_mix:In 1"}, {"output": last, "input": f"{key}_mix:In 2"}]
        last = f"{key}_mix:Out"
    nodes.append(_mix("master", v.get("enabled", True)))
    links += [{"output": last, "input": "master:In 1"}, {"output": "in:Out", "input": "master:In 2"}]
    chain = {"node.description": "Booth Mic", "media.name": "Booth Mic",
             "filter.graph": {"nodes": nodes, "links": links, "inputs": ["in:In"], "outputs": ["master:Out"]},
             "audio.channels": 1, "audio.position": ["MONO"],
             "capture.props": {"node.name": INPUT, "node.description": "Booth Mic input",
                               "media.class": "Audio/Sink/Internal", "node.passive": True},
             "playback.props": {"node.name": NODE, "node.description": "Booth Mic", "media.class": "Audio/Source"}}
    return _config("libpipewire-module-filter-chain", chain)


def build_selector(mic):
    """The mic record (scarlett.mic) → the selector's config text (booth-mic-select.service), or None when there is no
    channel to take. dont-reconnect on both ends: never moved to another mic, never played to a speaker."""
    if not mic or not mic.get("node") or not mic.get("position"):
        return None
    selector = {"node.description": "Booth Mic source", "audio.channels": 1,
                # Internal: no pipewire-pulse source-output, so no mic-in-use indicator while no app reads Booth Mic
                "capture.props": {"node.name": SELECTOR, "media.class": "Stream/Input/Audio/Internal",
                                  "target.object": mic["node"], "node.dont-reconnect": True,
                                  CHANNEL: mic.get("channel"),
                                  "audio.channels": 1, "audio.position": [mic["position"]],
                                  "stream.dont-remix": True, "node.passive": True},
                "playback.props": {"node.name": f"{SELECTOR}.out", "media.class": "Stream/Output/Audio/Internal",
                                   "target.object": INPUT, "node.dont-reconnect": True, "audio.channels": 1,
                                   "audio.position": ["MONO"], "node.passive": True}}
    return _config("libpipewire-module-loopback", selector)


def _channel(cfg):
    """The capture channel a selector config (any JSON on disk) was built for, or None."""
    try:
        return next(m["args"]["capture.props"][CHANNEL] for m in cfg["context.modules"]
                    if m["name"] == "libpipewire-module-loopback")
    except (AttributeError, KeyError, StopIteration, TypeError):
        return None


def _graph_nodes(cfg):
    return [n for m in cfg["context.modules"] for n in m.get("args", {}).get("filter.graph", {}).get("nodes", [])]


def shape(cfg):
    """The config without its control values: what a change needs a restart for; None when it isn't a config."""
    try:
        cfg = copy.deepcopy(cfg)
        for n in _graph_nodes(cfg):
            n.pop("control", None)
    except (AttributeError, KeyError, TypeError):  # any JSON on disk: replaced, never trusted
        return None
    return cfg


def _node(dump, name):
    """The PipeWire node named `name` in `pw-dump` JSON, or None."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None
    return next((o for o in objects if isinstance(objects, list) and isinstance(o, dict)
                 and ((o.get("info") or {}).get("props") or {}).get("node.name") == name), None)


def _node_id(dump, name):
    """The id of the PipeWire node named `name` in `pw-dump` JSON, or None."""
    return (_node(dump, name) or {}).get("id")


def selecting(dump=None):
    """True when Booth Mic's selector runs: its node is in `pw-dump` JSON (read now when not given)."""
    return _node_id(_dump() if dump is None else dump, SELECTOR) is not None


def set_params_cmd(dump, cfg):
    """The live change to every control in `cfg`, on the running chain's input node (by name, now), or None."""
    node = _node_id(dump, INPUT)
    if node is None:
        return None
    params = [x for n in _graph_nodes(cfg) for k, v in n.get("control", {}).items() for x in (f"{n['name']}:{k}", v)]
    return ["pw-cli", "set-param", str(node), "Props", json.dumps({"params": params})]


def _text(file):
    try:
        return file.read_text()
    except (OSError, ValueError):
        return None


def _load(file):
    try:
        return json.loads(file.read_text())
    except (OSError, ValueError):
        return None


def _save(cfg, file):
    """Write `cfg` → None, or what went wrong (disk full, read-only): the running process keeps the config it has."""
    try:
        store.save(cfg, file)
    except OSError as e:
        return f"not saved: {e.strerror or e}"
    return None


def _put_back(file, text):
    """`file` as it was (None: absent), so the next apply sees its change again and retries it."""
    try:
        if text is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(text)
    except OSError:
        pass


def _key(sel, dump, name):
    """heal's backoff key: (selector config text, the Scarlett node's object.serial, never reused as a node id is: tells
    a replug, restart now, from the same selector failing again), the serial None when the node isn't in `dump`."""
    scarlett_node = _node(dump, name)
    return sel, scarlett_node and scarlett_node["info"]["props"].get("object.serial", scarlett_node["id"])


def _restarted(key, clock):
    """Every selector restart, apply's or heal's: heal leaves it to come up for HEAL_BACKOFF_S."""
    _HEALED.clear()
    _HEALED[key] = clock()


def apply(settings, state, run=service.run, dump=_dump, file=None, clock=time.monotonic):
    """Bring both configs up to date with the settings and `state`, the Scarlett now (scarlett.read). New control values
    are set live on the chain, then recorded; a new chain shape (new code; never a change in what verifies alone, see
    the module) try-restarts both services; a new mic channel or node restarts only the selector, while Booth Mic runs. Unplugged (None) keeps the last selector: it unloaded itself and heal() brings
    it back. The Scarlett there with no channel to take (not routed, or none at the mic's position) stops the selector
    and removes its config: never a guessed channel, never one the mic has left; no Scarlett node (pw-dump failed) keeps
    the selector only while the ALSA routing has the mic on the channel it was built for. A new selector is always
    restarted. Returns what it did, with "not …" when it couldn't: then that change isn't recorded (a failed restart
    puts the old config back), so the next apply retries.
    A chain change that fails never holds back the selector."""
    file = file or path()
    select_file = file.with_name(SELECT_CONF)
    with _LOCK:  # built inside: the plugins verified just before the write, the latest settings applied last
        found = verified()
        chain, sel_text = json.loads(build(settings, found)), build_selector(state["mic"]) if state else None
        sel = sel_text and json.loads(sel_text)
        old, old_text, old_sel_text, said = _load(file), _text(file), _text(select_file), None
        kept = _plugins(old)
        if kept is not None and kept != found:  # what verifies changed: the running chain's plugins, dry if not now
            v = dict(settings.get("voice") or {}, **dict.fromkeys(missing(found), False))
            same = json.loads(build(dict(settings, voice=v), kept))
            if shape(same) == shape(old):  # nothing else changed: no restart; its next start loads what verifies
                chain = same
        restart = shape(old) != shape(chain)
        if old != chain and not restart and (cmd := set_params_cmd(dump(), chain)) and not run(cmd):
            said = "not applied: Booth Mic didn't take the change"
        # no Scarlett node (pw-dump failed, or it left PipeWire): kept only on the channel the ALSA routing still gives
        mic = state and state["mic"]
        kept = mic and not mic["node"] and mic["channel"] == _channel(_load(select_file))
        if state is not None and sel is None and old_sel_text is not None and not kept:
            if not run(STOP_SELECT):  # the config stays: the next apply stops it again
                return "not stopped: Booth Mic's source still takes the mic's old channel"
            _put_back(select_file, None)
        # the selector's own save first: a chain change that fails never holds back a new channel
        sel_said = sel is not None and sel != _load(select_file) and _save(sel, select_file)
        moved = sel_said is None
        if old != chain and not said:
            said = _save(chain, file)
        restart = restart and not said  # a chain that isn't written: nothing new to restart it with
        said = said or sel_said or None
        if not (restart or moved):
            return said or ("live" if old != chain else "unchanged")
        # RESTART_SELECT: Booth Mic, and a call's link to it, stay; never with Booth Mic stopped, which it would start
        off = not restart and not run(ACTIVE)
        if off or not run(RESTART if restart else RESTART_SELECT):
            if restart:
                _put_back(file, old_text)
            if moved:
                _put_back(select_file, old_sel_text)
            return "not restarted: Booth Mic is stopped" if off else "not restarted: Booth Mic's service didn't start"
        if sel is not None:  # RESTART restarts the selector too; shortcut: a pw-dump blip here keys it without the node
            _restarted(_key(sel_text, dump(), state["mic"]["node"]), clock)
        return ("restarted" if restart else "selector restarted") + (f", but {said}" if said else "")


def _choice(settings):
    return (settings.get("microphone") or {}).get("input", scarlett.DEFAULT_INPUT)


def heal(settings, read=scarlett.read, run=service.run, dump=_dump, file=None, clock=time.monotonic):
    """Every 10 s: apply() with the Scarlett read now, so a reroute, a new channel or a renamed Scarlett node is
    followed with or without a window. Then restart the selector when it should run, the Scarlett and Booth Mic's input
    are in PipeWire and it isn't: it unloads itself when the Scarlett goes, or when it started before it at login.
    Never without Booth Mic's input (BindsTo would pull a stopped booth-voice.service back up), and at most once a
    minute for the same config and Scarlett node, so a selector that won't stay up isn't restarted every tick. True when
    it restarted it."""
    state = read(_choice(settings))
    if state is None:
        return False  # unplugged: the selector keeps its config for the Scarlett's return
    with _LOCK:
        said = apply(settings, state, run, dump, file, clock)
        sel = build_selector(state["mic"])
        # only the selector's own config on disk: never revived on an old channel, whatever the chain's save did
        if sel is None or _load((file or path()).with_name(SELECT_CONF)) != json.loads(sel):
            return False
        if said.startswith(("restarted", "selector restarted")):
            return True  # by apply, which recorded it: it is coming up
        now = dump()
        key = _key(sel, now, state["mic"]["node"])
        if key[1] is None or _node_id(now, INPUT) is None or _node_id(now, SELECTOR) is not None:
            return False
        if key in _HEALED and clock() - _HEALED[key] < HEAL_BACKOFF_S:
            return False
        restarted = run(RESTART_SELECT)
        _restarted(key, clock)
        return restarted


def describe(state, selecting=True, said=""):
    """The Voice processing group's description for a Scarlett poll (scarlett.read), whether the selector runs and what
    apply said."""
    if said.startswith("not stopped"):
        return STALE
    mic = state["mic"] if state else None
    if state and mic is None:
        return NOT_ROUTED
    if not mic or not mic["node"] or not mic["position"] or not selecting:
        return WAITING
    return f"Booth Mic takes the Scarlett's capture channel {mic['channel']} ({mic['position']}), in mono"


def sync(settings, read=scarlett.read, run=service.run, dump=_dump, file=None):
    """The Scarlett now → apply → both services started (a no-op when they run). Returns describe."""
    state = read(_choice(settings))
    apply(settings, state, run, dump, file)
    if (file or path()).exists():
        run(START)
    return describe(state)


def write(file=None, where=None):
    """booth-voice.service's ExecStartPre: voice.conf (`file`: the one its pipewire loads) from the saved settings and
    the plugins verified now in `where`, so a plugin changed since the last write is never loaded. No systemctl, no
    Scarlett: the unit is starting. None, or what went wrong: then the unit doesn't start on the old config."""
    return _save(json.loads(build(store.load(), verified(where))), file or path())


def reload(run=service.run, file=None):
    """install.sh, once no old Booth runs (its poll would fight the new chain): Booth Mic restarted, only if it runs,
    when its chain's plugins aren't the ones verified now, so an install's plugins load now. False: that failed."""
    with _LOCK:
        return _plugins(_load(file or path())) == verified() or run(RESTART)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--write"]:
        said = write(*(Path(a) for a in args[1:3]))
        if said:
            print(f"booth.voice: {said}", file=sys.stderr)
        return 1 if said else 0
    if args == ["--reload"]:
        return 0 if reload() else 1
    print(sync(store.load()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
