"""The routing policy: PRO X off → output to the Scarlett with Direct Monitor on; on → back. Pure rules, then the
executor that reads the facts and runs the actions."""
import json
import threading
import unittest
from unittest import mock

from booth import routing
from tests.fixture import read

PROX = "alsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.iec958-stereo"
SCARLETT = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-stereo"
HDMI = "alsa_output.usb-Generic_USB_Audio-00.iec958-stereo"


def event(on, default=PROX, prox=PROX, scarlett=SCARLETT, direct=False):
    return {"kind": "headset", "on": on, "default": default, "prox": prox, "scarlett": scarlett, "direct": direct}


class World:
    """The desk: the default sink and Direct Monitor, changed only by the policy's actions (or the user, by hand)."""

    def __init__(self, default=PROX, direct=False, scarlett=SCARLETT):
        self.default, self.direct, self.scarlett, self.state, self.notes = default, direct, scarlett, {}, []

    def headset(self, on):
        self.state, actions = routing.next(self.state, event(on, self.default, PROX, self.scarlett,
                                                             None if self.scarlett is None else self.direct))
        for kind, value in actions:
            if kind == "default":
                self.default = value
            elif kind == "direct":
                self.direct = value
            else:
                self.notes.append(value)


class Rules(unittest.TestCase):
    def test_off_moves_output_to_the_scarlett_and_turns_direct_monitor_on(self):
        state, actions = routing.next({}, event(False))
        self.assertEqual(actions, [("default", SCARLETT), ("direct", True),
                                   ("notify", "PRO X off — sound moved to the Scarlett")])
        self.assertEqual(state, {"moved": True, "direct": False})  # Direct Monitor's value before, to put back

    def test_off_with_direct_monitor_already_on_writes_only_the_sink(self):
        state, actions = routing.next({}, event(False, direct=True))
        self.assertEqual(actions, [("default", SCARLETT), ("notify", routing.MOVED)])
        self.assertEqual(state, {"moved": True, "direct": True})

    def test_direct_monitor_is_never_touched_unless_the_rear_outputs_are_free(self):  # #2's answer
        with mock.patch.object(routing, "REAR_OUTPUTS_FREE", False):
            state, actions = routing.next({}, event(False))
            self.assertEqual(actions, [("default", SCARLETT), ("notify", routing.MOVED)])
            self.assertEqual(state, {"moved": True})
            self.assertEqual(routing.next(state, event(True, default=SCARLETT, direct=True)),
                             ({}, [("default", PROX), ("notify", routing.BACK)]))

    def test_on_returns_output_to_the_prox_and_direct_monitor_to_its_previous_value(self):
        state, actions = routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, direct=True))
        self.assertEqual(actions, [("default", PROX), ("direct", False), ("notify", routing.BACK)])
        self.assertEqual(state, {})

    def test_on_leaves_direct_monitor_on_when_it_was_on_before(self):
        self.assertEqual(routing.next({"moved": True, "direct": True}, event(True, default=SCARLETT, direct=True)),
                         ({}, [("default", PROX), ("notify", routing.BACK)]))

    def test_off_without_the_scarlett_switches_nothing_and_says_so(self):
        for absent in (event(False, scarlett=None, direct=None), event(False, scarlett=None, direct=False)):
            self.assertEqual(routing.next({}, absent), ({}, [("notify", routing.NO_SCARLETT)]))
        self.assertIn("Scarlett", routing.NO_SCARLETT)
        self.assertIn("PRO X off", routing.NO_SCARLETT)

    def test_on_after_a_move_that_never_happened_does_nothing(self):  # the Scarlett was absent at the off
        self.assertEqual(routing.next({}, event(True)), ({}, []))

    def test_off_while_another_output_is_the_default_respects_it(self):  # the user picked it by hand
        for default in (HDMI, SCARLETT, None):
            self.assertEqual(routing.next({}, event(False, default=default)), ({}, []), default)

    def test_a_sink_picked_by_hand_while_moved_stands_when_the_prox_comes_back(self):
        state, actions = routing.next({"moved": True, "direct": False}, event(True, default=HDMI, direct=True))
        self.assertEqual(actions, [("direct", False)])  # Booth's own change is still undone
        self.assertEqual(state, {})

    def test_direct_monitor_turned_off_by_hand_while_moved_stays_off(self):
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, direct=False)),
                         ({}, [("default", PROX), ("notify", routing.BACK)]))

    def test_the_prox_sink_gone_at_on_moves_nothing_but_direct_monitor(self):
        # shortcut in the rule: PipeWire may add the dongle's sink after the headset answers; not retried
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, prox=None,
                                                                              direct=True)), ({}, [("direct", False)]))

    def test_on_while_the_scarlett_is_gone_still_returns_to_the_prox(self):  # its name stays the configured default
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, scarlett=None,
                                                                              direct=None)),
                         ({}, [("default", PROX), ("notify", routing.BACK)]))

    def test_a_second_move_keeps_the_first_moves_direct_monitor_value(self):  # a restart, then the PRO X picked by hand
        state, actions = routing.next({"moved": True, "direct": False}, event(False, direct=True))
        self.assertEqual(state, {"moved": True, "direct": False})
        self.assertEqual(actions, [("default", SCARLETT), ("notify", routing.MOVED)])

    def test_next_never_changes_the_state_it_is_given(self):
        state = {"moved": True, "direct": False}
        routing.next(state, event(True, default=SCARLETT, direct=True))
        routing.next(state, event(False))
        self.assertEqual(state, {"moved": True, "direct": False})


