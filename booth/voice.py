"""Booth Mic, the voice chain: a pure builder (settings + the Scarlett's mic record → PipeWire config) and its service,
booth-voice.service (`pipewire -c voice.conf`, installed by install.sh), so Booth Mic lives on with Booth closed.

The graph, each node a separate, stable PipeWire node:
  Scarlett capture_FR → booth.mic.select (loopback: the source selector) → booth.mic.input (hidden internal sink)
    → filter-chain stages → Booth Mic (booth_mic, a mono Audio/Source)
The selector takes only the mic's channel, by position, never a mixdown; it is passive, so the Scarlett is opened only
while an app reads Booth Mic. Retargeting the selector (target.object metadata) never touches Booth Mic.
Every stage and the master switch is a dry/wet `mixer`, set live with `pw-cli set-param` on booth.mic.input: a switch
changes only control values, so it never restarts the service; a new mic channel or stage does.
    python3 -m booth.voice    write the config from the saved settings and the Scarlett now, start the service"""
import copy
import json
import sys
import threading

from booth import devices, scarlett, service, store

UNIT = "booth-voice.service"
RESTART = ["systemctl", "--user", "restart", UNIT]
START = ["systemctl", "--user", "start", UNIT]
NODE = "booth_mic"
INPUT = "booth.mic.input"
SELECTOR = "booth.mic.select"
RUMBLE_HZ = 80.0
WAITING = "Booth Mic is waiting for the Scarlett"
NOT_ROUTED = "Booth Mic has no input: the mic isn't routed to a capture channel"
_LOCK = threading.Lock()  # the page's poll and the service's start may apply at once


def path():
    return store.path().parent / "voice.conf"


def _mix(name, wet):
    """A dry/wet bypass: In 1 the processed signal, In 2 the dry one."""
    return {"type": "builtin", "name": name, "label": "mixer",
            "control": {"Gain 1": 1.0 if wet else 0.0, "Gain 2": 0.0 if wet else 1.0}}


def build(settings, mic):
    """The settings and the mic record (scarlett.mic) → the config text, or None when there is no channel to take."""
    if not mic or not mic.get("node") or not mic.get("position"):
        return None
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
    selector = {"node.description": "Booth Mic source", "audio.channels": 1,
                "capture.props": {"node.name": SELECTOR, "target.object": mic["node"], "audio.channels": 1,
                                  "audio.position": [mic["position"]], "stream.dont-remix": True,
                                  "node.passive": True},
                "playback.props": {"node.name": f"{SELECTOR}.out", "media.class": "Stream/Output/Audio/Internal",
                                   "target.object": INPUT, "audio.channels": 1, "audio.position": ["MONO"],
                                   "node.passive": True}}
    cfg = {"context.properties": {"log.level": 0},
           "context.spa-libs": {"audio.convert.*": "audioconvert/libspa-audioconvert",
                                "support.*": "support/libspa-support"},
           "context.modules": [{"name": "libpipewire-module-rt", "args": {}, "flags": ["ifexists", "nofail"]},
                               {"name": "libpipewire-module-protocol-native"},
                               {"name": "libpipewire-module-client-node"},
                               {"name": "libpipewire-module-adapter"},
                               {"name": "libpipewire-module-filter-chain", "args": chain},
                               {"name": "libpipewire-module-loopback", "args": selector}]}
    return json.dumps(cfg, indent=2)


def _graph_nodes(cfg):
    return [n for m in cfg["context.modules"] for n in m.get("args", {}).get("filter.graph", {}).get("nodes", [])]


def shape(cfg):
    """The config without its control values: what a change needs a restart for."""
    cfg = copy.deepcopy(cfg)
    for n in _graph_nodes(cfg):
        n.pop("control", None)
    return cfg


def set_params_cmd(dump, cfg):
    """The live change to every control in `cfg`, on the running chain's input node (by name, now), or None."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None
    ids = [o["id"] for o in objects if isinstance(objects, list) and isinstance(o, dict)
           and ((o.get("info") or {}).get("props") or {}).get("node.name") == INPUT]
    if not ids:
        return None
    params = [x for n in _graph_nodes(cfg) for k, v in n.get("control", {}).items() for x in (f"{n['name']}:{k}", v)]
    return ["pw-cli", "set-param", str(ids[0]), "Props", json.dumps({"params": params})]


def apply(settings, mic, run=service.run, dump=lambda: devices._run("pw-dump"), file=None):
    """Write the config when it changed: a new shape restarts booth-voice.service, new control values are set live.
    No mic channel (unplugged, not routed) keeps the last config: never a guessed one. Returns what it did."""
    text = build(settings, mic)
    if text is None:
        return "no mic"
    file, new = file or path(), json.loads(text)
    with _LOCK:
        try:
            old = json.loads(file.read_text())
        except (OSError, ValueError):
            old = None
        if old == new:
            return "unchanged"
        try:
            store.save(new, file)
        except OSError as e:  # disk full, read-only: the running chain keeps the config it has
            return f"not saved: {e.strerror or e}"
        if isinstance(old, dict) and "context.modules" in old and shape(old) == shape(new):
            if cmd := set_params_cmd(dump(), new):
                run(cmd)
            return "live"
        run(RESTART)
        return "restarted"


def describe(state):
    """The Voice processing group's description for a Scarlett poll (scarlett.read)."""
    mic = state["mic"] if state else None
    if state and mic is None:
        return NOT_ROUTED
    if not mic or not mic["node"] or not mic["position"]:
        return WAITING
    return f"Booth Mic takes the Scarlett's capture channel {mic['channel']} ({mic['position']}), in mono"


def sync(settings, read=scarlett.read, run=service.run, dump=lambda: devices._run("pw-dump"), file=None):
    """The Scarlett now → apply → the service started (a no-op when it runs), if there is a config. Returns describe."""
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
