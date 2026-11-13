"""Booth Stereo (#19): a second virtual source, "Booth Stereo (for recording)", for recording apps (Audacity, OBS): the
processed voice on the side the user picks, and on the other a source from a dropdown: the Scarlett's other input (its
instrument jack while the mic is on the XLR), the computer's sound (the configured default output's monitor) or any
other source. Booth Mic stays mono and untouched, so a call never goes one-eared.

booth-stereo.service runs it as its own PipeWire client (`pipewire -c stereo.conf`, installed by install.sh, never
enabled: sync() starts it, as booth.monitor's), so a new side or source restarts only it: never booth-voice,
booth-mic-select or booth_mic. Its one config holds three modules:
  booth.stereo.voice  its own copy of Booth Mic's chain (voice.build: the same stages, settings and verified plugins) on
                      booth.mic.input's monitor: the mic Booth Mic takes now (a fallback, #20, is followed), before
                      processing and before the mute → booth.stereo.input, on the voice's side
  booth.stereo.other  the other source → booth.stereo.input, on the other side. None when it is missing: that side is
                      silent and the row says so, never another source
  booth.stereo.input → booth_stereo, the stereo Audio/Source (FL, FR)
Each is passive and pinned to its target (target.object + node.dont-reconnect): nothing runs and no device is opened for
it while no app records Booth Stereo or reads Booth Mic, and a source that goes is never replaced by WirePlumber's pick.
shortcut: PipeWire 1.0.5's passive links wake both ways, so while an app reads Booth Mic (a call) Booth Stereo's chain
and its other source run too (measured: a BRIO mic on the other side opens; nothing reaches an app); upgrade to one-way
passive ports (PipeWire 1.2's node.passive = "in"/"out") once the desk has them.
Mute (#21) is booth_mic's node mute and Booth Mic's master gains: Booth Stereo reads neither, so it carries the voice even
while Booth Mic is muted (NOTE says so); its own master follows only the Voice processing switch. It is never the default
mic: Booth never makes it one, and its priority is below any mic's.
Its pre-start (`--write`) writes stereo.conf from the saved settings, the source found then and the plugins verified
then, as booth-voice's does, so a plugin changed since is never loaded. sync() runs on every heal tick and every voice
change: a knob or preset is set live on its chain too (voice.set_live on booth.stereo.voice, read back), never a restart.
    python3 -m booth.stereo --write [stereo.conf [plugins folder]]   booth-stereo.service's ExecStartPre"""
import json
import sys
import threading
import time
from pathlib import Path

from booth import devices, scarlett, service, store, voice

UNIT = "booth-stereo.service"
RESTART = ["systemctl", "--user", "restart", UNIT]  # starts it too
NODE, SINK, VOICE, OTHER = "booth_stereo", "booth.stereo.input", "booth.stereo.voice", "booth.stereo.other"
DESCRIPTION = "Booth Stereo (for recording)"
SCARLETT, COMPUTER = "scarlett", "computer"  # the other side's choices besides a source's node name
SIDES = {"L": ("FL", "Left"), "R": ("FR", "Right")}  # settings voice "stereo_side", the voice's: position, name
NOTE = ("For recording apps, not calls: Booth Stereo carries your voice even while Booth Mic is muted, and its other "
        "side may carry the computer's sound")
NO_MIC = "Waiting for Booth Mic"
NOT_RUNNING = "not running: Booth tries again within a minute"
BACKOFF_S = 60
_LOCK = threading.Lock()  # the heal tick, the page's changes and the voice throttle may sync at once
_STARTED = {}  # (the config's shape, booth.mic.input's object.serial) → when sync last started the unit for them
LAST = None  # sync()'s latest result, None before the first
SOURCES = []  # sources() as sync() last found them, for the page's dropdown


def path():
    return store.path().parent / "stereo.conf"


def _v(settings):
    return settings.get("voice") or {}


def sides(settings):
    """(the voice's side, the other side): "L" or "R"."""
    here = _v(settings).get("stereo_side", "L")
    return here, "R" if here == "L" else "L"


def _own(name):
    return name.startswith(("booth.", "booth_"))  # Booth's nodes: never a side's source (a loop, or an unmuted Booth Mic)


def _objects(dump):
    try:
        objects = json.loads(dump)
    except ValueError:
        return []
    return [o for o in objects if isinstance(o, dict)] if isinstance(objects, list) else []


def sources(dump):
    """`pw-dump` JSON → [(node name, description)] of every audio source a side may take: never Booth's own nodes, nor
    the Scarlett's (its own choice takes one channel; all of it would carry the mic)."""
    found = []
    for o in _objects(dump):
        props = (o.get("info") or {}).get("props") or {}
        name = props.get("node.name") or ""
        if (o.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Source" and name
                and not _own(name) and not name.startswith(scarlett.SOURCE)):
            found.append((name, props.get("node.description") or name))
    return found


