"""The routing policy (PRD #1): a pure state machine next(state, event) → (state, actions), and the thin executor the
service runs it with, one event at a time, off the main loop.

Rules (#12). The PRO X turns off while it is the default sink: output moves to the Scarlett, Direct Monitor turns on
and a notification says so; with no Scarlett sink nothing switches and the notification says that. The PRO X turns on
after Booth moved output: output returns to the PRO X if the Scarlett is still the default, and Direct Monitor to its
previous value if it is still on: a sink or a switch the user changed by hand stands.
Only on transitions: the events are HeadsetMonitor's "on" and "off", sent once per change, never on a poll that finds
nothing new, so the policy never re-asserts; the one exception is Executor.retry, which runs the last event again only
while its rule is unsettled (at "on": the PRO X sink not in PipeWire yet keeps the move untouched). Startup: the
monitor's first reading is one of those events and goes through the same rules, which look at the desk as it is: off
with the PRO X still the default sink moves output (sound would go nowhere), off with another default changes nothing,
and on returns output only when a move is saved (Booth moved it before it restarted).

state: settings["routing"], {} or {"moved": True, "direct": Direct Monitor's value before the move, absent when Booth
  didn't own it (no Scarlett card, or the rear outputs not free)}, or {"direct": False} once output is back but the
  Scarlett is gone, until Direct Monitor can be put back. next() never changes the dict it is given.
event: {"kind": "headset", "on": bool} | observe(): the facts read just before it.
actions: ("default", sink node name), ("direct", bool), ("notify", text), in the order to run.
A new event (#20: the Scarlett or a mic gone or back) is a new kind with its function in RULES and its facts in
observe(); the headset's rule doesn't change."""
import json
import queue
import sys
import threading
import traceback

from booth import devices, scarlett, service

REAR_OUTPUTS_FREE = True  # #2: the user confirmed nothing is plugged into the rear line outputs; False: never touch Direct
SINK = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_"  # the Scarlett's PipeWire sink: then serial and profile
MOVED = "PRO X off — sound moved to the Scarlett"
NO_SCARLETT = "PRO X off — the Scarlett isn't connected, so sound wasn't moved"
BACK = "PRO X on — sound is back on the headset"


def _headset(state, e):
    default = e["default"] or ""
    if not e["on"]:
        if devices.PRO_X_USB not in default:  # not playing on the headset: whatever the user chose stands
            return state, []
        if e["scarlett"] is None:
            return state, [("notify", NO_SCARLETT)]
        new, actions = {"moved": True}, [("default", e["scarlett"])]
        if "direct" in state:  # a second move keeps the value from before the first, even when it can't be read now
            new["direct"] = state["direct"]
        if REAR_OUTPUTS_FREE and e["direct"] is not None:
            new.setdefault("direct", e["direct"])
            if not e["direct"]:
                actions.append(("direct", True))
        return new, actions + [("notify", MOVED)]
    if state.get("moved") and not e["prox"]:  # the dongle back before WirePlumber adds its sink: the executor retries
        return state, []
    # still where Booth put it (the Scarlett's name stays configured when it is unplugged)
    back = state.get("moved") and default.startswith(SINK)
    actions, new = [("default", e["prox"])] if back else [], {}
    if state.get("direct") is False:
        if e["direct"] is None:  # the Scarlett gone: kept until Direct Monitor can be put back
            new = {"direct": False}
        elif e["direct"]:  # still on, as Booth left it
            actions.append(("direct", False))
    return new, actions + ([("notify", BACK)] if back else [])


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
    `notify(text)` and `save(state)` are called on the worker: the app passes ones that hop to the main loop. `run` and
    `card` are devices._run and devices.scarlett_card (the card, sink ids and controls are resolved fresh on every
    step); `write(cmd)` → True when the command succeeded, service.run: only what happened is said and saved."""

    def __init__(self, state, notify, save, run=devices._run, card=devices.scarlett_card, write=service.run):
        self.state, self.notify, self.save, self.run, self.card, self.write = state, notify, save, run, card, write
        self.events, self.last, self.pending = queue.Queue(), None, False
        threading.Thread(target=self._work, daemon=True).start()

    def listener(self, event):
        if event in ("on", "off"):  # never "battery" or "ack": no re-asserting on a poll
            self.last = {"kind": "headset", "on": event == "on"}
            self.events.put(self.last)

    def retry(self):
        """A GLib timeout callback: the last event again, only while its rule is unsettled (pw-dump unknown, a failed
        write, the PRO X sink not back yet, Direct Monitor still to put back), so nothing settled is re-asserted."""
        if self.pending:
            self.pending = False
            self.events.put(self.last)
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
        self.pending = True
        if not sinks:  # pw-dump failed or PipeWire not up yet (login): unknown, not "not the PRO X"
            return
        state, actions = next(self.state, event | observe(dump, ctrls))
        for kind, value in actions:
            if kind == "default":  # by its id in this dump: ids change on replug
                if not self.write(["wpctl", "set-default", str(sinks[value][0])]):
                    return  # nothing moved: nothing said or saved
            elif kind == "direct":  # DIRECT only; cset_cmd never writes 48V without the user's dialog
                if not self.write(scarlett.cset_cmd(card, scarlett.DIRECT, value)):  # card: read this step
                    state = {"direct": self.state.get("direct")} | state  # not put back: still Booth's to put back
            else:
                self.notify(value)
        self.pending = event["on"] and bool(state)
        if state != self.state:
            self.state = state
            self.save(state)
