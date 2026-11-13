"""The PRO X Wireless headset: the dongle's hardware-volume fix, and its volume and default output through wpctl.

The dongle's ALSA `PCM Playback Volume` can sit below full after a replug and the IEC958 profile never raises it, so
100 % is too quiet: Booth sets it to the control's maximum (0 dB) at start and whenever the dongle appears. The volume
slider is wpctl's volume of the PRO X sink, never above 1.0 (100 %). The sink's id is resolved from pw-dump right
before every command: PipeWire reuses ids after a replug.

HeadsetMonitor follows the battery (booth.hidpp) for the service's 10 s poll: on/off, a smoothed estimate, and one
low-battery warning per discharge. It also sends sidetone and auto power-off on the same one-exchange-at-a-time
guard, and re-sends the saved ones (only values the user chose in Booth) on every "on"."""
import functools
import json
import re
import sys
import traceback

from booth import devices, hidpp, routing, scarlett

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
    card_id = card()
    if card_id is None:
        return None
    cmd = fix_cmd(card_id, parse_amixer(run("amixer", "-c", card_id, "cget", f"name={PCM}")))
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


def volume_cmd(dump, percent, name=None):
    """`pw-dump` JSON and the slider's 0..100 → the wpctl command that sets the PRO X sink, or None when it is gone.
    Clamped to 1.0, and with wpctl's own limit as well: Booth never sets more than 100 %. `name`: the backup slider's
    sink (state()'s output) instead, set only while it is still the default: never an output sound has left."""
    sinks, default = devices.parse_sinks(dump)
    if name is None:
        name = devices.find_sink(sinks)
    elif name != default:
        return None
    if name not in sinks:
        return None
    return ["wpctl", "set-volume", "-l", "1.0", str(sinks[name][0]), f"{min(max(percent, 0), 100) / 100:.2f}"]


def default_cmd(dump, on, previous=None):
    """"Use for all sound" switched `on` → (make the PRO X the default sink, the default sink it replaces to remember by
    node name, or None when that is the PRO X itself). Off → (set the default back to `previous`, resolved now by its
    name; when it is gone or unknown, clear WirePlumber's configured default sink, setting 0 = Audio/Sink, so it picks
    one by its own rules, None); nothing when the default is no longer the PRO X (the user picked another one). Never a sink picked by priority here: that would be saved as the configured default.
    (None, None) when the PRO X sink is gone."""
    sinks, default = devices.parse_sinks(dump)
    name = devices.find_sink(sinks)
    if name is None:
        return None, None
    if on:
        return ["wpctl", "set-default", str(sinks[name][0])], (default if default != name else None)
    if default != name:
        return None, None
    if previous in sinks:
        return ["wpctl", "set-default", str(sinks[previous][0])], None
    return ["wpctl", "clear-default", "0"], None


def state(run=devices._run, card=devices.scarlett_card):
    """The PRO X sink now → {"volume": wpctl volume, "default": whether it is the default sink, "output": the default
    sink when it is another one (the backup Booth moved sound to, or the user's pick), else None}, or None when the
    PRO X sink is gone. output: {"name", "label": its description, "volume": wpctl's, "direct": the Scarlett's Direct
    Monitor when it is the Scarlett's sink and its card is there, else None}."""
    dump = run("pw-dump")
    sinks, default = devices.parse_sinks(dump)
    name = devices.find_sink(sinks)
    if name is None:
        return None
    volume = parse_volume(run("wpctl", "get-volume", str(sinks[name][0])))
    if volume is None:
        return None
    output = None
    if default in sinks and default != name:
        heard = parse_volume(run("wpctl", "get-volume", str(sinks[default][0])))
        if heard is not None:
            props = next((o["info"]["props"] for o in json.loads(dump) if o.get("id") == sinks[default][0]), {})
            card_id = card() if default.startswith(routing.SINK) else None
            ctrls = scarlett.parse_contents(run("amixer", "-c", card_id, "contents")) if card_id else {}
            output = {"name": default, "label": props.get("node.description") or default, "volume": heard,
                      "direct": scarlett.value(ctrls, scarlett.DIRECT)}
    return {"volume": volume, "default": name == default, "output": output}


