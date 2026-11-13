"""The routing policy: PRO X off → output to the Scarlett with Direct Monitor on; on → back. Pure rules, then the
executor that reads the facts and runs the actions."""
import json
import threading
import unittest
from unittest import mock

from booth import routing, voice
from tests.fixture import read

PROX = "alsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.iec958-stereo"
SCARLETT = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-stereo"
HDMI = "alsa_output.usb-Generic_USB_Audio-00.iec958-stereo"


def event(on, default=PROX, prox=PROX, scarlett=SCARLETT, direct=False, auto=True, **more):
    return {"kind": "headset", "on": on, "default": default, "prox": prox, "scarlett": scarlett, "direct": direct,
            "auto": auto} | more


MOVED_DIRECT = "PRO X off — sound moved to the Scarlett, Direct Monitor on"


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
        self.assertEqual(actions, [("default", SCARLETT), ("direct", True), ("notify", MOVED_DIRECT)])
        self.assertEqual(state, {"moved": True, "direct": False})  # Direct Monitor's value before, to put back

    def test_off_with_direct_monitor_already_on_writes_only_the_sink(self):
        state, actions = routing.next({}, event(False, direct=True))
        self.assertEqual(actions, [("default", SCARLETT), ("notify", "PRO X off — sound moved to the Scarlett")])
        self.assertEqual(state, {"moved": True, "direct": True})

    def test_with_the_setting_off_direct_monitor_is_never_touched(self):  # adjudication 1: the sink moves alone
        state, actions = routing.next({}, event(False, auto=False))
        self.assertEqual(actions, [("default", SCARLETT), ("notify", routing.MOVED)])
        self.assertEqual(state, {"moved": True})
        self.assertEqual(routing.next(state, event(True, default=SCARLETT, direct=True, auto=False)),
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

    def test_the_prox_sink_not_back_yet_at_on_puts_direct_monitor_back_and_keeps_only_the_move(self):
        # the dongle replugged: the headset answers before WirePlumber adds its sink; the executor retries the sink
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, prox=None,
                                                                              direct=True)),
                         ({"moved": True}, [("direct", False)]))
        self.assertEqual(routing.next({"moved": True}, event(True, default=SCARLETT, prox=None)), ({"moved": True}, []))

    def test_direct_monitor_left_to_put_back_never_waits_for_the_prox_sink(self):  # no move saved: the sink is moot
        self.assertEqual(routing.next({"direct": False}, event(True, prox=None, direct=True)), ({}, [("direct", False)]))

    def test_the_prox_sink_still_missing_at_the_cap_drops_the_move_and_says_so(self):
        self.assertEqual(routing.next({"moved": True}, event(True, default=SCARLETT, prox=None, final=True)),
                         ({}, [("notify", "PRO X back, but its output isn't available — sound stays on the Scarlett")]))
        self.assertEqual(routing.next({"moved": True, "direct": False},
                                      event(True, default=SCARLETT, prox=None, direct=True, final=True)),
                         ({}, [("direct", False), ("notify", routing.NO_PROX)]))

    def test_direct_monitor_to_put_back_is_kept_while_the_scarlett_is_gone(self):  # never dropped while it can't be read
        owed, gone = {"direct": False}, {"scarlett": None, "direct": None}
        self.assertEqual(routing.next(owed, event(True, final=True, **gone)), (owed, []))  # past the cap
        self.assertEqual(routing.next(owed, event(False, **gone)), (owed, [("notify", routing.NO_SCARLETT)]))
        self.assertEqual(routing.next(owed, event(False, default=HDMI, **gone)), (owed, []))
        # the Scarlett's sink back before its controls can be read: the move keeps the value to put back
        self.assertEqual(routing.next(owed, event(False, direct=None)),
                         ({"direct": False, "moved": True}, [("default", SCARLETT), ("notify", routing.MOVED)]))

    def test_an_off_with_direct_monitor_to_put_back_puts_it_back_first(self):  # Booth's own never taken as the user's
        owed = {"direct": False}
        self.assertEqual(routing.next(owed, event(False, default=HDMI, direct=True)), ({}, [("direct", False)]))
        self.assertEqual(routing.next(owed, event(False, default=HDMI)), ({}, []))  # off by hand meanwhile: resolved
        self.assertEqual(routing.next(owed, event(False, direct=True)),
                         ({"moved": True, "direct": False},
                          [("direct", False), ("default", SCARLETT), ("direct", True), ("notify", MOVED_DIRECT)]))

    def test_an_off_never_carries_a_record_that_is_neither_a_move_nor_owed(self):  # e.g. a hand-edited settings file
        self.assertEqual(routing.next({"direct": True}, event(False)), routing.next({}, event(False)))

    def test_the_tick_puts_direct_monitor_back_once_it_can_be_read(self):
        def tick(direct):
            return {"kind": "direct", "final": True, "default": PROX, "prox": PROX, "scarlett": SCARLETT,
                    "direct": direct, "auto": True}
        self.assertEqual(routing.next({"direct": False}, tick(True)), ({}, [("direct", False)]))
        self.assertEqual(routing.next({"direct": False}, tick(False)), ({}, []))
        self.assertEqual(routing.next({"direct": False}, tick(None)), ({"direct": False}, []))
        for state in ({}, {"moved": True, "direct": False}):  # nothing owed, or the next "on" puts it back
            self.assertEqual(routing.next(state, tick(True)), (state, []))

    def test_on_while_the_scarlett_is_gone_returns_to_the_prox_and_keeps_direct_monitor_to_put_back(self):
        # its name stays the configured default; Direct Monitor can't be read, so its previous value is kept
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(True, default=SCARLETT, scarlett=None,
                                                                              direct=None)),
                         ({"direct": False}, [("default", PROX), ("notify", routing.BACK)]))
        self.assertEqual(routing.next({"direct": False}, event(True, scarlett=None, direct=None)), ({"direct": False}, []))
        self.assertEqual(routing.next({"direct": False}, event(True, direct=True)), ({}, [("direct", False)]))
        self.assertEqual(routing.next({"direct": False}, event(True, direct=False)), ({}, []))

    def test_a_second_move_keeps_the_first_moves_direct_monitor_value(self):  # a restart, then the PRO X picked by hand
        state, actions = routing.next({"moved": True, "direct": False}, event(False, direct=True))
        self.assertEqual(state, {"moved": True, "direct": False})
        self.assertEqual(actions, [("default", SCARLETT), ("notify", routing.MOVED)])

    def test_a_second_move_with_direct_monitor_unreadable_keeps_the_saved_value(self):  # amixer failed
        self.assertEqual(routing.next({"moved": True, "direct": False}, event(False, direct=None)),
                         ({"moved": True, "direct": False}, [("default", SCARLETT), ("notify", routing.MOVED)]))

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
        self.assertEqual(desk.notes, [MOVED_DIRECT, routing.BACK])

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

    def test_the_dongle_replugged_returns_output_once_its_sink_is_back(self):
        desk = World()
        desk.headset(False)  # unplugged: off at once
        state, actions = routing.next(desk.state, event(True, desk.default, None, SCARLETT, desk.direct))
        self.assertEqual((state, actions), ({"moved": True}, [("direct", False)]))  # before WirePlumber adds its sink
        desk.state, desk.direct = state, False
        desk.headset(True)  # the retry, the sink back
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


