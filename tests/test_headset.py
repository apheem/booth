"""The PRO X headset: the dongle's hardware-volume fix (amixer), and its volume and default output through wpctl, with
the card and sink resolved fresh on every use. Fixtures are the live device's output."""
import contextlib
import io
import json
import unittest

from booth import devices, headset, hidpp
from tests.fixture import read

CGET, CONTENTS = read("headset/amixer-cget-pcm.txt"), read("headset/amixer-contents.txt")
DUMP, VOLUME = read("headset/pw-dump.json"), read("headset/wpctl-get-volume.txt")
PRO_X = "alsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.iec958-stereo"
SCARLETT = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-stereo"


def dump(edit):
    """The captured pw-dump after `edit(objects)`."""
    objects = json.loads(DUMP)
    edit(objects)
    return json.dumps(objects)


def node(objects, name):
    return next(o for o in objects if ((o.get("info") or {}).get("props") or {}).get("node.name") == name)


def default_sink(objects, name):
    meta = next(o for o in objects if o["type"] == "PipeWire:Interface:Metadata")
    next(m for m in meta["metadata"] if m["key"] == "default.audio.sink")["value"] = {"name": name}


class Amixer(unittest.TestCase):
    def test_cget_gives_the_pcm_range_and_value(self):
        self.assertEqual(headset.parse_amixer(CGET), {headset.PCM: {"min": 0, "max": 64, "values": [64]}})

    def test_contents_gives_every_integer_control_and_no_switch(self):
        ctrls = headset.parse_amixer(CONTENTS)
        self.assertEqual(ctrls[headset.PCM], {"min": 0, "max": 64, "values": [64]})
        self.assertEqual(ctrls["Mic Capture Volume"]["max"], 67)
        self.assertNotIn("PCM Playback Switch", ctrls)
        self.assertEqual(ctrls["Playback Channel Map"]["values"], [0, 0])


class HardwareFix(unittest.TestCase):
    def test_at_its_maximum_nothing_is_written(self):
        self.assertIsNone(headset.fix_cmd(1, headset.parse_amixer(CGET)))

    def test_below_its_maximum_it_is_set_to_the_devices_own_maximum(self):
        low = headset.parse_amixer(CGET.replace(": values=64", ": values=41").replace("max=64", "max=70"))
        self.assertEqual(headset.fix_cmd(1, low), ["amixer", "-c", "1", "cset", "name=PCM Playback Volume", "70"])

    def test_one_low_channel_is_enough_to_set_it(self):
        low = headset.parse_amixer(CGET.replace(": values=64", ": values=64,40"))
        self.assertEqual(headset.fix_cmd(1, low)[-1], "64")

    def test_no_pcm_control_writes_nothing(self):
        self.assertIsNone(headset.fix_cmd(1, headset.parse_amixer(CONTENTS.replace("PCM Playback Volume", "Other"))))

    def fix(self, card, cget=CGET):
        ran = []

        def run(*cmd):
            ran.append(list(cmd))
            return cget if cmd[3] == "cget" else ""
        headset.fix_hw_volume(run, lambda: card)
        return ran

    def test_the_card_is_resolved_on_every_fix_and_both_commands_use_its_alsa_id(self):
        low = CGET.replace(": values=64", ": values=41")
        self.assertEqual(self.fix("Headset", low),
                         [["amixer", "-c", "Headset", "cget", "name=PCM Playback Volume"],
                          ["amixer", "-c", "Headset", "cset", "name=PCM Playback Volume", "64"]])
        self.assertEqual(self.fix("Headset_1", low)[1],
                         ["amixer", "-c", "Headset_1", "cset", "name=PCM Playback Volume", "64"])

    def test_the_live_card_list_gives_the_id_to_amixer_never_the_number_resolved_once_for_both(self):
        ran, cards = [], read("headset/cards.txt")
        lists = iter([cards, cards.replace("[Headset  ", "[Headset_1")])  # a second resolution would see another id
        card = lambda: devices.headset_card(lambda path: next(lists))  # noqa: E731
        headset.fix_hw_volume(lambda *cmd: ran.append(list(cmd)) or CGET.replace(": values=64", ": values=41"), card)
        self.assertEqual([cmd[2] for cmd in ran], ["Headset", "Headset"])

    def test_already_at_max_only_reads(self):
        self.assertEqual(self.fix("Headset"), [["amixer", "-c", "Headset", "cget", "name=PCM Playback Volume"]])

    def test_no_dongle_runs_nothing(self):
        self.assertEqual(self.fix(None), [])


