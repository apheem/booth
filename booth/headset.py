"""The PRO X Wireless headset: the dongle's hardware-volume fix, and its volume and default output through wpctl.

The dongle's ALSA `PCM Playback Volume` can sit below full after a replug and the IEC958 profile never raises it, so
100 % is too quiet: Booth sets it to the control's maximum (0 dB) at start and whenever the dongle appears. The volume
slider is wpctl's volume of the PRO X sink, never above 1.0 (100 %). The sink's id is resolved from pw-dump right
before every command: PipeWire reuses ids after a replug."""
import re

from booth import devices

PCM = "PCM Playback Volume"
_NAME = re.compile(r"name='(.*)'")
_INT = re.compile(r"^\s*; type=INTEGER,.*\bmin=(-?\d+),max=(-?\d+)")
_VALUES = re.compile(r"^\s*: values=(-?\d+(?:,-?\d+)*)\s*$")
_VOLUME = re.compile(r"^Volume: (\d+(?:\.\d+)?)")


def parse_amixer(text):
    """`amixer cget` or `amixer contents` output → {control name: {"min", "max", "values"}} of its integer controls."""
    ctrls, name = {}, None
    for line in text.splitlines():
        if line.startswith("numid="):
            m = _NAME.search(line)
            name = m.group(1) if m else None
        elif name and (m := _INT.match(line)):
            ctrls[name] = {"min": int(m.group(1)), "max": int(m.group(2))}
        elif name in ctrls and (m := _VALUES.match(line)):
            ctrls[name]["values"] = [int(v) for v in m.group(1).split(",")]
    return {n: c for n, c in ctrls.items() if "values" in c}


def fix_cmd(card, ctrls):
    """The amixer write that sets the dongle's PCM Playback Volume to the control's own maximum (0 dB on the PRO X), or
    None when every channel is already there or the card has no such control."""
    pcm = ctrls.get(PCM)
    if pcm is None or all(v == pcm["max"] for v in pcm["values"]):
        return None
    return ["amixer", "-c", str(card), "cset", f"name={PCM}", str(pcm["max"])]


def fix_hw_volume(run=devices._run, card=devices.headset_card):
    """Read the dongle's hardware volume and set it to full when it isn't (nothing when unplugged) → the write or None."""
    n = card()
    if n is None:
        return None
    cmd = fix_cmd(n, parse_amixer(run("amixer", "-c", str(n), "cget", f"name={PCM}")))
    if cmd:
        run(*cmd)
    return cmd


def parse_volume(text):
    """`wpctl get-volume` output ("Volume: 0.63", maybe with " [MUTED]") → 0.63, or None."""
    m = _VOLUME.match(text.strip())
    return float(m.group(1)) if m else None


def percent(volume):
    """wpctl volume → the slider's position, 0..100 (a boost above 100 % set elsewhere shows as 100)."""
    return round(min(max(volume, 0.0), 1.0) * 100)


def volume_cmd(dump, percent):
    """`pw-dump` JSON and the slider's 0..100 → the wpctl command that sets the PRO X sink, or None when it is gone.
    Clamped to 1.0, and with wpctl's own limit as well: Booth never sets more than 100 %."""
    sinks, _ = devices.parse_sinks(dump)
    name = devices.find_sink(sinks)
    if name is None:
        return None
    return ["wpctl", "set-volume", "-l", "1.0", str(sinks[name][0]), f"{min(max(percent, 0), 100) / 100:.2f}"]


def default_cmd(dump, on, previous=None):
    """"Use for all sound" switched `on` → (make the PRO X the default sink, the default sink it replaces to remember by
    node name, or None when that is the PRO X itself). Off → (set the default back to `previous`, resolved now by its
    name; when it is gone or unknown, clear WirePlumber's configured default sink, setting 0 = Audio/Sink, so it picks
    one by its own rules, None). Never a sink picked by priority here: that would be saved as the configured default.
    (None, None) when the PRO X sink is gone."""
    sinks, default = devices.parse_sinks(dump)
    name = devices.find_sink(sinks)
    if name is None:
        return None, None
    if on:
        return ["wpctl", "set-default", str(sinks[name][0])], (default if default != name else None)
    if previous in sinks:
        return ["wpctl", "set-default", str(sinks[previous][0])], None
    return ["wpctl", "clear-default", "0"], None


def state(run=devices._run):
    """The PRO X sink now → {"volume": wpctl volume, "default": whether it is the default sink}, or None when gone."""
    sinks, default = devices.parse_sinks(run("pw-dump"))
    name = devices.find_sink(sinks)
    if name is None:
        return None
    volume = parse_volume(run("wpctl", "get-volume", str(sinks[name][0])))
    return None if volume is None else {"volume": volume, "default": name == default}


def status(state):
    """The Headphones sidebar subtitle, e.g. "Volume 63 %"."""
    return "Not connected" if state is None else f"Volume {round(state['volume'] * 100)} %"


def send(make, run=devices._run):
    """Resolve the sinks now and run make(pw-dump JSON)'s command, if any."""
    cmd = make(run("pw-dump"))
    if cmd:
        run(*cmd)


class Writer:
    """The Headphones page's writes, one at a time, off the main loop. While one runs, a newer ask of the same kind
    replaces the queued one (a slider drag sends the last value asked for); kinds never drop each other.
    `spawn(make, done)` runs send(make) elsewhere and calls done() back on the main loop. `writes` counts the asks
    and the finished writes, so a poll begun before one, or while one is busy, is not shown (it may have read the old
    value)."""

    def __init__(self, spawn):
        self.spawn, self.pending, self.busy, self.writes = spawn, {}, False, 0

    def want(self, kind, make):
        self.writes += 1
        self.pending[kind] = make
        if not self.busy:
            self._next()

    def _done(self):
        self.writes += 1  # a poll begun while it ran may have read before it landed
        self._next()

    def _next(self):
        self.busy = bool(self.pending)
        if self.busy:
            self.spawn(self.pending.popitem()[1], self._done)

    def fresh(self, since):
        """True when a poll begun at `writes == since` may be shown."""
        return not self.busy and self.writes == since
