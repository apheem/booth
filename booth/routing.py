"""The routing policy (PRD #1): a pure state machine next(state, event) → (state, actions), and the thin executor the
service runs it with, one event at a time, off the main loop.

Rules (#12, as adjudicated). The PRO X turns off while it is the default sink: output moves to the Scarlett, Direct
Monitor turns on when the user's setting says so (settings["routing"]["direct_monitor"], on when absent; off: the sink
moves alone) and a notification says what happened; with no Scarlett sink nothing switches and the notification says
that. The PRO X turns on after Booth moved output: Direct Monitor goes back to its previous value at once if it is
still on, and output returns to the PRO X if the Scarlett is still the default, once the PRO X sink is in PipeWire: a
sink or a switch the user changed by hand stands.
Only on transitions: the events are HeadsetMonitor's "on" and "off", sent once per change, never on a poll that finds
nothing new, so the policy never re-asserts; the one exception is Executor.retry, which runs the last event again only
while its rule is unsettled, for at most TRIES ticks after the transition (~60 s); the last one has "final": True and
gives up: the PRO X sink still missing drops the move and says so, a write still failing is dropped and said.
Startup: the monitor's first reading is one of those events and goes through the same rules, which look at the desk as
it is: off with the PRO X still the default sink moves output (sound would go nowhere), off with another default changes
nothing, and on returns output only when a move is saved (Booth moved it before it restarted).

state: the record in settings["routing"], {} or {"moved": True, "direct": Direct Monitor's value before the move,
  absent when Booth didn't own it}, or {"direct": False} once output is back but the Scarlett is gone, until Direct
  Monitor can be put back. A move's record lives for one move cycle, honoured at the first "on". The {"direct": False}
  one is never dropped while Direct Monitor can't be read (no cap, kept through later "off"s and "on"s): it is resolved
  the first time it can be, by the 10 s tick's "direct" event or an "off" (put back first, then the off rule), and then
  it is gone, so a Direct Monitor turned on by hand later stands. next() never changes the dict it is given.
event: {"kind": "headset", "on": bool, "final": bool when the last retry} or {"kind": "direct", "final": True} (the tick
  while Direct Monitor is owed and the Scarlett's card is there) | observe(): the facts read just before it, with
  "auto": the user's Direct Monitor setting.
actions: ("default", sink node name), ("direct", bool), ("notify", text), in the order to run.
A new event is a new kind with its function in RULES and its facts in observe(); the headset's rule doesn't change.
#20's "mic" rule decides only WHEN a Booth Mic source change is said (notify + sidebar label): voice.choose picks the
source and voice.apply writes it, run by the app after each heal, not by the Executor (nothing to write here)."""
import json
import queue
import sys
import threading
import traceback

from booth import devices, scarlett, service, voice

SETTING = "direct_monitor"  # settings["routing"][SETTING]: the user's "Turn on Direct Monitor…" switch, never the record
SINK = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_"  # the Scarlett's PipeWire sink: then serial and profile
MOVED = "PRO X off — sound moved to the Scarlett"
DIRECT_ON = ", Direct Monitor on"  # added only when Booth turned it on: the executor drops it when it didn't
NO_SCARLETT = "PRO X off — the Scarlett isn't connected, so sound wasn't moved"
BACK = "PRO X on — sound is back on the headset"
NO_PROX = "PRO X back, but its output isn't available — sound stays on the Scarlett"
GAVE_UP = "Sound routing gave up: {} kept failing"
TRIES = 6  # retries after a transition, one per 10 s tick: then the last event gives up


def _owed(state):
    """Output is back but Direct Monitor isn't: Booth turned it on and the Scarlett was gone to put it back."""
    return state.get("direct") is False and not state.get("moved")