class Volume(unittest.TestCase):
    def test_get_volume(self):
        self.assertEqual(headset.parse_volume(VOLUME), 0.63)

    def test_get_volume_muted(self):
        self.assertEqual(headset.parse_volume(VOLUME.replace("\n", " [MUTED]\n")), 0.63)

    def test_get_volume_failed(self):
        self.assertIsNone(headset.parse_volume(""))
        self.assertIsNone(headset.parse_volume("Translate ID error: '33' is not a valid ID\n"))

    def test_slider_position_never_leaves_0_to_100(self):
        self.assertEqual([headset.percent(v) for v in (0.63, 1.0, 1.5, 0.0, -0.2)], [63, 100, 100, 0, 0])

    def test_the_slider_sets_the_pro_x_sink_by_its_id_from_pw_dump(self):
        self.assertEqual(headset.volume_cmd(DUMP, 63), ["wpctl", "set-volume", "-l", "1.0", "33", "0.63"])

    def test_never_more_than_100_percent(self):
        self.assertEqual(headset.volume_cmd(DUMP, 150)[-1], "1.00")
        self.assertEqual(headset.volume_cmd(DUMP, 100)[-1], "1.00")
        self.assertEqual(headset.volume_cmd(DUMP, -5)[-1], "0.00")

    def test_a_new_id_after_a_replug_is_used(self):
        self.assertEqual(headset.volume_cmd(dump(lambda o: node(o, PRO_X).update(id=90)), 50)[4], "90")

    def test_no_pro_x_sink_no_command(self):
        gone = dump(lambda o: o.remove(node(o, PRO_X)))
        self.assertIsNone(headset.volume_cmd(gone, 50))
        self.assertIsNone(headset.volume_cmd("", 50))  # no PipeWire: pw-dump printed nothing


class UseForAllSound(unittest.TestCase):
    """On remembers the default sink it replaces, by node name; off puts that one back, or clears WirePlumber's
    configured default sink (setting 0) when it is gone. Never a sink picked by priority."""

    def test_on_makes_the_pro_x_the_default_sink_and_remembers_the_one_before(self):
        self.assertEqual(headset.default_cmd(dump(lambda o: default_sink(o, SCARLETT)), True),
                         (["wpctl", "set-default", "33"], SCARLETT))

    def test_on_by_the_id_of_now_after_a_replug(self):
        self.assertEqual(headset.default_cmd(dump(lambda o: node(o, PRO_X).update(id=90)), True)[0],
                         ["wpctl", "set-default", "90"])

    def test_on_when_the_pro_x_already_is_the_default_remembers_nothing(self):
        self.assertEqual(headset.default_cmd(dump(lambda o: default_sink(o, PRO_X)), True)[1], None)

    def test_off_puts_back_the_remembered_sink_by_its_id_of_now(self):
        moved = dump(lambda o: (node(o, SCARLETT).update(id=201), default_sink(o, PRO_X)))
        self.assertEqual(headset.default_cmd(moved, False, SCARLETT), (["wpctl", "set-default", "201"], None))

    def test_off_with_the_remembered_sink_gone_clears_the_configured_default(self):
        gone = dump(lambda o: (o.remove(node(o, SCARLETT)), default_sink(o, PRO_X)))
        self.assertEqual(headset.default_cmd(gone, False, SCARLETT), (["wpctl", "clear-default", "0"], None))

    def test_off_with_nothing_remembered_clears_the_configured_default(self):
        self.assertEqual(headset.default_cmd(dump(lambda o: default_sink(o, PRO_X)), False, None),
                         (["wpctl", "clear-default", "0"], None))

    def test_off_never_moves_a_default_the_user_picked_elsewhere(self):
        for previous in (SCARLETT, None):
            self.assertEqual(headset.default_cmd(dump(lambda o: default_sink(o, SCARLETT)), False, previous),
                             (None, None))

    def test_no_pro_x_sink_no_command(self):
        self.assertEqual(headset.default_cmd(dump(lambda o: o.remove(node(o, PRO_X))), True), (None, None))
        self.assertEqual(headset.default_cmd(dump(lambda o: o.remove(node(o, PRO_X))), False, SCARLETT), (None, None))