OFF_NOTE = "The headset is off: its sidetone and auto power-off come back when it turns on"


def playing(state, on):
    """The Sound Output card's line: where sound plays now. `on`: HeadsetMonitor.on."""
    if state is None:
        return "Unknown"
    if state["default"]:
        return "PRO X Wireless" + (" — it is off, so nothing is heard" if on is False else "")
    if state["output"] is None:
        return "No sound output"
    return state["output"]["label"] + (" — the backup while the PRO X is off" if on is False else "")


def note(state, on):
    """The PRO X card's description: why its rows are greyed out or hidden, or None."""
    if state is None:
        return "Not connected"
    return OFF_NOTE if on is False else None


def status(state, monitor=None):
    """The Headphones sidebar subtitle, e.g. "~70 % · Volume 63 %"; "Not connected" when the dongle's sink is gone.
    Off with sound on another output: that one's volume, the one heard."""
    if state is None:
        return "Not connected"
    volume = f"Volume {round(state['volume'] * 100)} %"
    if monitor is None or monitor.on is None:
        return volume
    if monitor.on:
        return f"~{monitor.percent} % · {volume}"
    output = state.get("output")
    return f"Off · Backup volume {round(output['volume'] * 100)} %" if output else f"Off · {volume}"


def card(monitor):
    """The Headphones status card → (title, battery line or None)."""
    if monitor.on is None:  # before the first answer
        return "PRO X Wireless", None
    if not monitor.on:
        return "PRO X Wireless — Off", None
    return "PRO X Wireless — Connected", f"~{monitor.percent} % (est.)" + (", charging" if monitor.charging else "")


MISSES = 2  # no answer this many polls in a row (~20 s) is off; orchestrator adjudication on #9
SMOOTHING = 0.3  # each 10 s reading moves the voltage this far: a load spike barely shows, a real drop in ~a minute
WARN_AT, REARM_ABOVE = 15, 30
REQUESTS = {"sidetone": hidpp.sidetone_request, "auto_off": hidpp.auto_off_request}


def warn(armed, percent, charging):
    """The low-battery rule → (armed, warn now): once at or below WARN_AT %, then not again until above REARM_ABOVE %."""
    if armed and percent <= WARN_AT and not charging:
        return False, True
    return armed or percent > REARM_ABOVE, False


