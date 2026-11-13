"""The Scarlett's stuck output, heard and restarted (seen 2026-11-13 on the Solo 4th Gen, firmware 2188).

The fault: the Scarlett stops playing while the computer still says it plays. PipeWire's sink runs, ALSA's pointer
advances, the kernel logs nothing and the inputs keep working; inside the device nothing arrives (its own Level Meter
reads 0 on every playback point). It lasts through suspends, until the device's sample rate is set anew: a replug, or
any change of rate.
What starts it: strong content below about 20 Hz in what is played. From a working device at 48 kHz, 15 Hz at -34 dBFS
at the device (-20 dBFS at 20 % volume) kills it within a second and 5 Hz at that level in about six; 25 Hz at that
level, 15 Hz 10 dB quieter and anything quiet or high (1 kHz, 19 kHz) play on. Nothing else does: reading the mixer
hard, suspend and resume, capture starting and stopping, an underrun, the camera streaming, Direct Monitor, Booth
Mic's chain and the test suite were each tried. A 30 Hz 4th-order high-pass in front of the sink prevents it (tried
with a filter-chain sink: 15 Hz at -6 dBFS through it for 20 s and the device played on); Booth doesn't run one yet,
so this module is the repair, not the prevention.

Heard (Watch): one stream listens to the Scarlett sink's monitor for as long as the sink lasts. It is passive, so the
Scarlett still sleeps when nothing plays, and Internal, so no app lists it and no recording indicator shows; nothing
it hears is kept. While sound goes out unbroken (its peak times the sink's volume), the device's Level Meter is read:
no playback point moving, twice in a row, is stuck. That is about half a second of sound; silence can't tell.
Restarted (restart): one pair of silent streams, a playback to its sink and a capture from its source, in one
node.group and with node.force-rate at another rate than the device runs at. PipeWire takes both directions through
the rate change, which restarts the device's playback; a playback stream alone changes nothing while anything
records, as the two share the device's clock. Streams already running keep running, and no PipeWire setting is
written. Any rate but the graph's own is taken while streams run (forcing its own is ignored), so a second stop in
one call goes on to the next rate; the graph goes back to its own by itself once everything on the Scarlett is idle.
A second pair right behind the first stalled the streams, so each restart sends one pair and is checked by ear after.
shortcut: until that idle Booth Mic's chain runs at the other rate; restart Booth Mic then if its noise suppression
ever sounds wrong after a restart mid-call."""
import array
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import wave

from gi.repository import GLib

from booth import devices, routing, scarlett

METER = "iface=PCM,name=Level Meter"
POINTS = 12  # the Solo 4th Gen's meter: any other count is another device's layout, never judged
# the points playback reaches with the Solo's stock routing: the analogue outputs, the mixer's PCM inputs, the loopback
PLAYBACK = (0, 1, 2, 3, 10, 11)
SENT_MIN = 0.01  # -40 dBFS at the device: its meter reads about 40 of 4095 there, never 0
CHUNK_S = 0.1
CHUNK = 800 * 4  # CHUNK_S of mono 8 kHz float32, as listen() asks for
SUSTAIN = 3  # chunks of unbroken sound before the device is judged: its buffer is behind the monitor, a pause ahead
EVERY = 5  # then the meter is read every this many chunks, and every chunk once it read silent
STRIKES = 2
GAIN_S = 5  # the sink's volume is read again this often while sound plays
RESTART_S = 0.4
REVIVED = "The Scarlett's output had stopped — Booth restarted it"
GAVE_UP = "The Scarlett's output has stopped and restarting it didn't help — unplug it and plug it back in"
LISTEN = ("{ stream.capture.sink = true node.dont-reconnect = true node.passive = true "
          "media.class = Stream/Input/Audio/Internal node.name = booth.output.watch }")
_VALUES = re.compile(r": values=(\d+(?:,\d+)*)")