class State(unittest.TestCase):
    def state(self, text=DUMP):
        asked = []

        def run(*cmd):
            asked.append(list(cmd))
            return text if cmd == ("pw-dump",) else VOLUME
        return headset.state(run), asked

    def test_volume_of_the_sink_resolved_now_and_not_default(self):
        state, asked = self.state()
        self.assertEqual(state, {"volume": 0.63, "default": False})
        self.assertEqual(asked, [["pw-dump"], ["wpctl", "get-volume", "33"]])

    def test_default_when_pipewire_says_so(self):
        state, _ = self.state(dump(lambda o: default_sink(o, PRO_X)))
        self.assertIs(state["default"], True)

    def test_unplugged(self):
        self.assertEqual(self.state(dump(lambda o: o.remove(node(o, PRO_X)))), (None, [["pw-dump"]]))

    def test_sidebar_subtitle(self):
        self.assertEqual(headset.status({"volume": 0.63, "default": False}), "Volume 63 %")
        self.assertEqual(headset.status({"volume": 1.5, "default": False}), "Volume 150 %")  # boosted elsewhere: the truth
        self.assertEqual(headset.status(None), "Not connected")

    def test_sidebar_subtitle_with_the_battery(self):
        on, off = monitor((3990, False)), monitor(hidpp.OFF)
        self.assertEqual(headset.status({"volume": 0.63, "default": False}, on), "~75 % · Volume 63 %")
        self.assertEqual(headset.status({"volume": 0.63, "default": False}, off), "Off · Volume 63 %")
        self.assertEqual(headset.status(None, off), "Not connected")  # the dongle itself is gone
        self.assertEqual(headset.status({"volume": 0.63, "default": False}, headset.HeadsetMonitor()), "Volume 63 %")


def monitor(*readings):
    """A HeadsetMonitor after these battery readings, recording its events and notifications."""
    m = headset.HeadsetMonitor(notify=lambda pct: m.log.append(("notify", pct)))
    m.log = []
    m.listeners.append(lambda event: m.log.append(event))
    for r in readings:
        m.update(r)
    return m