class HeadsetMonitor:
    """The PRO X's on/off and battery, from the service's poll (every primary instance, window or not).

    For subscribers (the Headphones page; the routing policy, #12): append a callable to `listeners`; it is called on
    the main loop with "on", "off", "battery" (level or charging changed) or "ack" (a setting's reply came back)
    after the state below is updated:
      on        True, False (the off reply, the dongle unplugged, or MISSES polls unanswered), or None before
                the first answer
      percent   the smoothed estimate, 0..100, or None when off
      charging  True while charging
      refused   the kinds ("sidetone", "auto_off") whose value the user sent got no acknowledgement on two tries, each
                with a poll answering on before and after it (off, an unplug or a miss only keeps it for the next
                "on"); kept until Booth restarts (a re-send on "on" never adds one); "ack" is emitted when one is added
    `notify(percent)` is called once per discharge at <= WARN_AT %. `spawn(work, done)` runs work() off the main loop
    and done(its result) back on it. `settings`: the app's shared settings dict, whose "headphones" sidetone and
    auto_off, when saved, are re-sent on every "on" (never a value the user didn't choose); None sends nothing."""

    def __init__(self, notify=lambda percent: None, spawn=None, settings=None):
        self.notify, self.spawn, self.listeners, self.settings = notify, spawn, [], settings
        self.on, self.percent, self.charging, self.mv, self.armed, self.busy = None, None, False, None, True, False
        self.misses, self.queue, self.refused = 0, {}, set()  # queue: {kind: (value, the user's ask now)}
        # a user's value with no ack: {kind: (value, on before its ask)}, judged by the next poll; struck: one strike
        self.held, self.struck = {}, set()

    def poll(self):
        """A GLib timeout callback: ask the headset, unless an exchange (a poll's or a setting's) hasn't answered."""
        if not self.busy:
            self.busy = True
            self.spawn(hidpp.battery, self._done)
        return True

    def _done(self, reading):
        self.busy = False
        on = reading is not None and reading != hidpp.OFF
        for kind, (value, sure) in list(self.held.items()):
            if not on:  # off (the error reply, an unplug) or a miss: no verdict; it goes again once a poll says on
                self.held[kind] = value, False
                continue
            del self.held[kind]
            if sure and kind in self.struck:  # on before and after the ask, twice: this headset doesn't take it
                self.refused.add(kind)
                self._emit(["ack"])
                continue
            if sure:
                self.struck.add(kind)
            self.queue.setdefault(kind, (value, True))  # before update(): an "on" re-send must not take it over
        self.update(reading)
        self._next()

    def set(self, kind, value):
        """Send the headset "sidetone" (0..128) or "auto_off" (minutes) once no exchange is in flight: never two
        threads on the node. A newer value replaces a queued one of its kind; kinds never drop each other."""
        self.queue[kind] = value, True
        self.held.pop(kind, None)
        self.struck.discard(kind)  # a new value gets its own two tries
        self._next()

    def _next(self):
        if not self.busy and self.queue and self.on is not False:  # off: kept for the next "on", which re-sends
            self.busy = True
            kind, (value, user) = self.queue.popitem()
            sure = self.on is True and not self.misses  # the last poll answered on
            self.spawn(functools.partial(hidpp.setting, REQUESTS[kind](value)),
                       lambda ok: self._acked(kind, value, user, sure, ok))

    def _acked(self, kind, value, user, sure, ok):
        self.busy = False
        # the error reply, an unplug, a dropped reply: the next poll decides (unless a newer ask of it is queued)
        if user and ok is not True and kind not in self.queue:
            self.held[kind] = value, sure
        self._emit(["ack"])
        self._next()

    def update(self, reading):
        """A battery reading → the new state and its events: (mV, charging); hidpp.OFF, off at once; None for no
        answer, a miss: off only after MISSES in a row, and a single one changes nothing (smoothing included)."""
        events = []
        if reading is None:
            self.misses += 1
            if self.misses < MISSES:  # one dropped reply on a marginal radio link must not flap off → on
                return
        else:
            self.misses = 0
        if reading is None or reading == hidpp.OFF:
            if self.on is not False:
                self.on, self.percent, self.mv = False, None, None
                events.append("off")
        else:
            mv, charging = reading
            fresh = not self.on or charging != self.charging  # charging moves the voltage for real: no lag behind it
            self.mv = mv if fresh else self.mv + SMOOTHING * (mv - self.mv)
            percent = hidpp.estimate(round(self.mv))
            if not self.on:
                events.append("on")
                saved = self.settings["headphones"] if self.settings is not None else {}
                for kind in REQUESTS:  # they may reset at power-on; a user's ask still queued goes as theirs
                    if kind in saved:
                        self.queue.setdefault(kind, (saved[kind], False))
            if fresh or percent != self.percent:
                events.append("battery")
            self.on, self.percent, self.charging = True, percent, charging
        self._emit(events)
        if self.on:
            self.armed, now = warn(self.armed, self.percent, self.charging)
            if now:
                self.notify(self.percent)

    def _emit(self, events):
        for event in events:
            for listener in list(self.listeners):
                try:
                    listener(event)
                except Exception:  # one broken subscriber must not stop the others or the warning
                    traceback.print_exc(file=sys.stderr)


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
