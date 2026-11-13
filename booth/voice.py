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
(a fast replug, a profile switch) and a login race with only the background Booth running.
Fallback (#20): with the Scarlett gone from ALSA and PipeWire, heal() moves the selector to a backup mic, the PRO X mic
while the headset is on, else the BRIO's, each found by name and taken at its own first discovered position, and back
to the Scarlett when it returns (choose()). apply() stays the one writer of the selector; only the selector restarts, so
Booth Mic and its node mute stay. With neither backup the selector stops: Booth Mic is silent, never on a mic Booth no
longer picks. A backup feeds Booth Mic only while a Booth primary instance runs to watch it and say it: Booth's
shutdown stops it (release()), and booth-mic-select.service's ExecCondition (`--select`) never starts one without Booth:
it removes its config, so Booth Mic is silent until Booth starts, heals and says it.
Every stage and the master switch is a dry/wet `mixer`, set live with `pw-cli set-param` on booth.mic.input: a switch
or a knob changes only control values, so it never restarts anything; a new shape restarts both.
The plugin stages (STAGES) are in the graph exactly when their plugin verified (booth.plugins) as the chain was built
for its start: booth-voice.service's ExecStartPre runs `--write`, which writes voice-run.conf, the only file its pipewire
loads and one nothing else writes, so a plugin changed since is never loaded. voice.conf is what Booth wants (apply's
record of what it set live); voice-run.conf is what runs. A change in what verifies alone (a changed file, a read that
fails for a moment, an install's new plugins) never restarts the running chain, which mapped verified code: a stage
whose plugin stopped verifying goes dry live, one that verifies only since waits for the next start, and the page says
both are missing (loaded()). A new shape (new code) restarts both services, never starting one the user stopped.
    python3 -m booth.voice            write the configs from the saved settings and the Scarlett now, start the services
    python3 -m booth.voice --write [voice-run.conf [plugins folder]]
                                      only voice-run.conf, from the saved settings and the plugins verified now
                                      (ExecStartPre)
    python3 -m booth.voice --reload   restart Booth Mic, if it runs, when its plugins aren't the ones verified now
                                      (install.sh, once no old Booth runs)
    python3 -m booth.voice --select voice-select.conf
                                      booth-mic-select.service's ExecCondition: may_select (exit 0) or not (1)"""
import copy
import json
import os
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
RUN_CONF = "voice-run.conf"  # the chain booth-voice.service's pipewire loads: written only by its ExecStartPre
CHANNEL = "booth.mic.channel"  # the selector's capture channel, from the ALSA routing: checked without pw-dump
RUMBLE_HZ = 80.0
WAITING = "Booth Mic is waiting for the Scarlett"
NOT_ROUTED = "Booth Mic has no input: the mic isn't routed to a capture channel"
STALE = "Booth Mic may still be using the old channel: couldn't stop it"
STOPPED = "Booth Mic isn't running: see systemctl --user status booth-voice.service"
_LOCK = threading.RLock()  # the page's poll, the service's start and heal may run at once
HEAL_BACKOFF_S = 60
_HEALED = {}  # (selector config, Scarlett node object.serial) → when heal last restarted the selector for it
SCARLETT, PRO_X_MIC, BRIO_MIC = "Scarlett", "PRO X mic", "BRIO mic"  # what Booth Mic takes; None: no mic
NO_INPUT = "no input"  # the Scarlett's card there, its source not in PipeWire (profile Off, a failed node): silent
# the backups in order, by their PipeWire source's name: then the serial and profile
BACKUPS = ((PRO_X_MIC, "alsa_input." + devices.PRO_X_USB), (BRIO_MIC, "alsa_input.usb-046d_Logitech_BRIO_"))
CHOSEN = SCARLETT  # the mic heal() chose last, for the routing policy's notice
SEEN = None  # the pw-dump heal() read last for its revive check, for running(); None when it read none
_VERIFIED = {}  # plugin path → the stat of it and its folder when it last verified: unchanged, it isn't hashed again
CLOSED = False  # Booth is shutting down (release()): no heal moves the selector again


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


def _stat(path, pin):
    """What verify() checked that can change: the file (inode, size, times, never through a symlink) and its folder."""
    try:
        f, d = os.stat(path, follow_symlinks=False), os.stat(path.parent)
    except OSError:
        return None
    return (pin, f.st_ino, f.st_size, f.st_mtime_ns, f.st_ctime_ns, f.st_mode, f.st_uid,
            d.st_ino, d.st_mode, d.st_uid, d.st_ctime_ns)


def verified(where=None, manifest=plugins.MANIFEST):
    """{plugin file: its path} for each plugin that verifies now (plugins.verify): the only paths a config may hold. A
    file and folder unchanged since they last verified aren't hashed again (the polls); write() and reload() clear that
    first, so voice-run.conf is always written from a fresh hash."""
    found = {}
    for name in manifest:
        path = Path(where or plugins.folder()) / name
        key = _stat(path, manifest[name])
        if key is not None and _VERIFIED.get(str(path)) == key:
            found[name] = str(path)
            continue
        _VERIFIED.pop(str(path), None)
        try:
            found[name] = str(plugins.verify(name, where, manifest))
        except (plugins.Refused, OSError):
            continue
        if key is not None:  # taken before the hash: a change since misses on the next call, so it is hashed again
            _VERIFIED[str(path)] = key
    return found


def _plugins(cfg):
    """{plugin file: path} of the plugins a chain config (any JSON on disk) loads; None when it isn't a config."""
    try:
        return {n["plugin"].rsplit("/", 1)[-1]: n["plugin"] for n in _graph_nodes(cfg) if n.get("type") == "ladspa"}
    except (AttributeError, KeyError, TypeError):
        return None


def _running(file):
    """The chain config Booth Mic runs (`file`: voice.conf): voice-run.conf, its pre-start's; voice.conf until a first
    start of the unit that loads voice-run.conf (an older install's unit loaded voice.conf)."""
    run = file.with_name(RUN_CONF)
    return _load(run if run.exists() else file)


def loaded(file=None, found=None):
    """`found` (verified() now when not given) narrowed to the plugins the running chain loaded: the stages that process
    the voice now."""
    kept = _plugins(_running(file or path())) or {}
    return {k: v for k, v in (verified() if found is None else found).items() if kept.get(k) == v}


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


def _capture(cfg, key):
    """`key` of a selector config's (any JSON on disk) capture.props: CHANNEL, the capture channel it was built for;
    "target.object", the source it takes. None when absent."""
    try:
        return next(m["args"]["capture.props"][key] for m in cfg["context.modules"]
                    if m["name"] == "libpipewire-module-loopback")
    except (AttributeError, KeyError, StopIteration, TypeError):
        return None


def _named(node):
    """A source's node name → SCARLETT, PRO_X_MIC or BRIO_MIC, or None."""
    return next((n for n, prefix in ((SCARLETT, scarlett.SOURCE),) + BACKUPS if (node or "").startswith(prefix)), None)


def choose(state, on, dump, current):
    """The one source choice for the selector → (the mic: SCARLETT, PRO_X_MIC, BRIO_MIC or None, what apply() takes).
    `state`: scarlett.read; `on`: the PRO X headset answers on (HeadsetMonitor.on; None: not answered yet); `dump`:
    pw-dump JSON, read while the Scarlett is absent; `current`: the source the selector config on disk takes. The
    Scarlett in ALSA is always the Scarlett, its mic routed or not (not routed: apply stops the selector, never another
    mic). Absent from ALSA and PipeWire: the PRO X mic while on, else the BRIO mic, at its first discovered position (a
    backup with none is skipped). Neither: a backup's selector is stopped ({"mic": None}); the Scarlett's keeps its config (None: kept).
    In ALSA, routed, with no node in `state` and none in `dump` either: NO_INPUT (`dump`: "" when not needed). A kept
    selector is named by what it takes (`current`), never by what it would take."""
    try:
        known = isinstance(json.loads(dump), list)
    except ValueError:
        known = False
    if state is not None:
        return (NO_INPUT if known and state["mic"] and not scarlett._source(dump)[0] else SCARLETT), state
    if not known:  # pw-dump failed: nothing to choose from, the selector stays as it is
        return _named(current), None
    if scarlett._source(dump)[0]:  # amixer failed, or the card is going: still in PipeWire, so not absent yet
        return _named(current), None
    if on is None:  # not answered yet (Booth just started): the PRO X kept; a fresh choice takes the BRIO, a mic that
        on = _named(current) == PRO_X_MIC  # carries sound, since an off headset's dongle stays in PipeWire, silent
    for name, prefix in BACKUPS:
        if name == PRO_X_MIC and not on:  # off: its dongle stays in PipeWire, its mic carries nothing
            continue
        node, positions = scarlett._source(dump, prefix)
        if node and positions[0]:  # channel None: never kept as a Scarlett channel by apply
            return name, {"mic": {"node": node, "position": positions[0], "channel": None}}
    return None, ({"mic": None} if _named(current) in (PRO_X_MIC, BRIO_MIC) else None)


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


def running(dump=None):
    """False when `pw-dump` JSON (read now when not given) lists no Booth Mic input: its service is stopped or failed
    to start. True when pw-dump gave nothing: never blamed on Booth Mic."""
    dump = _dump() if dump is None else dump
    return not dump or _node_id(dump, INPUT) is not None


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


def _mtime(file):
    try:
        return file.stat().st_mtime_ns
    except OSError:
        return -1


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
    restarted, only while Booth Mic runs: with it stopped the selector is stopped and its new config kept for its next
    start, never the old channel. Returns what it did, with "not …" when it couldn't: then that change isn't recorded
    (a failed chain restart puts the old voice.conf back; a failed selector restart removes its config), so the next
    apply retries. A live change is recorded only once set-param took it.
    A chain change that fails never holds back the selector."""
    file = file or path()
    select_file = file.with_name(SELECT_CONF)
    with _LOCK:  # built inside: the plugins verified just before the write, the latest settings applied last
        found = verified()
        chain, sel_text = json.loads(build(settings, found)), build_selector(state["mic"]) if state else None
        sel = sel_text and json.loads(sel_text)
        old, old_text, old_sel_text, said = _load(file), _text(file), _text(select_file), None
        now = _running(file)
        kept = _plugins(now)
        if kept is not None and kept != found:  # what verifies changed: the running chain's plugins, dry if not now
            v = dict(settings.get("voice") or {}, **dict.fromkeys(missing(found), False))
            same = json.loads(build(dict(settings, voice=v), kept))
            if shape(same) == shape(now):  # nothing else changed: no restart; its next start loads what verifies
                chain = same
        restart = shape(now) != shape(chain)
        # the values Booth Mic runs with: its run conf's when its pre-start ran after apply last recorded voice.conf (a
        # kept run conf holds older ones), else voice.conf's, what apply set live since
        live = now if not restart and _mtime(file.with_name(RUN_CONF)) > _mtime(file) else old
        changed = old != chain or live != chain
        if live != chain and not restart and not ((cmd := set_params_cmd(dump(), chain)) and run(cmd)):
            said = "not applied: Booth Mic didn't take the change"  # no input node (pw-dump failed): not sent either
        # no Scarlett node (pw-dump failed, or it left PipeWire): kept only on the channel the ALSA routing still gives
        mic = state and state["mic"]
        kept = mic and not mic["node"] and mic["channel"] == _capture(_load(select_file), CHANNEL)
        if state is not None and sel is None and old_sel_text is not None and not kept:
            if not run(STOP_SELECT):  # the config stays: the next apply stops it again
                return "not stopped: Booth Mic's source still takes the mic's old channel"
            _put_back(select_file, None)
        # the selector's own save first: a chain change that fails never holds back a new channel
        sel_said = sel is not None and sel != _load(select_file) and _save(sel, select_file)
        moved = sel_said is None
        if changed and not said:  # also when only the run conf was behind: recorded newer, so sent once
            said = _save(chain, file)
        restart = restart and not said  # a chain that isn't written: nothing new to restart it with
        said = said or sel_said or None
        if not (restart or moved):
            return said or ("live" if changed else "unchanged")
        # RESTART_SELECT: Booth Mic, and a call's link to it, stay; never with Booth Mic stopped (or starting), which
        # it would start (BindsTo): the new selector config stays, for whatever starts it next
        if not restart and not run(ACTIVE):
            if not run(STOP_SELECT):
                return "not stopped: Booth Mic's source still takes the mic's old channel"
            return "selector stopped: Booth Mic is stopped" + (f", but {said}" if said else "")
        if not run(RESTART if restart else RESTART_SELECT):
            if restart:
                _put_back(file, old_text)
            if moved:  # never the old channel back (Restart=, a later start would load it): the next apply retries
                _put_back(select_file, None)
            return "not restarted: Booth Mic's service didn't start"
        if sel is not None:  # RESTART restarts the selector too; shortcut: a pw-dump blip here keys it without the node
            _restarted(_key(sel_text, dump(), state["mic"]["node"]), clock)
        return ("restarted" if restart else "selector restarted") + (f", but {said}" if said else "")


def _choice(settings):
    return (settings.get("microphone") or {}).get("input", scarlett.DEFAULT_INPUT)


def current(file=None):
    """The mic the selector config on disk takes (`file`: voice.conf): SCARLETT, PRO_X_MIC, BRIO_MIC, or None."""
    return _named(_capture(_load((file or path()).with_name(SELECT_CONF)), "target.object"))


def _owned(run=devices._run):
    """A Booth primary instance runs: its bus name has an owner. False when the bus can't be asked."""
    return "true" in run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                         "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner", service.APP_ID)


def may_select(select_file, owned=_owned):
    """booth-mic-select.service's ExecCondition: True when the selector config `select_file` may start. The Scarlett's
    always; a backup's only while Booth runs to say it, else removed (Booth Mic silent until Booth heals and says it):
    only the very file read, never one Booth wrote while the bus was asked (store.save: always a new inode)."""
    try:
        with open(select_file) as f:
            cfg, ino = json.loads(f.read()), os.fstat(f.fileno()).st_ino
    except (OSError, ValueError):
        return False
    source = _named(_capture(cfg, "target.object"))
    if source in dict(BACKUPS) and not owned():
        aside = select_file.with_name(f".{select_file.name}.refused")
        try:  # moved aside atomically, then checked: a newer config goes back unless a newer one still is there
            os.replace(select_file, aside)
            if os.stat(aside).st_ino != ino:
                try:
                    os.link(aside, select_file)
                except FileExistsError:
                    pass
            os.unlink(aside)
        except OSError:
            pass
        return False
    return source is not None


def release(run=service.run, file=None):
    """Booth's shutdown: a backup selector stopped and its config removed (config first, so nothing restarts it),
    since no Booth is left to say it; the Scarlett's runs on. No heal moves the selector after this."""
    global CLOSED
    with _LOCK:
        CLOSED = True
        if current(file) in dict(BACKUPS):
            _put_back((file or path()).with_name(SELECT_CONF), None)
            run(STOP_SELECT)


def heal(settings, read=scarlett.read, run=service.run, dump=_dump, file=None, clock=time.monotonic, on=lambda: False):
    """Every 10 s (and on a debounced sound-card or headset change): apply() with the mic choose() picks now, from the
    Scarlett read now, so a reroute, a new channel, a renamed Scarlett node or a fallback is followed with or without a
    window; `on()`: the PRO X headset answers on (None: not yet). The choice is kept in CHOSEN. Then restart the selector when it should
    run, its source and Booth Mic's input are in PipeWire and it isn't: it unloads itself when its source goes, or when
    it started before it at login. Never without Booth Mic's input (BindsTo would pull a stopped booth-voice.service
    back up), and at most once a minute for the same config and source node, so a selector that won't stay up isn't
    restarted every tick. True when it restarted it."""
    global CHOSEN, SEEN
    select_file = (file or path()).with_name(SELECT_CONF)
    with _LOCK:  # read, chosen and applied as one: a page poll's apply never lands between
        SEEN = None
        if CLOSED:
            return False
        state = read(_choice(settings))
        mic = state and state["mic"]
        CHOSEN, state = choose(state, on(), dump() if state is None or (mic and not mic["node"]) else "",
                               _capture(_load(select_file), "target.object"))
        if state is None:
            return False  # unplugged with no backup (or pw-dump failed): the selector keeps its config
        said = apply(settings, state, run, dump, file, clock)
        sel, on_disk = build_selector(state["mic"]), _load(select_file)
        if said.startswith("not ") and on_disk != (sel and json.loads(sel)):  # not moved: named by what still feeds
            CHOSEN = _named(_capture(on_disk, "target.object"))
        # only the selector's own config on disk: never revived on an old channel, whatever the chain's save did
        if sel is None or on_disk != json.loads(sel):
            return False
        if said.startswith(("restarted", "selector restarted")):
            return True  # by apply, which recorded it: it is coming up
        now = SEEN = dump()
        key = _key(sel, now, state["mic"]["node"])
        if key[1] is None or _node_id(now, INPUT) is None or _node_id(now, SELECTOR) is not None:
            return False
        if key in _HEALED and clock() - _HEALED[key] < HEAL_BACKOFF_S:
            return False
        restarted = run(RESTART_SELECT)
        _restarted(key, clock)
        return restarted


def describe(state, selecting=True, said="", running=True, source=None):
    """The Voice processing group's description for a Scarlett poll (scarlett.read), whether the selector runs, what
    apply said, whether Booth Mic runs and, with the Scarlett unplugged, the mic its selector takes (current())."""
    if said.startswith("not stopped"):
        return STALE
    mic = state["mic"] if state else None
    if state and mic is None:
        return NOT_ROUTED
    if not running:
        return STOPPED
    if state is None and source in dict(BACKUPS):
        return f"Booth Mic runs from the {source}: the Scarlett isn't connected"
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
    """booth-voice.service's ExecStartPre: voice-run.conf (`file`: the one its pipewire loads) from the saved settings
    and the plugins verified now in `where`, so a plugin changed since the last write is never loaded. No systemctl, no
    Scarlett: the unit is starting. None; "kept …" when it can't be written but the one there loads only plugins
    verified now, at their verified paths, in the shape this code builds from them (else every apply would restart it):
    Booth Mic starts on it, touched so apply sends it the saved values live; "not …": then the unit doesn't start."""
    _VERIFIED.clear()  # what pipewire loads: hashed now, never a poll's cached verdict
    file, found, settings = file or path().with_name(RUN_CONF), verified(where), store.load()
    cfg = json.loads(build(settings, found))
    said = None if _load(file) == cfg else _save(cfg, file)
    on_disk = _load(file) if said else None
    kept = _plugins(on_disk)
    if (kept is not None and all(found.get(k) == v for k, v in kept.items())
            and shape(on_disk) == shape(json.loads(build(settings, kept)))):
        try:
            os.utime(file)  # newer than voice.conf: its values may be older than what Booth set live
        except OSError:
            pass
        return f"kept the last config, {said}"
    return said


def reload(run=service.run, file=None):
    """install.sh, once no old Booth runs (its poll would fight the new chain): Booth Mic restarted, only if it runs,
    when its chain's plugins aren't the ones verified now, so an install's plugins load now. False: that failed."""
    with _LOCK:
        _VERIFIED.clear()  # an install's fresh plugins: hashed now
        return _plugins(_running(file or path())) == verified() or run(RESTART)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--write"]:
        said = write(*(Path(a) for a in args[1:3]))
        if said:
            print(f"booth.voice: {said}", file=sys.stderr)
        return 1 if said and said.startswith("not ") else 0
    if args == ["--reload"]:
        return 0 if reload() else 1
    if args[:1] == ["--select"] and len(args) == 2:
        return 0 if may_select(Path(args[1])) else 1
    print(sync(store.load()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