class Battery(unittest.TestCase):
    """The service's 10 s battery poll: on/off and battery events, smoothing, and the warn-once rule."""

    def test_the_first_answer_is_on_and_a_battery_level(self):
        m = monitor((3990, False))
        self.assertEqual((m.on, m.percent, m.charging, m.log), (True, 75, False, ["on", "battery"]))

    def test_the_error_reply_is_off_at_once(self):
        m = monitor(hidpp.OFF)
        self.assertEqual((m.on, m.percent, m.log), (False, None, ["off"]))
        m = monitor((3990, False), hidpp.OFF)
        self.assertEqual((m.on, m.log[-1]), (False, "off"))

    def test_one_missed_reply_does_not_flap_off_and_on(self):  # adjudication: a single ~1 s timeout is a miss
        m = monitor((3990, False), None, (3990, False))
        self.assertEqual((m.on, m.percent, m.log), (True, 75, ["on", "battery"]))

    def test_one_missed_reply_keeps_the_smoothing(self):
        m = monitor((3990, False), None, (3830, False))  # a load spike after a miss still moves only part of the way
        self.assertEqual(m.percent, monitor((3990, False), (3830, False)).percent)
        self.assertGreater(m.percent, 50)

    def test_two_missed_replies_in_a_row_are_off(self):
        m = monitor((3990, False), None)
        self.assertEqual((m.on, m.percent), (True, 75))
        m.update(None)
        self.assertEqual((m.on, m.percent, m.log), (False, None, ["on", "battery", "off"]))

    def test_misses_count_only_in_a_row(self):
        m = monitor((3990, False), None, (3990, False), None)
        self.assertEqual((m.on, m.log), (True, ["on", "battery"]))

    def test_no_answer_before_the_first_reply_is_off_after_two_misses(self):
        m = monitor(None)
        self.assertEqual((m.on, m.log), (None, []))
        m.update(None)
        self.assertEqual((m.on, m.log), (False, ["off"]))

    def test_going_off_drops_the_last_level(self):  # no stale percent for a headset that no longer answers
        m = monitor((3990, False), hidpp.OFF)
        self.assertEqual((m.on, m.percent, m.log), (False, None, ["on", "battery", "off"]))

    def test_on_off_on_emits_each_change_once(self):
        m = monitor((3990, False), (3990, False), None, None, (3990, False))
        self.assertEqual(m.log, ["on", "battery", "off", "on", "battery"])

    def test_an_unchanged_level_emits_nothing(self):
        m = monitor((3990, False))
        m.log.clear()
        m.update((3990, False))
        self.assertEqual(m.log, [])

    def test_a_level_change_alone_is_a_battery_event(self):
        m = monitor((3990, False))
        m.log.clear()
        m.update((3830, False))  # smoothed, still discharging, but the estimate moved
        self.assertLess(m.percent, 75)
        self.assertEqual(m.log, ["battery"])

    def test_readings_are_smoothed(self):
        m = monitor((3990, False), (3830, False))  # one low read (a load spike) moves it only part of the way
        self.assertGreater(m.percent, 50)
        self.assertLess(m.percent, 75)
        for _ in range(30):
            m.update((3830, False))
        self.assertEqual(m.percent, 50)  # a lasting change gets there

    def test_charging_or_coming_back_on_starts_the_smoothing_afresh(self):
        m = monitor((3830, False), (4150, True))  # plugged in: the voltage jumps, and that is the truth
        self.assertEqual((m.percent, m.charging), (100, True))
        m = monitor((3830, False), hidpp.OFF, (4150, False))  # swapped/charged while off
        self.assertEqual(m.percent, 100)

    def test_a_charging_change_alone_is_a_battery_event(self):
        m = monitor((4150, False))
        m.log.clear()
        m.update((4150, True))
        self.assertEqual(m.log, ["battery"])

    def test_one_warning_per_discharge_at_15_percent(self):
        m = monitor(*[(3710, False)] * 5)  # ~13 %
        self.assertEqual([e for e in m.log if e[0] == "notify"], [("notify", 13)])

    def test_exactly_15_percent_warns(self):
        m = monitor((3717, False))  # 3717 mV → 15 % on the curve
        self.assertEqual((m.percent, [e for e in m.log if e[0] == "notify"]), (15, [("notify", 15)]))

    def test_above_15_percent_no_warning(self):
        m = monitor((3746, False))  # ~21 %
        self.assertNotIn("notify", [e[0] for e in m.log])

    def test_it_rearms_only_above_30_percent(self):
        m = monitor((3710, False))
        for mv in [3780] * 30:  # back to 30 %: not re-armed
            m.update((mv, False))
        for _ in range(30):
            m.update((3710, False))
        self.assertEqual(len([e for e in m.log if e[0] == "notify"]), 1)
        for _ in range(30):
            m.update((3830, False))  # 50 %: re-armed
        for _ in range(30):
            m.update((3710, False))
        self.assertEqual(len([e for e in m.log if e[0] == "notify"]), 2)

    def test_off_and_on_again_low_is_the_same_discharge(self):
        m = monitor((3710, False), hidpp.OFF, (3710, False))
        self.assertEqual(len([e for e in m.log if e[0] == "notify"]), 1)

    def test_no_warning_while_charging(self):
        m = monitor((3710, True))
        self.assertNotIn("notify", [e[0] for e in m.log])

    def test_a_listener_that_raises_does_not_stop_the_others_or_the_warning(self):
        m = headset.HeadsetMonitor(notify=lambda pct: seen.append(("notify", pct)))
        seen = []
        m.listeners += [lambda e: 1 / 0, seen.append]
        with contextlib.redirect_stderr(io.StringIO()):
            m.update((3710, False))
        self.assertEqual(seen, ["on", "battery", ("notify", 13)])

    def test_card_texts(self):
        self.assertEqual(headset.card(monitor((3990, False))), ("PRO X Wireless — Connected", "~75 % (est.)"))
        self.assertEqual(headset.card(monitor((3990, True))), ("PRO X Wireless — Connected", "~75 % (est.), charging"))
        self.assertEqual(headset.card(monitor(hidpp.OFF)), ("PRO X Wireless — Off", None))
        self.assertEqual(headset.card(headset.HeadsetMonitor()), ("PRO X Wireless", None))  # before the first poll