def _configured_sink(dump):
    """The configured default output's node name (default.configured.audio.sink, what the user or Booth chose), or
    None: never the one WirePlumber falls back to."""
    for o in _objects(dump):
        if (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if isinstance(m, dict) and m.get("key") == "default.configured.audio.sink" \
                        and isinstance(m.get("value"), dict):
                    return m["value"].get("name")
    return None


def _find(choice, settings, dump, read):
    """The other side's choice now → (its target for build(), or None, and what it carries or why it is silent)."""
    if choice == SCARLETT:  # the input the mic isn't on, its one channel, by position
        mic = (settings.get("microphone") or {}).get("input", scarlett.DEFAULT_INPUT)
        state, (node, positions) = read(mic), scarlett._source(dump)
        if state is None or node is None:
            return None, "the Scarlett isn't connected"
        name = next(k for k in scarlett.INPUTS if k != mic)
        channel = scarlett.routing(state["ctrls"]).get(name)
        if channel is None or channel == (state["mic"] or {}).get("channel") or channel > len(positions):
            return None, f"the Scarlett's {name} input isn't routed to a capture channel"
        position = positions[channel - 1]
        return {"node": node, "position": position}, f"the Scarlett's {name} input, capture channel {channel} ({position})"
    if choice == COMPUTER:  # the default output's monitor, by name now: it follows the output the user picks
        sink = _configured_sink(dump)
        if sink is None:
            return None, "no default output is set"
        if sink not in devices.parse_sinks(dump)[0] or _own(sink):
            return None, "the default output isn't connected"
        props = voice._node(dump, sink)["info"]["props"]
        return {"node": sink, "sink": True}, f"the computer's sound ({props.get('node.description') or sink})"
    found = dict(sources(dump))
    if choice not in found:
        return None, f"{choice} isn't connected"
    return {"node": choice}, found[choice]


def target(settings, dump, read=scarlett.read):
    """The other side's source in `pw-dump` JSON now (and the Scarlett's routing, read(the mic's input)) → (its target
    for build(): {"node", + "position": one Scarlett channel, or "sink": True, an output's monitor}, or None: that side
    silent; the row's text)."""
    there = SIDES[sides(settings)[1]][1]
    other, said = _find(_v(settings).get("stereo_source", SCARLETT), settings, dump, read)
    return other, f"{there}: {said}" if other else f"{there}: silent, {said}"


def choices(settings, found):
    """The dropdown → [(settings value, label)]: the Scarlett, computer sound, each source in `found` (sources()), and the
    saved one when it isn't there now, so a source that is away stays chosen."""
    items = [(SCARLETT, "Scarlett instrument input"), (COMPUTER, "Computer sound")] + list(found)
    saved = _v(settings).get("stereo_source", SCARLETT)
    return items + ([] if saved in dict(items) else [(saved, f"{saved} (not connected)")])


def _into(name, position):
    """A stream into booth.stereo.input, on one side only (dont-remix: never spread to both)."""
    return {"node.name": name, "media.class": "Stream/Output/Audio/Internal", "target.object": SINK,
            "node.dont-reconnect": True, "node.passive": True, "audio.channels": 1, "audio.position": [position],
            "stream.dont-remix": True}


def build(settings, found, other):
    """The settings, the plugins `found` verified and the other side's target (target()) → booth-stereo.service's config
    text. The voice: Booth Mic's chain as voice.build() makes it, never muted (#21)."""
    here, there = sides(settings)
    cfg = json.loads(voice.build(dict(settings, voice=dict(_v(settings), muted=False)), found))
    chain = cfg["context.modules"][-1]["args"]
    chain.update({"node.description": DESCRIPTION, "media.name": DESCRIPTION,
                  # Internal, as Booth Mic's selector: no mic-in-use indicator while no app records Booth Stereo
                  "capture.props": {"node.name": VOICE, "media.class": "Stream/Input/Audio/Internal",
                                    "target.object": voice.INPUT, "stream.capture.sink": True,
                                    "node.dont-reconnect": True, "node.passive": True},
                  "playback.props": _into(f"{VOICE}.out", SIDES[here][0])})
    source = {"node.description": DESCRIPTION, "audio.channels": 2, "audio.position": ["FL", "FR"],
              "capture.props": {"node.name": SINK, "node.description": "Booth Stereo input",
                                "media.class": "Audio/Sink/Internal", "node.passive": True},
              "playback.props": {"node.name": NODE, "node.description": DESCRIPTION, "media.class": "Audio/Source",
                                 "priority.session": -1000}}  # below every mic's: never WirePlumber's fallback pick
    modules = [{"name": "libpipewire-module-loopback", "args": source},
               {"name": "libpipewire-module-filter-chain", "args": chain}]
    if other:
        capture = {"node.name": OTHER, "media.class": "Stream/Input/Audio/Internal", "target.object": other["node"],
                   "node.dont-reconnect": True, "node.passive": True, "audio.channels": 1,
                   "audio.position": [other.get("position", "MONO")]}
        if "position" in other:  # one Scarlett channel, never a mixdown with the mic's
            capture["stream.dont-remix"] = True
        if other.get("sink"):
            capture["stream.capture.sink"] = True
        modules.append({"name": "libpipewire-module-loopback",
                        "args": {"node.description": "Booth Stereo's other side", "audio.channels": 1,
                                 "capture.props": capture, "playback.props": _into(f"{OTHER}.out", SIDES[there][0])}})
    cfg["context.modules"][-1:] = modules
    return json.dumps(cfg, indent=2)  # pretty: PipeWire 1.0.5 skips the modules of a one-line config


def _without_other(cfg):
    """A config without its other-side module: what runs once WirePlumber removed it with its source."""
    try:
        return dict(cfg, **{"context.modules": [m for m in cfg["context.modules"]
                                                if ((m.get("args") or {}).get("capture.props") or {}).get("node.name")
                                                != OTHER]})
    except (AttributeError, KeyError, TypeError):  # any JSON on disk
        return None


def _live(cfg, now, run, dump):
    """The chain's controls set live on booth.stereo.voice when it doesn't run them already → None, or what failed."""
    have = voice._live_params(now, VOICE)
    want = [(f"{n['name']}:{k}", v) for n in voice._graph_nodes(cfg) for k, v in n.get("control", {}).items()]
    if all(k in have and voice._same(v, have[k]) for k, v in want):
        return None
    missed = voice.set_live(cfg, run, dump, VOICE)
    return None if missed == [] else f"not applied: Booth Stereo didn't take {', '.join(missed or ['the change'])}"


def sync(*args, **kwargs):
    """_sync(), its result kept in LAST for the page's poll."""
    global LAST
    with _LOCK:
        LAST = _sync(*args, **kwargs)
        return LAST


def _sync(settings, read=scarlett.read, run=service.run, dump=voice._dump, file=None, clock=time.monotonic):
    """The settings and the desk now → Booth Stereo. Running as wanted: new knob values set live, else nothing. A new
    side, source or chain shape, or not running while Booth Mic's input is there: its unit restarted (its pre-start
    writes the config), at once for a new config or a new booth.mic.input, else at most once in BACKOFF_S. Its other
    side gone (WirePlumber removed that loopback with its source) is what is wanted: silent, no restart. Returns the
    other side's text (target()), NO_MIC, NOT_RUNNING or "not …"."""
    global SOURCES
    file = file or path()
    now = dump()
    mic = voice._node(now, voice.INPUT)
    if mic is None:  # Booth Mic not running, or pw-dump failed: no voice to take
        return NO_MIC
    SOURCES = sources(now)
    other, said = target(settings, now, read)
    found = voice.verified()
    cfg = json.loads(build(settings, found, other))
    if voice._node_id(now, NODE) is not None and voice._node_id(now, VOICE) is not None:
        running = voice._load(file)
        if running is not None and voice._node_id(now, OTHER) is None:
            running = _without_other(running)
        kept = voice._plugins(running)
        if kept is not None and kept != found:  # as Booth Mic's: what verifies changed alone never restarts it
            v = dict(_v(settings), **dict.fromkeys(voice.missing(found), False))
            same = json.loads(build(dict(settings, voice=v), kept, other))
            if voice.shape(same) == voice.shape(running):  # the running chain's plugins, dry if not verified now
                cfg = same
        if voice.shape(cfg) == voice.shape(running):
            return _live(cfg, now, run, dump) or said
    key = (json.dumps(voice.shape(cfg), sort_keys=True), mic["info"]["props"].get("object.serial", mic["id"]))
    if key in _STARTED and clock() - _STARTED[key] < BACKOFF_S:
        return NOT_RUNNING
    _STARTED.clear()
    _STARTED[key] = clock()
    return said if run(RESTART) else f"not started: see systemctl --user status {UNIT}"


def write(file=None, where=None):
    """booth-stereo.service's ExecStartPre: stereo.conf (`file`: the one its pipewire loads) from the saved settings,
    the source found now and the plugins verified now in `where`. None, or "not saved: …": then the unit doesn't
    start."""
    voice._VERIFIED.clear()  # what pipewire loads: hashed now, never a cached verdict
    settings, now = store.load(), voice._dump()
    cfg = build(settings, voice.verified(where), target(settings, now, scarlett.read)[0])
    return voice._save(json.loads(cfg), file or path())


def main():
    args = sys.argv[1:]
    if args[:1] == ["--write"] and len(args) <= 3:
        said = write(*(Path(a) for a in args[1:3]))
        if said:
            print(f"booth.stereo: {said}", file=sys.stderr)
        return 1 if said else 0
    print("usage: python3 -m booth.stereo --write [stereo.conf [plugins folder]]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