def _headset(state, e):
    default, final = e["default"] or "", e.get("final", False)
    if not e["on"]:
        if _owed(state) and e["direct"] is not None:  # put back first: Booth's own is never taken as the user's value
            new, actions = _headset({}, e | {"direct": False})
            return new, ([("direct", False)] if e["direct"] else []) + actions
        kept = state if state.get("moved") or _owed(state) else {}  # this cycle's record, or one still owed
        if devices.PRO_X_USB not in default:  # not playing on the headset: whatever the user chose stands
            return kept, []
        if e["scarlett"] is None:
            return kept, [("notify", NO_SCARLETT)]
        # a second move keeps the value from before the first, even when it can't be read now
        new, actions, said = kept | {"moved": True}, [("default", e["scarlett"])], MOVED
        if e["auto"] and e["direct"] is not None:
            new.setdefault("direct", e["direct"])
            if not e["direct"]:
                actions.append(("direct", True))
                said += DIRECT_ON
        return new, actions + [("notify", said)]
    new, actions = {}, []
    if state.get("direct") is False:  # put back at once, never waiting for the PRO X sink
        if e["direct"]:  # still on, as Booth left it
            actions.append(("direct", False))
        elif e["direct"] is None:  # the Scarlett gone: kept until it is back, past the cap (the tick checks)
            new["direct"] = False
    # still where Booth put it (the Scarlett's name stays configured when it is unplugged)
    if state.get("moved") and default.startswith(SINK):
        if e["prox"]:
            return new, [("default", e["prox"])] + actions + [("notify", BACK)]
        if not final:  # the dongle back before WirePlumber adds its sink: the executor retries the sink alone
            return new | {"moved": True}, actions
        return new, actions + [("notify", NO_PROX)]  # never written later: sound stays on the Scarlett
    return new, actions


def _direct(state, e):
    """The tick while Direct Monitor is owed and the Scarlett is back: put it back if still on, then the record is gone."""
    if not _owed(state) or e["direct"] is None:
        return state, []
    return {}, [("direct", False)] if e["direct"] else []


DOWN = "down"  # Booth Mic's input isn't in PipeWire: its chain is stopped or restarting
MIC_SAID = {voice.SCARLETT: "Booth Mic is back on the Scarlett",
            voice.PRO_X_MIC: "Scarlett not connected — Booth Mic now uses the PRO X mic",
            voice.BRIO_MIC: "Scarlett not connected — Booth Mic now uses the BRIO mic",
            None: "Booth Mic has no mic — connect the Scarlett or turn on the PRO X",
            voice.NO_INPUT: "Booth Mic has no input: the Scarlett's input isn't in PipeWire",
            DOWN: "Booth Mic isn't running — a call may have switched to another mic"}
LABELS = {voice.SCARLETT: "from the Scarlett", voice.PRO_X_MIC: "from PRO X mic", voice.BRIO_MIC: "from BRIO mic",
          None: "no mic", voice.NO_INPUT: "no input", DOWN: "not running"}


def label(source):
    """The sidebar's line for Booth Mic's source."""
    return f"Booth Mic · {LABELS[source]}"


def _mic(state, e):
    """#20: the mic voice.heal chose (or DOWN), after each heal. A move to a backup or to no mic is said on the heal
    that made it: never a mic the user wasn't told of. Only the return to the Scarlett (and DOWN, Booth Mic's restart
    gap) waits for a second heal in a row, so a flap Scarlett → BRIO → Scarlett is said once each way, never zero times,
    never a stream. state: {"said": the last source said, the Scarlett at start, "seen": a return seen once}, in memory
    only (a restart on a backup says it again)."""
    said, source = state.get("said", voice.SCARLETT), e["source"]
    if source == said:
        return {"said": said}, []
    if source in (voice.SCARLETT, DOWN) and state.get("seen") != source:
        return {"said": said, "seen": source}, []
    return {"said": source}, [("notify", MIC_SAID[source]), ("label", source)]


RULES = {"headset": _headset, "direct": _direct, "mic": _mic}


def next(state, event):
    """The policy: the saved state and an event with its facts → (the new state, the actions to run)."""
    return RULES[event["kind"]](state, event)


def _configured(dump):
    """`pw-dump` JSON → the default sink the user (or Booth) chose, default.configured.audio.sink, or None."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None
    for o in objects if isinstance(objects, list) else []:
        if (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if m.get("key") == "default.configured.audio.sink" and isinstance(m.get("value"), dict):
                    return m["value"].get("name")
    return None


def observe(dump, ctrls):
    """`pw-dump` JSON and the Scarlett's parsed controls ({} when unplugged) → the facts next() reads. The configured
    default, not WirePlumber's fallback: with the dongle unplugged the PRO X is still what the user chose."""
    sinks, effective = devices.parse_sinks(dump)
    return {"default": _configured(dump) or effective, "prox": devices.find_sink(sinks),
            "scarlett": min((n for n in sinks if (n or "").startswith(SINK)), default=None),
            "direct": scarlett.value(ctrls, scarlett.DIRECT)}


