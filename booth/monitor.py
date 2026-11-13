"""Processed monitoring (#18): hear Booth Mic, the processed voice, in the PRO X headset. A loopback booth_mic → the PRO
X sink, run as its own PipeWire client by booth-monitor.service (`pipewire -c monitor.conf`, written beside the settings;
installed by install.sh, never enabled: sync() starts and stops it), so it works with the window closed. It only reads
booth_mic, like any app: it never restarts or reconfigures Booth Mic, its chain or its selector.

Privacy: it plays to the PRO X sink only, by name with node.dont-reconnect, never the default sink. When the routing
(#12) moves output to the Scarlett, or the user picks speakers, the voice never goes to the room (the Scarlett's
headphone jack shares its rear line outputs); on the Scarlett, Direct Monitor is the monitoring. That sink's monitor
carries it to any app capturing the computer's sound (a screen share with sound, a desktop recording): the label says so.
A PRO X that is off keeps its dongle's sink: sync() stops the loopback then (PAUSED, the switch stays on), so Booth Mic
and its mic aren't held open for a dead headset, and starts it again when the headset is on. With the dongle gone
WirePlumber destroys the loopback instead of moving it; the same with booth_mic gone (Booth Mic restarted), and sync(),
every 10 s, starts it again once both are back.

While on it reads Booth Mic, so Booth Mic and the Scarlett stay in use (and the mic-in-use indicator shows): the label
says so. Mute (#21) is Booth Mic's node mute, so monitoring hears what the call hears, silence; a fallback (#20) moves
only the selector, so monitoring follows Booth Mic onto the backup mic, as the call does."""
import json
import threading
import time

from booth import devices, service, store, voice

UNIT = "booth-monitor.service"
RESTART = ["systemctl", "--user", "restart", UNIT]  # starts it too
STOP = ["systemctl", "--user", "stop", UNIT]
CAPTURE, PLAYBACK = "booth.monitor", "booth.monitor.out"
# Measured (#18): ticks through a copy of Booth Mic's chain and this loopback against the same ticks sent direct, one
# clock: 40 ms at quantum 1024, 35.5 at 256; RNNoise's frames are most of it (without it 5 ms: the limiter's lookahead).
# The Scarlett's capture and the headset's playback and radio add theirs on top: not measurable without playing to it.
DELAY_MS = 40
LABEL = (f"Booth adds about {DELAY_MS} ms, the Scarlett and the headset a little more. Plays in the PRO X headset, and "
         "apps capturing the computer's sound (screen sharing with sound, desktop recording) hear it too; keeps Booth Mic "
         "and the Scarlett in use while on")
PAUSED = "Paused: the PRO X is off"
NO_PROX = "Waiting for the PRO X headset's dongle"
NO_MIC = "Waiting for Booth Mic"
NOT_RUNNING = "not running: Booth tries again within a minute"
BACKOFF_S = 60
_LOCK = threading.Lock()  # the page's switch and the 10 s tick may sync at once
_STARTED = {}  # (Booth Mic's object.serial, the PRO X sink's) → when sync last started the unit for them
LAST = None  # sync()'s latest result, None before the first


def path():
    return store.path().parent / "monitor.conf"


def build(sink):
    """The PRO X sink's node name → booth-monitor.service's config text. Mono both ends: the playback stream upmixes it
    to both ears. dont-reconnect both ends: never another mic, never another output."""
    loop = {"node.description": "Booth monitoring", "audio.channels": 1, "audio.position": ["MONO"],
            "capture.props": {"node.name": CAPTURE, "media.class": "Stream/Input/Audio", "target.object": voice.NODE,
                              "node.dont-reconnect": True, "audio.channels": 1, "audio.position": ["MONO"]},
            "playback.props": {"node.name": PLAYBACK, "media.class": "Stream/Output/Audio", "target.object": sink,
                               "node.dont-reconnect": True, "audio.channels": 1, "audio.position": ["MONO"]}}
    return voice._config("libpipewire-module-loopback", loop)


def _serial(node):
    return node["info"]["props"].get("object.serial", node["id"])


def sync(*args, **kwargs):
    """_sync(), its result kept in LAST for the page's poll: a heal tick's result shows on the switch's row too."""
    global LAST
    with _LOCK:
        LAST = _sync(*args, **kwargs)
        return LAST


def problem(said):
    """A sync() result → what the switch's row must say, or None when it is on or off as switched."""
    if said in (None, "on", "off"):
        return None
    return f"Set, but {said}" if said.startswith("not ") else said


def _sync(settings, run=service.run, dump=voice._dump, file=None, clock=time.monotonic, on=None):
    """The switch (settings["voice"]["monitor"]) → the unit. Off, or on with the PRO X off (`on`: HeadsetMonitor.on,
    None until it answers): stopped, then its config removed (ExecCondition: then nothing can start it); a failed stop
    keeps the config, so the next call stops it again. On: the config for the PRO X sink found now, started (restart)
    when it is new, or when the loopback isn't in PipeWire, Booth Mic is, and it wasn't started for these same nodes in
    the last BACKOFF_S. Nothing without the PRO X sink in pw-dump. Returns what it did: "on", "off", PAUSED, NO_PROX,
    NO_MIC, or "not …"."""
    file = file or path()
    switched = (settings.get("voice") or {}).get("monitor") is True
    if not switched or on is False:
        if not file.exists():
            return PAUSED if switched else "off"
        if not run(STOP):
            return f"not stopped: see systemctl --user status {UNIT}"
        voice._put_back(file, None)
        return PAUSED if switched else "off"
    now = dump()
    sink = devices.find_sink(devices.parse_sinks(now)[0])
    if sink is None:  # unplugged, or pw-dump failed: the running loopback (if any) is left to its own end
        return NO_PROX
    cfg = json.loads(build(sink))
    changed = cfg != voice._load(file)
    if changed and (said := voice._save(cfg, file)):
        return said
    mic = voice._node(now, voice.NODE)
    if mic is None:
        return NO_MIC
    up = voice._node_id(now, CAPTURE) is not None and voice._node_id(now, PLAYBACK) is not None
    if up and not changed:
        return "on"
    key = (_serial(mic), _serial(voice._node(now, sink)))
    if not changed and key in _STARTED and clock() - _STARTED[key] < BACKOFF_S:
        return NOT_RUNNING
    _STARTED.clear()
    _STARTED[key] = clock()
    return "on" if run(RESTART) else f"not started: see systemctl --user status {UNIT}"
