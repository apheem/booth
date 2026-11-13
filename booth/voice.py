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
changes only control values, so it never restarts anything; a new stage restarts both.
    python3 -m booth.voice    write the configs from the saved settings and the Scarlett now, start the services"""
import copy
import json
import sys
import threading
import time

from booth import devices, scarlett, service, store

UNIT, SELECT_UNIT = "booth-voice.service", "booth-mic-select.service"
RESTART = ["systemctl", "--user", "restart", UNIT, SELECT_UNIT]  # a new chain: the selector re-links to its new input
START = ["systemctl", "--user", "start", UNIT, SELECT_UNIT]
RESTART_SELECT = ["systemctl", "--user", "restart", SELECT_UNIT]
STOP_SELECT = ["systemctl", "--user", "stop", SELECT_UNIT]
NODE = "booth_mic"
INPUT = "booth.mic.input"
SELECTOR = "booth.mic.select"
SELECT_CONF = "voice-select.conf"
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


def build(settings):
    """The settings → Booth Mic's chain config text (booth-voice.service), whatever the Scarlett does."""
    v = settings.get("voice") or {}
    nodes = [{"type": "builtin", "name": "in", "label": "copy"},
             {"type": "builtin", "name": "rumble", "label": "bq_highpass", "control": {"Freq": RUMBLE_HZ, "Q": 0.707}},
             _mix("rumble_mix", v.get("rumble", True)),
             _mix("master", v.get("enabled", True))]
    links = [{"output": "in:Out", "input": "rumble:In"},
             {"output": "rumble:Out", "input": "rumble_mix:In 1"},
             {"output": "in:Out", "input": "rumble_mix:In 2"},
             {"output": "rumble_mix:Out", "input": "master:In 1"},
             {"output": "in:Out", "input": "master:In 2"}]
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
                                  "audio.channels": 1, "audio.position": [mic["position"]],
                                  "stream.dont-remix": True, "node.passive": True},
                "playback.props": {"node.name": f"{SELECTOR}.out", "media.class": "Stream/Output/Audio/Internal",
                                   "target.object": INPUT, "node.dont-reconnect": True, "audio.channels": 1,
                                   "audio.position": ["MONO"], "node.passive": True}}
    return _config("libpipewire-module-loopback", selector)


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


def apply(settings, state, run=service.run, dump=_dump, file=None):
    """Bring both configs up to date with the settings and `state`, the Scarlett now (scarlett.read). New control values
    are set live on the chain, then recorded; a new chain shape (a new stage) restarts both services; a new mic channel
    or node restarts only the selector. Unplugged (None) keeps the last selector: it unloaded itself and heal() brings
    it back. The Scarlett there with no channel to take (not routed, or none at the mic's position) stops the selector
    and removes its config: never a guessed channel, never one the mic has left; no Scarlett node (pw-dump failed) changes
    no selector. A new selector restarts only with Booth Mic's input there. Returns what it did, with "not …" when it
    couldn't: then that change isn't recorded (a failed restart puts the old config back), so the next apply retries.
    A chain change that fails never holds back the selector."""
    file = file or path()
    select_file = file.with_name(SELECT_CONF)
    chain, sel = json.loads(build(settings)), build_selector(state["mic"]) if state else None
    sel = sel and json.loads(sel)
    with _LOCK:
        old, old_text, old_sel_text, said = _load(file), _text(file), _text(select_file), None
        restart = shape(old) != shape(chain)
        if old != chain and not restart and (cmd := set_params_cmd(dump(), chain)) and not run(cmd):
            said = "not applied: Booth Mic didn't take the change"
        # a node None is PipeWire unknown (pw-dump failed) or without the Scarlett: never a reason to stop it
        known = state is not None and (state["mic"] is None or state["mic"]["node"])
        if known and sel is None and old_sel_text is not None:
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
        if not restart and _node_id(dump(), INPUT) is None:  # BindsTo would pull a stopped booth-voice back up
            return said or "selector saved: heal starts it with Booth Mic's input"
        if not run(RESTART if restart else RESTART_SELECT):  # RESTART_SELECT: Booth Mic, and a call's link to it, stay
            if restart:
                _put_back(file, old_text)
            if moved:
                _put_back(select_file, old_sel_text)
            return "not restarted: Booth Mic's service didn't start"
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
        said = apply(settings, state, run, dump, file)
        sel = build_selector(state["mic"])
        # only the selector's own config on disk: never revived on an old channel, whatever the chain's save did
        if sel is None or _load((file or path()).with_name(SELECT_CONF)) != json.loads(sel):
            return False
        now = dump()
        scarlett_node = _node(now, state["mic"]["node"])
        # object.serial, never reused as a node id is: tells a replug (restart now) from the same selector failing again
        key = (sel, scarlett_node and scarlett_node["info"]["props"].get("object.serial", scarlett_node["id"]))
        restarted = said.startswith(("restarted", "selector restarted"))  # by apply: it is coming up
        if not restarted:
            if key[1] is None or _node_id(now, INPUT) is None or _node_id(now, SELECTOR) is not None:
                return False
            if key in _HEALED and clock() - _HEALED[key] < HEAL_BACKOFF_S:
                return False
            restarted = run(RESTART_SELECT)
        _HEALED.clear()
        _HEALED[key] = clock()
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


def main():
    print(sync(store.load()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
