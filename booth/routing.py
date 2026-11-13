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
  Monitor can be put back. A record lives for one move cycle: it is honoured at the first "on" (or the Scarlett back
  within the cap) and dropped at the cap or the next "off". next() never changes the dict it is given.
event: {"kind": "headset", "on": bool, "final": bool when the last retry} | observe(): the facts read just before it,
  with "auto": the user's Direct Monitor setting.
actions: ("default", sink node name), ("direct", bool), ("notify", text), in the order to run.
A new event (#20: the Scarlett or a mic gone or back) is a new kind with its function in RULES and its facts in
observe(); the headset's rule doesn't change."""
import json
import queue
import sys
import threading
import traceback

from booth import devices, scarlett, service

SETTING = "direct_monitor"  # settings["routing"][SETTING]: the user's "Turn on Direct Monitor…" switch, never the record
SINK = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_"  # the Scarlett's PipeWire sink: then serial and profile
MOVED = "PRO X off — sound moved to the Scarlett"
DIRECT_ON = ", Direct Monitor on"  # added only when Booth turned it on: the executor drops it when it didn't
NO_SCARLETT = "PRO X off — the Scarlett isn't connected, so sound wasn't moved"
BACK = "PRO X on — sound is back on the headset"
NO_PROX = "PRO X back, but its output isn't available — sound stays on the Scarlett"
GAVE_UP = "Sound routing gave up: {} kept failing"
TRIES = 6  # retries after a transition, one per 10 s tick: then the last event gives up


def _headset(state, e):
    default, final = e["default"] or "", e.get("final", False)
    if not e["on"]:
        kept = state if state.get("moved") else {}  # this move cycle's record (a restart while moved), never an older one
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
        elif e["direct"] is None and not final:  # the Scarlett gone: kept until it is back, within the cap
            new["direct"] = False
    # still where Booth put it (the Scarlett's name stays configured when it is unplugged)
    if state.get("moved") and default.startswith(SINK):
        if e["prox"]:
            return new, [("default", e["prox"])] + actions + [("notify", BACK)]
        if not final:  # the dongle back before WirePlumber adds its sink: the executor retries the sink alone
            return new | {"moved": True}, actions
        return new, actions + [("notify", NO_PROX)]  # never written later: sound stays on the Scarlett
    return new, actions


RULES = {"headset": _headset}


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
        final = event.get("final", False)
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
        self.pending = event["on"] and bool(state)  # at the final retry the rules keep nothing
        if state != self.state:
            self.state = state
            self.save(state)
