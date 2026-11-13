"""The Scarlett's stuck output, found and restarted (seen 2026-11-13 on the Solo 4th Gen, firmware 2188).

The fault: the Scarlett stops playing while the computer still says it plays. PipeWire's sink runs, ALSA's pointer
advances, the kernel logs nothing and the inputs keep working; inside the device nothing arrives (its own Level Meter
reads 0 on every playback point). It lasts through suspends, until the device's sample rate is set anew: a replug, or
any change of rate. What starts it is not known: reading the mixer hard, suspend and resume, capture starting and
stopping, an underrun, the camera streaming, Direct Monitor and Booth Mic's chain were each tried, and none does.

Found (look, stuck): while the Scarlett's sink runs, what PipeWire hands it (its monitor's peak times the sink's
volume) against the device's Level Meter. Signal sent and no playback point moving, in two looks: stuck.
Restarted (restart): one pair of silent streams, a playback to its sink and a capture from its source, in one
node.group and with node.force-rate at another rate than the device runs at. PipeWire takes both directions through
the rate change, which restarts the device's playback; a playback stream alone changes nothing while anything
records, as the two share the device's clock. Streams already running keep running, and no PipeWire setting is
written. The graph goes back to its own rate by itself once everything on the Scarlett is idle: forcing it back while
streams run is ignored, and a second pair right behind the first stalled them, so restart sends one pair only.
shortcut: until that idle Booth Mic's chain runs at the other rate; restart Booth Mic then if its noise suppression
ever sounds wrong after a restart mid-call."""
import array
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import wave

from gi.repository import GLib

from booth import devices, routing, scarlett

METER = "iface=PCM,name=Level Meter"
POINTS = 12  # the Solo 4th Gen's meter: any other count is another device's layout, never judged
# the points playback reaches with the Solo's stock routing: the analogue outputs, the mixer's PCM inputs, the loopback
PLAYBACK = (0, 1, 2, 3, 10, 11)
SENT_MIN = 0.01  # -40 dBFS at the device: its meter reads about 40 of 4095 there, never 0
LOOK_S = 0.6
RESTART_S = 0.4
BACKOFF_S = 60  # between two restarts
REVIVED = "The Scarlett's output had stopped — Booth restarted it"
GAVE_UP = "The Scarlett's output has stopped and restarting it didn't help — unplug it and plug it back in"
_VALUES = re.compile(r": values=(\d+(?:,\d+)*)")


def nodes(dump):
    """`pw-dump` JSON → {"sink", "source": the Scarlett's node names (source None when gone), "running": its sink
    runs, "gain": the sink's linear volume, 0 when muted, "apps": the applications with a running stream}, or None
    without the Scarlett's sink."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None
    found = {"sink": None, "source": None, "apps": []}
    for o in objects if isinstance(objects, list) else []:
        info = o.get("info") or {}
        props, name = info.get("props") or {}, (info.get("props") or {}).get("node.name") or ""
        if name.startswith(routing.SINK):
            p = ((info.get("params") or {}).get("Props") or [{}])[0]
            found.update(sink=name, running=info.get("state") == "running",
                         gain=0.0 if p.get("mute") else p.get("volume", 1.0) * max(p.get("channelVolumes") or [1.0]))
        elif name.startswith(scarlett.SOURCE):
            found["source"] = name
        elif str(props.get("media.class")).startswith("Stream/") and info.get("state") == "running":
            found["apps"].append(props.get("application.name") or name)
    return found if found["sink"] else None


def parse_meter(text):
    """`amixer cget` of the Level Meter → its points, [] when it couldn't be read."""
    m = _VALUES.search(text)
    return [int(v) for v in m.group(1).split(",")] if m else []