def nodes(dump):
    """`pw-dump` JSON → {"sink", "source": the Scarlett's node names (source None when gone), "id": the sink's, new
    after a replug, "gain": the sink's linear volume, 0 when muted, "apps": the applications with a running stream},
    or None without the Scarlett's sink."""
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
            found.update(sink=name, id=o.get("id"),
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


def listen(sink):
    """The one listening stream, on the sink's monitor by name (see the module's text): mono at 8 kHz."""
    return subprocess.Popen(["pw-record", "--target", sink, "--channels", "1", "--rate", "8000", "--format", "f32",
                             "-P", LISTEN, "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def other_rate(hw_params):
    """The playback stream's /proc hw_params → a rate the device isn't at, and never the graph's own 48000, which
    PipeWire won't force while streams run: 44100, or 88200 from there. The device is touchier at 88200 (25 Hz at
    -34 dBFS stopped it there within a second, twice): that stop is heard like any other and goes back to 44100."""
    return 88200 if "rate: 44100" in hw_params else 44100


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
    """ensure() from the service's tick, on a worker: the listener started when the Scarlett's sink is there and none
    runs, and ended when the sink it listens to was replaced (a replug). stop() at quit. say(text) is called on the
    listener's thread with REVIVED once sound is heard inside the device after a restart, or GAVE_UP when it isn't:
    then nothing is restarted again until the device is heard (a replug)."""

    def __init__(self, say, run=devices._run, card=devices.scarlett_card, restart=restart, listen=listen,
                 clock=time.monotonic, log=lambda text: print(text, file=sys.stderr)):
        self.say, self.run, self.card, self.restart, self.listen = say, run, card, restart, listen
        self.clock, self.log, self.lock = clock, log, threading.Lock()
        self.proc, self.thread, self.found, self.dumped = None, None, None, float("-inf")
        self.loud, self.strikes, self.skip, self.pending, self.failed = 0, 0, 0, False, False

    def ensure(self):
        if not self.lock.acquire(blocking=False):  # the last tick's pw-dump still out
            return
        try:
            found = nodes(self.run("pw-dump"))
            if not found:  # no Scarlett, or pw-dump unreadable this once: a listener that runs is left alone
                return
            if self.thread and self.thread.is_alive():
                if found["id"] != self.found["id"] and self.proc:
                    self.proc.kill()  # it listens to a sink that is gone: the next tick starts one on the new sink
                return
            self.found, self.dumped = found, self.clock()
            self.thread = threading.Thread(target=self.follow, daemon=True)
            self.thread.start()
        finally:
            self.lock.release()

    def stop(self):
        if self.proc:
            self.proc.kill()

    def follow(self):
        """The listener's thread: every chunk heard until the stream ends."""
        self.loud = self.strikes = self.skip = 0
        self.proc = proc = self.listen(self.found["sink"])
        try:
            while len(raw := proc.stdout.read(CHUNK)) == CHUNK:
                if said := self.hear(raw):
                    self.say(said)
        finally:
            proc.kill()
            proc.wait()

    def refresh(self):
        self.found, self.dumped = nodes(self.run("pw-dump")) or self.found, self.clock()

    def hear(self, raw):
        """CHUNK_S of the sink's monitor → what to say, or None."""
        if self.skip:  # what piled up while the restart ran: older than the meter is now
            self.skip -= 1
            return None
        level = peak(raw)
        if level >= SENT_MIN and self.clock() - self.dumped > GAIN_S:
            self.refresh()  # the volume as it is now
        if level * self.found["gain"] < SENT_MIN:  # too quiet for the meter to show, or a pause: unknown
            self.loud = self.strikes = 0
            return None
        self.loud += 1
        if not self.strikes and (self.loud - SUSTAIN) % EVERY:  # read at SUSTAIN chunks (% keeps the earlier ones
            return None                                          # out), then every EVERY; every one after a strike
        card = self.card()
        meters = parse_meter(self.run("amixer", "-c", card, "cget", METER)) if card else []
        if len(meters) != POINTS:  # unreadable, or another device's layout
            return None
        if any(meters[i] for i in PLAYBACK):  # heard inside the device
            said, self.strikes, self.pending, self.failed = REVIVED if self.pending else None, 0, False, False
            return said
        if not self.strikes and not self.failed:  # the first silent read: the volume as it is right now, first
            self.refresh()
            if level * self.found["gain"] < SENT_MIN:
                self.loud = 0
                return None
        self.strikes += 1
        if self.strikes < STRIKES:
            return None
        self.loud = self.strikes = 0
        if self.pending:  # the restart didn't help
            self.pending, self.failed = False, True
            return GAVE_UP
        if self.failed:
            return None
        self.log(f"Booth: the Scarlett's output was stuck (sent {level * self.found['gain']:.3f}, its meter {meters}); "
                 f"restarting it. Streams running: {', '.join(self.found['apps']) or 'none'}")  # to find what starts it
        began = self.clock()
        self.restart(self.found)
        self.pending, self.skip = True, int((self.clock() - began) / CHUNK_S) + 2
        return None