class Poll(unittest.TestCase):
    def test_the_query_runs_off_the_main_loop_and_polls_never_pile_up(self):
        spawned = []
        m = headset.HeadsetMonitor(spawn=lambda work, done: spawned.append((work, done)))
        self.assertIs(m.poll(), True)  # a GLib timeout that keeps going
        m.poll()  # the first one hasn't answered yet
        self.assertEqual(len(spawned), 1)
        work, done = spawned[0]
        self.assertIs(work, headset.hidpp.battery)
        done((3990, False))
        self.assertEqual((m.on, m.percent), (True, 75))
        m.poll()
        self.assertEqual(len(spawned), 2)


class Settings(unittest.TestCase):
    """Sidetone and auto power-off through the monitor: one HID++ exchange at a time with the battery poll, the saved
    values (or the defaults) re-sent on every "on", and `acked` true only for a command the headset echoed."""

    def setUp(self):
        self.spawned, self.log, self.saved = [], [], {}
        self.m = headset.HeadsetMonitor(spawn=lambda work, done: self.spawned.append((work, done)),
                                        settings={"headphones": self.saved})
        self.m.listeners.append(self.log.append)

    def sent(self):
        """The requests of every settings exchange spawned so far."""
        return [work.args[0] for work, _ in self.spawned if getattr(work, "func", None) is hidpp.setting]

    def finish(self, result):
        self.spawned[-1][1](result)

    def on(self):
        self.m.poll()
        self.finish((3990, False))

    def test_on_sends_the_saved_values_one_exchange_at_a_time(self):
        self.saved.update(sidetone=64, auto_off=10)
        self.on()
        self.assertEqual(len(self.sent()), 1)  # the second waits for the first's reply
        self.finish(True)
        self.assertEqual(sorted(self.sent()), sorted([hidpp.sidetone_request(64), hidpp.auto_off_request(10)]))
        self.finish(True)
        self.assertFalse(self.m.busy)

    def test_nothing_saved_sends_sidetone_off_and_30_minutes(self):
        self.on()
        self.finish(True)
        self.assertEqual(sorted(self.sent()), sorted([hidpp.sidetone_request(0), hidpp.auto_off_request(30)]))
        self.assertEqual(self.saved, {})  # a probe, not the user's choice: nothing is saved

    def test_every_on_sends_them_again(self):  # they may reset at power-on
        self.on()
        self.assertEqual(len(self.sent()), 1)
        self.finish(True)
        self.finish(True)
        self.m.poll()
        self.finish(hidpp.OFF)
        self.on()
        self.assertEqual(len(self.sent()), 3)
        self.finish(True)
        self.assertEqual(len(self.sent()), 4)

    def test_a_battery_poll_waits_while_a_setting_is_in_flight(self):
        self.on()
        self.m.poll()
        self.assertEqual(len(self.spawned), 2)  # the battery query, then one setting: no second thread on the node

    def test_a_setting_asked_during_a_battery_query_waits_for_its_reply(self):
        self.m.poll()
        self.m.set("sidetone", 10)
        self.assertEqual(self.sent(), [])
        self.finish(None)  # a miss: still off is unknown, nothing re-sent, the user's ask goes now
        self.assertEqual(self.sent(), [hidpp.sidetone_request(10)])

    def test_a_drag_sends_only_the_last_value_and_kinds_never_drop_each_other(self):
        self.m.poll()
        for level in (10, 20, 30):
            self.m.set("sidetone", level)
        self.m.set("auto_off", 5)
        self.finish(None)
        self.finish(True)
        self.finish(True)
        self.assertEqual(sorted(self.sent()), sorted([hidpp.sidetone_request(30), hidpp.auto_off_request(5)]))

    def test_only_an_acknowledged_command_is_acked(self):
        self.m.set("sidetone", 10)
        self.finish(True)
        self.assertEqual((self.m.acked, self.log), ({"sidetone": True}, ["ack"]))
        for result in (False, None):  # refused (the captured error reply), or no answer / the exchange raised
            self.m.set("sidetone", 10)
            self.finish(result)
            self.assertEqual(self.m.acked, {"sidetone": False})
        self.assertFalse(self.m.busy)

    def test_off_forgets_every_acknowledgement(self):
        self.on()
        self.finish(True)
        self.finish(True)
        self.assertEqual(self.m.acked, {"sidetone": True, "auto_off": True})
        self.m.poll()
        self.finish(hidpp.OFF)
        self.assertEqual((self.m.acked, self.log[-1]), ({}, "off"))

    def test_without_the_settings_nothing_is_sent(self):  # the monitor before the app loaded them
        m = headset.HeadsetMonitor(spawn=lambda work, done: self.spawned.append((work, done)))
        m.poll()
        self.finish((3990, False))
        self.assertEqual(self.sent(), [])


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.spawned = []
        self.writer = headset.Writer(lambda make, done: self.spawned.append((make, done)))

    def test_one_write_at_a_time_and_a_drag_sends_only_the_last_value(self):
        for p in (10, 20, 30):
            self.writer.want("volume", p)
        self.assertEqual([m for m, _ in self.spawned], [10])
        self.spawned[0][1]()
        self.assertEqual([m for m, _ in self.spawned], [10, 30])
        self.spawned[1][1]()
        self.assertEqual(len(self.spawned), 2)
        self.assertFalse(self.writer.busy)

    def test_a_switch_write_is_never_dropped_by_a_volume_write(self):
        self.writer.want("volume", 10)
        self.writer.want("volume", 20)
        self.writer.want("default", True)
        self.spawned[0][1]()
        self.spawned[1][1]()
        self.assertEqual(sorted(str(m) for m, _ in self.spawned), ["10", "20", "True"])

    def test_a_poll_begun_before_a_write_is_not_shown(self):
        since = self.writer.writes
        self.assertTrue(self.writer.fresh(since))
        self.writer.want("volume", 10)
        self.assertFalse(self.writer.fresh(since))  # in flight
        self.spawned[0][1]()
        self.assertFalse(self.writer.fresh(since))  # done, but the poll read the old value
        self.assertTrue(self.writer.fresh(self.writer.writes))

    def test_a_poll_begun_while_a_write_is_in_flight_is_not_shown(self):
        self.writer.want("volume", 10)
        since = self.writer.writes
        self.assertFalse(self.writer.fresh(since))  # still in flight: the poll may have read the old value
        self.spawned[0][1]()
        self.assertFalse(self.writer.fresh(since))  # done, but it may have read before the write landed
        self.assertTrue(self.writer.fresh(self.writer.writes))


class Send(unittest.TestCase):
    def test_the_sink_is_resolved_right_before_the_write(self):
        ran = []
        headset.send(lambda text: ["wpctl", "x", text], lambda *cmd: ran.append(cmd) or "DUMP")
        self.assertEqual(ran, [("pw-dump",), ("wpctl", "x", "DUMP")])

    def test_nothing_to_send(self):
        ran = []
        headset.send(lambda text: None, lambda *cmd: ran.append(cmd) or "")
        self.assertEqual(ran, [("pw-dump",)])


if __name__ == "__main__":
    unittest.main()