class Executor:
    """Runs the policy on one worker thread, events in the order they came (off → on → off fast runs as three steps,
    each on facts read just before it). `listener` goes in HeadsetMonitor.listeners, `retry` on a GLib timer.
    `settings` is the app's shared dict: the record is taken from settings["routing"] once, the user's SETTING read there
    at each step. `notify(text)` and `save(state)` are called on the worker: the app passes ones that hop to the main
    loop; save waits there and → True when the record reached the disk (Direct Monitor is turned on only after).
    `run` and `card` are devices._run and devices.scarlett_card (the card, sink ids and controls are resolved fresh on
    every step); `write(cmd)` → True when the command succeeded, service.run: only what happened is said and saved."""

    def __init__(self, settings, notify, save, run=devices._run, card=devices.scarlett_card, write=service.run):
        self.settings, self.notify, self.save, self.run, self.card, self.write = settings, notify, save, run, card, write
        self.state = {k: v for k, v in settings.get("routing", {}).items() if k != SETTING}
        self.events, self.last, self.pending, self.tries = queue.Queue(), None, False, 0
        threading.Thread(target=self._work, daemon=True).start()

    def listener(self, event):
        if event in ("on", "off"):  # never "battery" or "ack": no re-asserting on a poll
            self.last, self.tries = {"kind": "headset", "on": event == "on"}, 0
            self.events.put(self.last)

    def retry(self):
        """A GLib timeout callback: the last event again, only while its rule is unsettled (pw-dump unknown, a failed
        write, the PRO X sink not back yet, Direct Monitor still to put back), so nothing settled is re-asserted; at
        most TRIES times after a transition, the last one final."""
        if self.pending:
            self.pending, self.tries = False, self.tries + 1
            self.events.put(self.last | {"final": True} if self.tries >= TRIES else self.last)
        elif _owed(self.state) and self.card():  # no cap: once the Scarlett is back; a failed write gives up at once
            self.events.put({"kind": "direct", "final": True})
        return True

    def _work(self):
        while True:
            event = self.events.get()
            try:
                self.step(event)
            except Exception:  # one failed step (a tool missing, a parse error) must not stop the next events
                traceback.print_exc(file=sys.stderr)

    def step(self, event):
        card = self.card()
        ctrls = scarlett.parse_contents(self.run("amixer", "-c", card, "contents")) if card else {}
        dump = self.run("pw-dump")
        sinks, _ = devices.parse_sinks(dump)
        final, headset = event.get("final", False), event["kind"] == "headset"  # a put-back tick leaves the retries be
        if headset:
            self.pending = not final  # the last retry gives up, settled or not
        if not sinks:  # pw-dump failed or PipeWire not up yet (login): unknown, not "not the PRO X"
            return
        before, quiet, direct_on = self.state, False, False
        auto = self.settings.get("routing", {}).get(SETTING, True)  # the switch as it is now
        state, actions = next(before, event | observe(dump, ctrls) | {"auto": auto})
        for kind, value in actions:
            if kind == "default":  # by its id in this dump: ids change on replug
                if not self.write(["wpctl", "set-default", str(sinks[value][0])]):
                    if final:
                        self.notify(GAVE_UP.format("moving the sound"))
                    if not event["on"]:
                        return  # nothing moved: Direct Monitor never turned on alone, nothing said or saved
                    state, quiet = state if final else state | {"moved": True}, True  # the move stays, to retry
            elif kind == "direct":  # DIRECT only; cset_cmd never writes 48V without the user's dialog
                if value:  # write-ahead: the record is on disk before Direct Monitor is on, or it isn't turned on
                    self.state = state
                    if not self.save(state):
                        continue
                if self.write(scarlett.cset_cmd(card, scarlett.DIRECT, value)):  # card: read this step
                    direct_on = value
                elif final:
                    self.notify(GAVE_UP.format("switching Direct Monitor"))
                else:
                    state = {"direct": before.get("direct")} | state  # not put back: still Booth's to put back
            elif not quiet:
                self.notify(value if direct_on else value.removesuffix(DIRECT_ON))  # only what happened
        if headset:  # after the final retry only Direct Monitor can be left owed: the tick has it
            self.pending = event["on"] and bool(state) and not final
        if state != self.state:
            self.state = state
            self.save(state)