class Sequences(unittest.TestCase):
    def test_off_then_on_puts_everything_back(self):
        desk = World()
        desk.headset(False)
        self.assertEqual((desk.default, desk.direct), (SCARLETT, True))
        desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.state), (PROX, False, {}))
        self.assertEqual(desk.notes, [routing.MOVED, routing.BACK])

    def test_rapid_flapping_always_ends_where_the_last_event_says(self):
        desk = World()
        for _ in range(10):
            desk.headset(False)
            desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.state), (PROX, False, {}))
        desk.headset(False)
        self.assertEqual((desk.default, desk.direct, desk.state), (SCARLETT, True, {"moved": True, "direct": False}))

    def test_a_manual_choice_while_on_is_never_reasserted_over(self):
        desk = World()
        desk.headset(True)  # the startup reading: nothing to do
        desk.default = HDMI  # the user picks another output
        desk.headset(False)
        desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.notes), (HDMI, False, []))

    def test_startup_with_the_headset_already_off_and_still_the_default_moves_output(self):
        desk = World()
        desk.headset(False)  # HeadsetMonitor's first reading: None → off
        self.assertEqual((desk.default, desk.direct), (SCARLETT, True))

    def test_startup_with_the_headset_already_off_and_another_default_changes_nothing(self):
        desk = World(default=SCARLETT)
        desk.headset(False)
        desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.notes), (SCARLETT, False, []))

    def test_startup_with_the_headset_on_changes_nothing(self):
        desk = World()
        desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.state, desk.notes), (PROX, False, {}, []))

    def test_a_move_saved_before_a_restart_is_undone_when_the_headset_comes_on(self):
        desk = World(default=SCARLETT, direct=True)
        desk.state = {"moved": True, "direct": False}  # loaded from settings["routing"]
        desk.headset(False)  # the first reading: still off, already on the Scarlett
        self.assertEqual((desk.default, desk.direct, desk.state), (SCARLETT, True, {"moved": True, "direct": False}))
        desk.headset(True)
        self.assertEqual((desk.default, desk.direct, desk.state), (PROX, False, {}))

    def test_flapping_without_the_scarlett_never_switches(self):
        desk = World(scarlett=None)
        for _ in range(3):
            desk.headset(False)
            desk.headset(True)
        self.assertEqual((desk.default, desk.state), (PROX, {}))
        self.assertEqual(desk.notes, [routing.NO_SCARLETT] * 3)


def dump(configured=PROX, effective=PROX):
    """The captured pw-dump (sink ids: PRO X 33, Scarlett 176) with the given default sinks."""
    objects = json.loads(read("headset/pw-dump.json"))
    for o in objects:
        for m in o.get("metadata") or []:
            if m["key"] == "default.configured.audio.sink":
                m["value"] = {"name": configured}
            elif m["key"] == "default.audio.sink":
                m["value"] = {"name": effective}
    return json.dumps(objects)