class MicRule(unittest.TestCase):
    """#20: Booth Mic's source, as heal chose it. A move to a backup or to no mic is said on the heal that made it; only
    the return to the Scarlett (and DOWN) waits for a second heal in a row, so a flap is said once each way."""

    @staticmethod
    def mic(source):
        return {"kind": "mic", "source": source}

    def test_a_move_to_a_backup_or_no_mic_is_said_on_the_heal_that_made_it_and_only_once(self):
        for source in (voice.PRO_X_MIC, voice.BRIO_MIC, None):  # privacy: never a mic the user wasn't told of
            state, actions = routing.next({}, self.mic(source))
            self.assertEqual(actions, [("notify", routing.MIC_SAID[source]), ("label", source)])
            self.assertEqual(routing.next(state, self.mic(source)), (state, []))
        state, actions = routing.next({"said": voice.PRO_X_MIC, "seen": voice.SCARLETT}, self.mic(voice.BRIO_MIC))
        self.assertEqual((state, actions[0]), ({"said": voice.BRIO_MIC}, ("notify", routing.MIC_SAID[voice.BRIO_MIC])))

    def test_booth_mic_starts_on_the_scarlett_so_nothing_is_said_for_it(self):
        state, actions = routing.next({}, self.mic(voice.SCARLETT))
        self.assertEqual((state, actions), ({"said": voice.SCARLETT}, []))

    def test_the_return_to_the_scarlett_is_said_once_it_holds_on_two_heals(self):
        state, actions = routing.next({"said": voice.BRIO_MIC}, self.mic(voice.SCARLETT))
        self.assertEqual((state, actions), ({"said": voice.BRIO_MIC, "seen": voice.SCARLETT}, []))
        state, actions = routing.next(state, self.mic(voice.SCARLETT))
        self.assertEqual(actions, [("notify", routing.MIC_SAID[voice.SCARLETT]), ("label", voice.SCARLETT)])

    def test_a_flap_is_said_once_each_way_never_zero_never_a_stream(self):
        state, said = {}, []
        for source in [voice.PRO_X_MIC, voice.SCARLETT] * 5 + [voice.SCARLETT]:
            state, actions = routing.next(state, self.mic(source))
            said += [v for k, v in actions if k == "notify"]
        self.assertEqual(said, [routing.MIC_SAID[voice.PRO_X_MIC], routing.MIC_SAID[voice.SCARLETT]])
        self.assertEqual(state, {"said": voice.SCARLETT})

    def test_booth_mic_down_is_said_once_it_holds_on_two_heals(self):  # its 5 s restart gap is not a move
        state, actions = routing.next({}, self.mic(routing.DOWN))
        self.assertEqual(actions, [])
        self.assertEqual(routing.next(state, self.mic(routing.DOWN))[1][0], ("notify", routing.MIC_SAID[routing.DOWN]))

    def test_the_mic_rule_never_touches_the_mute_or_a_default(self):  # mute lives on booth_mic: nothing to carry over
        state, kinds = {}, set()
        for source in (voice.PRO_X_MIC, voice.BRIO_MIC, None, routing.DOWN, voice.SCARLETT):
            for _ in range(2):
                state, actions = routing.next(state, self.mic(source))
                kinds |= {k for k, _ in actions}
        self.assertEqual(kinds, {"notify", "label"})

    def test_the_sidebar_label_names_the_mic(self):
        self.assertEqual(routing.label(voice.PRO_X_MIC), "Booth Mic · from PRO X mic")
        self.assertEqual(routing.label(voice.BRIO_MIC), "Booth Mic · from BRIO mic")
        self.assertEqual(routing.label(voice.SCARLETT), "Booth Mic · from the Scarlett")
        self.assertEqual(routing.label(None), "Booth Mic · no mic")
        self.assertEqual(routing.label(routing.DOWN), "Booth Mic · not running")


