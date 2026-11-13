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
heal() restarts it once the Scarlett is back. #20's fallback changes only the selector's target and positions.
Every stage and the master switch is a dry/wet `mixer`, set live with `pw-cli set-param` on booth.mic.input: a switch
changes only control values, so it never restarts anything; a new stage restarts both.
    python3 -m booth.voice    write the configs from the saved settings and the Scarlett now, start the services"""
import copy
import json
import sys
import threading

from booth import devices, scarlett, service, store

UNIT, SELECT_UNIT = "booth-voice.service", "booth-mic-select.service"
RESTART = ["systemctl", "--user", "restart", UNIT, SELECT_UNIT]  # a new chain: the selector re-links to its new input
START = ["systemctl", "--user", "start", UNIT, SELECT_UNIT]
RESTART_SELECT = ["systemctl", "--user", "restart", SELECT_UNIT]
NODE = "booth_mic"
INPUT = "booth.mic.input"
SELECTOR = "booth.mic.select"
SELECT_CONF = "voice-select.conf"
RUMBLE_HZ = 80.0
WAITING = "Booth Mic is waiting for the Scarlett"
NOT_ROUTED = "Booth Mic has no input: the mic isn't routed to a capture channel"
_LOCK = threading.Lock()  # the page's poll, the service's start and heal may run at once


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
                "capture.props": {"node.name": SELECTOR, "target.object": mic["node"], "node.dont-reconnect": True,
                                  "audio.channels": 1, "audio.position": [mic["position"]],
                                  "stream.dont-remix": True, "node.passive": True},
                "playback.props": {"node.name": f"{SELECTOR}.out", "media.class": "Stream/Output/Audio/Internal",
                                   "target.object": INPUT, "node.dont-reconnect": True, "audio.channels": 1,
                                   "audio.position": ["MONO"], "node.passive": True}}
    return _config("libpipewire-module-loopback", selector)


def _graph_nodes(cfg):
    return [n for m in cfg["context.modules"] for n in m.get("args", {}).get("filter.graph", {}).get("nodes", [])]


def shape(cfg):
    """The config without its control values: what a change needs a restart for."""
    cfg = copy.deepcopy(cfg)
    for n in _graph_nodes(cfg):
        n.pop("control", None)
    return cfg


def _node_id(dump, name):
    """The id of the PipeWire node named `name` in `pw-dump` JSON, or None."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None
    return next((o["id"] for o in objects if isinstance(objects, list) and isinstance(o, dict)
                 and ((o.get("info") or {}).get("props") or {}).get("node.name") == name), None)


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


def apply(settings, mic, run=service.run, dump=_dump, file=None):
    """Bring both configs up to date. New control values are set live on the chain, then recorded; a new chain shape (a
    new stage) restarts both services; a new mic channel or node restarts only the selector. No mic channel (unplugged,
    not routed) keeps the last selector: never a guessed one. Returns what it did, "not …" when it couldn't: then that
    change isn't recorded, so the next apply sees it again and retries."""
    file = file or path()
    select_file = file.with_name(SELECT_CONF)
    chain, sel = json.loads(build(settings)), build_selector(mic)
    sel = sel and json.loads(sel)
    with _LOCK:
        old, said = _load(file), None
        live = isinstance(old, dict) and "context.modules" in old and shape(old) == shape(chain)
        if old != chain:
            if live and (cmd := set_params_cmd(dump(), chain)) and not run(cmd):
                return "not applied: Booth Mic didn't take the change"
            if said := _save(chain, file):
                return said
        moved = sel is not None and sel != _load(select_file) and not (said := _save(sel, select_file))
        if old != chain and not live:
            run(RESTART)
            return said or "restarted"
        if moved:
            run(RESTART_SELECT)  # Booth Mic, and a call's link to it, stay
            return "selector restarted"
        return said or ("live" if old != chain else "unchanged")


def heal(run=service.run, dump=_dump, file=None):
    """Restart the selector when its Scarlett is in PipeWire but the selector isn't: it unloads itself when the Scarlett
    goes, or when it started before the Scarlett (or Booth Mic's input) at login. Returns True when it restarted it."""
    with _LOCK:
        sel = _load((file or path()).with_name(SELECT_CONF))
        try:
            target = sel["context.modules"][-1]["args"]["capture.props"]["target.object"]
        except (TypeError, KeyError, IndexError):
            return False
        now = dump()
        if _node_id(now, target) is None or _node_id(now, SELECTOR) is not None:
            return False
        return run(RESTART_SELECT)


def describe(state, selecting=True):
    """The Voice processing group's description for a Scarlett poll (scarlett.read), and whether the selector runs."""
    mic = state["mic"] if state else None
    if state and mic is None:
        return NOT_ROUTED
    if not mic or not mic["node"] or not mic["position"] or not selecting:
        return WAITING
    return f"Booth Mic takes the Scarlett's capture channel {mic['channel']} ({mic['position']}), in mono"


def sync(settings, read=scarlett.read, run=service.run, dump=_dump, file=None):
    """The Scarlett now → apply → both services started (a no-op when they run). Returns describe."""
    state = read((settings.get("microphone") or {}).get("input", scarlett.DEFAULT_INPUT))
    apply(settings, state["mic"] if state else None, run, dump, file)
    if (file or path()).exists():
        run(START)
    return describe(state)


def main():
    print(sync(store.load()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