class Observe(unittest.TestCase):
    def test_the_facts_from_pw_dump_and_the_scarletts_controls(self):
        ctrls = routing.scarlett.parse_contents(read("scarlett/amixer-contents.txt"))
        self.assertEqual(routing.observe(dump(), ctrls),
                         {"default": PROX, "prox": PROX, "scarlett": SCARLETT, "direct": False})

    def test_the_configured_default_wins_over_wireplumbers_fallback(self):  # the dongle unplugged: the PRO X was chosen
        self.assertEqual(routing.observe(dump(PROX, HDMI), {})["default"], PROX)

    def test_with_no_configured_default_the_effective_one_is_used(self):
        objects = json.loads(dump(effective=SCARLETT))
        for o in objects:
            if o.get("metadata"):
                o["metadata"] = [m for m in o["metadata"] if m["key"] != "default.configured.audio.sink"]
        self.assertEqual(routing.observe(json.dumps(objects), {})["default"], SCARLETT)

    def test_nothing_known(self):
        self.assertEqual(routing.observe("", {}), {"default": None, "prox": None, "scarlett": None, "direct": None})


class Fake:
    """devices._run stand-in: the given pw-dump and the Scarlett's captured controls; records the writes."""

    def __init__(self, pw):
        self.pw, self.writes = pw, []

    def __call__(self, *cmd):
        if cmd == ("pw-dump",):
            return self.pw
        if cmd == ("amixer", "-c", "Gen", "contents"):
            return read("scarlett/amixer-contents.txt")
        self.writes.append(list(cmd))
        return ""


class Executor(unittest.TestCase):
    def executor(self, pw, state=None, card="Gen"):
        self.notes, self.saved, self.run = [], [], Fake(pw)
        with mock.patch.object(routing.threading, "Thread"):
            return routing.Executor(state or {}, self.notes.append, self.saved.append, self.run, lambda: card)

    def test_off_sets_the_scarlett_by_its_id_now_and_direct_monitor_on_by_name(self):
        ex = self.executor(dump())
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "176"],
                                           ["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "on"]])
        self.assertEqual(self.notes, [routing.MOVED])
        self.assertEqual(self.saved, [{"moved": True, "direct": False}])
        self.assertEqual(ex.state, {"moved": True, "direct": False})

    def test_on_puts_back_the_prox_and_direct_monitor(self):
        self.executor(dump(SCARLETT, SCARLETT), {"moved": True, "direct": True}).step({"kind": "headset", "on": True})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "33"]])  # Direct Monitor was on before: kept
        self.assertEqual((self.notes, self.saved), ([routing.BACK], [{}]))

    def test_direct_monitor_back_off(self):
        run = Fake("")
        with mock.patch.object(routing.threading, "Thread"):
            ex = routing.Executor({"moved": True, "direct": False}, [].append, [].append, run, lambda: "Gen")
        with mock.patch.object(routing.scarlett, "value", return_value=True):  # on, as Booth left it
            ex.step({"kind": "headset", "on": True})
        self.assertEqual(run.writes, [["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "off"]])

    def test_nothing_to_do_writes_and_saves_nothing(self):
        self.executor(dump(HDMI, HDMI)).step({"kind": "headset", "on": False})
        self.assertEqual((self.run.writes, self.notes, self.saved), ([], [], []))

    def test_the_scarlett_unplugged_is_read_as_absent(self):
        objects = [o for o in json.loads(dump()) if "Focusrite" not in json.dumps(o.get("info") or {})]
        self.executor(json.dumps(objects), card=None).step({"kind": "headset", "on": False})
        self.assertEqual((self.run.writes, self.notes, self.saved), ([], [routing.NO_SCARLETT], []))

    def test_only_on_and_off_reach_the_policy_in_order_on_one_worker(self):
        seen, done = [], threading.Event()
        with mock.patch.object(routing.Executor, "step", lambda self, e: seen.append(e) or len(seen) == 3 and done.set()):
            ex = routing.Executor({}, print, print)
            for name in ("off", "battery", "ack", "on", "off"):
                ex.listener(name)
            self.assertTrue(done.wait(5))
        self.assertEqual([e["on"] for e in seen], [False, True, False])
        self.assertEqual({e["kind"] for e in seen}, {"headset"})

    def test_a_failing_step_never_stops_the_worker(self):
        seen, done = [], threading.Event()

        def step(self, e):
            seen.append(e)
            if len(seen) == 1:
                raise OSError("pw-dump")
            done.set()
        with mock.patch.object(routing.Executor, "step", step), mock.patch.object(routing.traceback, "print_exc"):
            ex = routing.Executor({}, print, print)
            ex.listener("off")
            ex.listener("on")
            self.assertTrue(done.wait(5))


if __name__ == "__main__":
    unittest.main()