class Observe(unittest.TestCase):
    def test_the_facts_from_pw_dump_and_the_scarletts_controls(self):
        direct = "name='Direct Monitor Playback Switch'\n  ; type=BOOLEAN,access=rw------,values=1\n  : values="
        contents = read("scarlett/amixer-contents.txt")
        self.assertIn(direct + "off", contents)
        ctrls = routing.scarlett.parse_contents(contents.replace(direct + "off", direct + "on"))  # 48V stays off
        self.assertEqual(routing.observe(dump(), ctrls),
                         {"default": PROX, "prox": PROX, "scarlett": SCARLETT, "direct": True})

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
    """devices._run stand-in for the reads (the given pw-dump, the Scarlett's captured controls) and service.run's for
    the writes: records them, and fails the commands named in `failing`."""

    def __init__(self, pw, failing=()):
        self.pw, self.writes, self.failing, self.log, self.direct = pw, [], failing, [], "off"

    def __call__(self, *cmd):
        if cmd == ("pw-dump",):
            return self.pw
        if cmd == ("amixer", "-c", "Gen", "contents"):
            return read("scarlett/amixer-contents.txt").replace(
                "name='Direct Monitor Playback Switch'\n  ; type=BOOLEAN,access=rw------,values=1\n  : values=off",
                f"name='Direct Monitor Playback Switch'\n  ; type=BOOLEAN,access=rw------,values=1\n  : values={self.direct}")
        self.writes.append(list(cmd))
        return ""

    def write(self, cmd):
        self.writes.append(cmd)
        self.log.append(cmd[0])
        return cmd[0] not in self.failing