def peak(raw):
    """pw-record's raw float32 samples → their peak, 0..1."""
    samples = array.array("f")
    samples.frombytes(raw[:len(raw) // 4 * 4])
    return max(map(abs, samples), default=0.0)


def stuck(sent, meters):
    """True when signal goes to the device (`sent`: its peak there) and no playback point of its meter moves. Too
    quiet to show on the meter, a meter that couldn't be read or another layout: unknown, never stuck."""
    return sent >= SENT_MIN and len(meters) == POINTS and not any(meters[i] for i in PLAYBACK)


def listen(sink):
    """The sink's monitor for LOOK_S, by name: mono at 8 kHz, so the whole look waits in the pipe and the recorder is
    never left blocked on it (a blocked one doesn't end)."""
    return subprocess.Popen(["timeout", str(LOOK_S), "pw-record", "--target", sink, "--channels", "1", "--rate", "8000",
                             "--format", "f32", "-P", "{ stream.capture.sink = true node.dont-reconnect = true }", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def look(found, run=devices._run, card=devices.scarlett_card, listen=listen):
    """One look, LOOK_S long → (the peak sent to the device, each meter point's highest of three reads; [] when a
    read failed)."""
    card_id = card()
    if card_id is None:
        return 0.0, []
    heard, reads = listen(found["sink"]), []
    for _ in range(3):
        time.sleep(LOOK_S / 4)
        reads.append(parse_meter(run("amixer", "-c", card_id, "cget", METER)))
    raw = heard.communicate()[0]
    return peak(raw) * found["gain"], [max(point) for point in zip(*reads)]  # zip: a failed read leaves no points


def other_rate(hw_params):
    """The playback stream's /proc hw_params → a rate the device isn't at: the same one again changes nothing."""
    return 48000 if "rate: 44100" in hw_params else 44100


def _hw_params(card=devices.scarlett_card):
    try:
        return open(f"/proc/asound/{card()}/pcm0p/sub0/hw_params").read()
    except OSError:
        return ""


def restart(found, run=devices._run, popen=subprocess.Popen, hw_params=_hw_params):
    """The pair of silent streams (see the module's text): the capture's samples are discarded unread."""
    rate = other_rate(hw_params())
    props = f"{{ node.dont-reconnect = true node.force-rate = {rate} node.group = booth.revive }}"
    path, cap = os.path.join(GLib.get_user_runtime_dir(), "booth-silence.wav"), None
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(int(rate * RESTART_S) * 4))
        if found["source"]:
            cap = popen(["pw-record", "--target", found["source"], "--channels", "1", "--rate", "8000", "--format",
                         "s16", "-P", props, "-"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run("pw-play", "--target", found["sink"], "-P", props, path)
    finally:
        if cap:
            cap.kill()
            cap.wait()
        with contextlib.suppress(OSError):
            os.unlink(path)


class Watch:
    """check(), from the service's tick on a worker → the notification to send, or None. A restart only after two
    stuck looks, at most one per BACKOFF_S, and after one that didn't help none until the device is heard again."""

    def __init__(self, run=devices._run, look=look, restart=restart, clock=time.monotonic, sleep=time.sleep,
                 log=lambda text: print(text, file=sys.stderr)):
        self.run, self.look, self.restart, self.clock, self.sleep, self.log = run, look, restart, clock, sleep, log
        self.revived, self.failed = float("-inf"), False

    def check(self):
        found = nodes(self.run("pw-dump"))
        if not found or not found["running"]:
            return None
        sent, meters = self.look(found)
        if not stuck(sent, meters):
            if sent >= SENT_MIN and meters:  # heard inside the device: a later stop gets its own restart
                self.failed = False
            return None
        if self.failed or self.clock() - self.revived < BACKOFF_S:
            return None
        self.sleep(1)
        if not stuck(*self.look(found)):  # the sound began as the first look ended
            return None
        self.revived = self.clock()
        self.log(f"Booth: the Scarlett's output was stuck (sent {sent:.3f}, its meter {meters}); restarting it. "
                 f"Streams running: {', '.join(found['apps']) or 'none'}")  # what was on, to find what starts it
        self.restart(found)
        self.sleep(1)
        self.failed = stuck(*self.look(found))
        return GAVE_UP if self.failed else REVIVED