class Executor(unittest.TestCase):
    def executor(self, pw, state=None, card="Gen", failing=(), auto=None):
        self.notes, self.saved, self.run, self.saving = [], [], Fake(pw, failing), True
        settings = {"routing": (state or {}) | ({} if auto is None else {"direct_monitor": auto})}
        with mock.patch.object(routing.threading, "Thread"):
            return routing.Executor(settings, self.notes.append, self.save, self.run, lambda: card, self.run.write)

    def save(self, state):  # the app's: waited for on the main loop → True when it reached the disk
        self.saved.append(state)
        self.run.log.append("save")
        return self.saving

    def settle(self, ex):
        """The 10 s timer until nothing is pending: the number of retries it ran."""
        for n in range(20):
            ex.retry()
            if ex.events.empty():
                return n
            ex.step(ex.events.get_nowait())
        self.fail("still retrying after 20 ticks")

    def test_booth_saves_that_it_turns_direct_monitor_on_before_it_does(self):  # adjudication 3: write-ahead
        ex = self.executor(dump())
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.log, ["wpctl", "save", "amixer"])
        self.assertEqual(self.saved, [{"moved": True, "direct": False}])  # once: nothing new to save after the write
        self.assertEqual(self.notes, ["PRO X off — sound moved to the Scarlett, Direct Monitor on"])

    def test_a_record_that_cant_be_saved_leaves_direct_monitor_off(self):
        ex = self.executor(dump())
        self.saving = False
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.log, ["wpctl", "save"])
        self.assertEqual(self.notes, [routing.MOVED])  # says only what happened
        self.assertEqual(self.saved, [{"moved": True, "direct": False}])  # tried once: the app said it wasn't saved

    def test_a_failed_direct_monitor_write_at_off_keeps_the_record_and_says_only_the_move(self):
        ex = self.executor(dump(), failing=("amixer",))
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.notes, [routing.MOVED])
        self.assertEqual((ex.state, self.saved), ({"moved": True, "direct": False}, [{"moved": True, "direct": False}]))
        self.assertEqual(self.settle(ex), 0)

    def test_direct_monitor_is_written_to_the_card_whose_controls_were_read(self):  # never a second lookup mid-step
        cards, ex = iter(["Gen", "Other"]), self.executor(dump())
        ex.card = lambda: next(cards)
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.writes[-1], ["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "on"])

    def test_the_setting_off_moves_only_the_sink(self):
        ex = self.executor(dump(), auto=False)
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "176"]])
        self.assertEqual((self.notes, self.saved, ex.state), ([routing.MOVED], [{"moved": True}], {"moved": True}))

    def test_the_setting_is_read_at_each_step_and_never_part_of_the_record(self):
        ex = self.executor(dump(SCARLETT, SCARLETT), {"moved": True, "direct": False}, auto=True)
        self.assertEqual(ex.state, {"moved": True, "direct": False})
        ex.settings["routing"] = {"direct_monitor": False}  # the switch turned off since
        self.run.pw = dump()
        ex.state = {}
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "176"]])

    def test_the_prox_sink_missing_for_the_whole_cap_drops_the_move_and_says_so_once(self):
        objects = [o for o in json.loads(dump(SCARLETT, SCARLETT)) if "Logitech" not in json.dumps(o.get("info") or {})]
        ex = self.executor(json.dumps(objects), {"moved": True, "direct": False})
        self.run.direct = "on"
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        # Direct Monitor back at once; only the sink waits
        self.assertEqual(self.run.writes, [["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "off"]])
        self.assertEqual(self.saved, [{"moved": True}])
        self.run.direct = "off"
        self.assertEqual(self.settle(ex), 6)  # ~60 s
        self.assertEqual(self.notes, [routing.NO_PROX])
        self.assertEqual((ex.state, self.saved[-1]), ({}, {}))
        self.assertEqual(len(self.run.writes), 1)  # the sink is never written later

    def test_a_write_that_keeps_failing_is_dropped_after_the_cap_and_said_once(self):
        ex = self.executor(dump(), failing=("wpctl",))
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.settle(ex), 6)
        self.assertEqual(len(self.run.writes), 7)
        self.assertEqual(self.notes, ["Sound routing gave up: moving the sound kept failing"])
        self.assertEqual((ex.state, self.saved), ({}, []))
        ex.listener("off")  # a new transition starts a new cap
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.settle(ex), 6)

    def test_putting_direct_monitor_back_that_keeps_failing_is_dropped_after_the_cap(self):
        ex = self.executor(dump(PROX, PROX), {"direct": False}, failing=("amixer",))
        self.run.direct = "on"
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.settle(ex), 6)
        self.assertEqual(self.notes, ["Sound routing gave up: switching Direct Monitor kept failing"])
        self.assertEqual((ex.state, self.saved), ({}, [{}]))

    def unplug(self, ex, configured):
        """The Scarlett gone: no card, no sink; Direct Monitor kept on across the unplug, as Booth left it."""
        objects = [o for o in json.loads(dump(configured, PROX)) if "Focusrite" not in json.dumps(o.get("info") or {})]
        self.run.pw, self.run.direct, ex.card = json.dumps(objects), "on", lambda: None

    def plug(self, ex):
        self.run.pw, ex.card = dump(), lambda: "Gen"

    def off_then_on_without_the_scarlett(self):
        ex = self.executor(dump())
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        self.unplug(ex, SCARLETT)
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        self.assertEqual((self.notes[-1], ex.state), (routing.BACK, {"direct": False}))
        return ex

    def assert_put_back_on_return(self, ex):
        self.assertEqual(ex.state, {"direct": False})
        writes = len(self.run.writes)
        self.plug(ex)
        self.assertEqual(self.settle(ex), 1)  # the next tick
        self.assertEqual(self.run.writes[writes:],
                         [["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "off"]])
        self.assertEqual((ex.state, self.saved[-1]), ({}, {}))
        self.run.direct = "on"  # turned on by hand later: never turned off by Booth
        self.assertEqual((self.settle(ex), len(self.run.writes)), (0, writes + 1))

    def test_direct_monitor_is_put_back_when_the_scarlett_returns_after_the_cap(self):
        ex = self.off_then_on_without_the_scarlett()
        self.assertEqual(self.settle(ex), 6)  # the cap passes with the Scarlett gone: no tick while it is
        self.assert_put_back_on_return(ex)

    def test_direct_monitor_is_put_back_when_the_scarlett_returns_after_more_transitions(self):
        ex = self.off_then_on_without_the_scarlett()
        self.unplug(ex, PROX)
        for name in ("off", "on", "off"):  # moves nothing and keeps the record
            ex.listener(name)
            ex.step(ex.events.get_nowait())
        self.assertEqual(self.notes[-1], routing.NO_SCARLETT)
        self.assert_put_back_on_return(ex)

    def test_an_off_after_the_scarlett_returned_puts_direct_monitor_back_before_its_move(self):
        ex = self.off_then_on_without_the_scarlett()
        self.plug(ex)
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        dm = ["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch"]
        self.assertEqual(self.run.writes[-3:], [dm + ["off"], ["wpctl", "set-default", "176"], dm + ["on"]])
        self.assertEqual((self.notes[-1], ex.state), (MOVED_DIRECT, {"moved": True, "direct": False}))

    def test_a_record_with_the_scarlett_present_resolves_at_the_first_tick(self):  # e.g. saved before a restart
        for failing, notes in (((), []), (("amixer",), ["Sound routing gave up: switching Direct Monitor kept failing"])):
            ex = self.executor(dump(PROX, PROX), {"direct": False}, failing=failing)
            self.run.direct = "on"
            self.assertEqual(self.settle(ex), 1)
            self.assertEqual(self.run.writes,
                             [["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "off"]])
            self.assertEqual((self.notes, ex.state, self.saved), (notes, {}, [{}]))

    def test_a_put_back_tick_never_stops_the_headsets_retries(self):
        objects = [o for o in json.loads(dump(SCARLETT, SCARLETT)) if "Logitech" not in json.dumps(o.get("info") or {})]
        ex = self.executor(json.dumps(objects), {"moved": True})
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        ex.step({"kind": "direct", "final": True})
        self.assertTrue(ex.pending)

    def test_pw_dump_unknown_for_the_whole_cap_stops_retrying(self):
        ex = self.executor("")
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.settle(ex), 6)
        self.assertEqual((self.run.writes, self.saved), ([], []))

    def test_off_sets_the_scarlett_by_its_id_now_and_direct_monitor_on_by_name(self):
        ex = self.executor(dump())
        ex.step({"kind": "headset", "on": False})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "176"],
                                           ["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "on"]])
        self.assertEqual(self.notes, [MOVED_DIRECT])
        self.assertEqual(self.saved, [{"moved": True, "direct": False}])
        self.assertEqual(ex.state, {"moved": True, "direct": False})

    def test_on_puts_back_the_prox_and_direct_monitor(self):
        self.executor(dump(SCARLETT, SCARLETT), {"moved": True, "direct": True}).step({"kind": "headset", "on": True})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "33"]])  # Direct Monitor was on before: kept
        self.assertEqual((self.notes, self.saved), ([routing.BACK], [{}]))

    def test_direct_monitor_back_off(self):
        ex = self.executor(dump(PROX, PROX), {"direct": False})
        with mock.patch.object(routing.scarlett, "value", return_value=True):  # on, as Booth left it
            ex.step({"kind": "headset", "on": True})
        self.assertEqual(self.run.writes, [["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "off"]])
        self.assertEqual((ex.state, self.saved), ({}, [{}]))

    def test_a_failed_direct_monitor_write_keeps_its_value_to_put_back(self):
        ex = self.executor(dump(PROX, PROX), {"direct": False}, failing=("amixer",))
        with mock.patch.object(routing.scarlett, "value", return_value=True):
            ex.step({"kind": "headset", "on": True})
        self.assertEqual((ex.state, self.saved), ({"direct": False}, []))

    def test_a_failed_set_default_moves_nothing_says_nothing_and_retries(self):
        ex = self.executor(dump(), failing=("wpctl",))
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "176"]])  # Direct Monitor is never turned on alone
        self.assertEqual((self.notes, self.saved, ex.state), ([], [], {}))
        self.assertIs(ex.retry(), True)
        self.assertEqual(ex.events.get_nowait(), {"kind": "headset", "on": False})
        self.run.failing = ()
        ex.step({"kind": "headset", "on": False})
        self.assertEqual((self.notes, self.saved), ([MOVED_DIRECT], [{"moved": True, "direct": False}]))
        ex.retry()
        self.assertTrue(ex.events.empty())  # settled: never re-asserted

    def test_a_failed_set_default_at_on_keeps_the_move_and_retries(self):
        ex = self.executor(dump(SCARLETT, SCARLETT), {"moved": True, "direct": True}, failing=("wpctl",))
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        self.assertEqual((self.notes, self.saved, ex.state), ([], [{"moved": True}], {"moved": True}))
        ex.retry()
        self.assertEqual(ex.events.get_nowait(), {"kind": "headset", "on": True})

    def test_an_empty_or_failed_pw_dump_is_unknown_and_retried(self):  # at login, PipeWire not up yet
        for pw in ("", "[]"):
            ex = self.executor(pw)
            ex.listener("off")
            ex.step(ex.events.get_nowait())
            self.assertEqual((self.run.writes, self.notes, self.saved), ([], [], []))
            ex.retry()
            self.assertEqual(ex.events.get_nowait(), {"kind": "headset", "on": False})
            ex.retry()
            self.assertTrue(ex.events.empty())  # once per unsettled step: a slow worker never piles them up
            self.run.pw = dump()
            ex.step({"kind": "headset", "on": False})
            self.assertEqual(self.notes, [MOVED_DIRECT])

    def test_the_prox_sink_not_back_yet_is_retried_until_it_is(self):
        objects = [o for o in json.loads(dump(SCARLETT, SCARLETT)) if "Logitech" not in json.dumps(o.get("info") or {})]
        ex = self.executor(json.dumps(objects), {"moved": True, "direct": False})
        ex.listener("on")
        ex.step(ex.events.get_nowait())
        self.assertEqual((self.run.writes, self.notes, self.saved), ([], [], [{"moved": True}]))
        ex.retry()
        self.assertEqual(ex.events.get_nowait(), {"kind": "headset", "on": True})
        self.run.pw = dump(SCARLETT, SCARLETT)
        ex.step({"kind": "headset", "on": True})
        self.assertEqual(self.run.writes, [["wpctl", "set-default", "33"]])
        self.assertEqual((self.notes, self.saved, ex.state), ([routing.BACK], [{"moved": True}, {}], {}))
        ex.retry()
        self.assertTrue(ex.events.empty())

    def test_nothing_pending_never_retries(self):
        ex = self.executor(dump(HDMI, HDMI))
        self.assertIs(ex.retry(), True)  # before any event
        ex.listener("off")
        ex.step(ex.events.get_nowait())
        ex.retry()
        self.assertTrue(ex.events.empty())

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
            self.assertIs(ex.write, routing.service.run)  # the writes report their exit status
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
