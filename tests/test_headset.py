"""The PRO X headset: the dongle's hardware-volume fix (amixer), and its volume and default output through wpctl, with
the card and sink resolved fresh on every use. Fixtures are the live device's output."""
import json
import unittest

from booth import devices, headset
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

    def test_the_card_is_resolved_on_every_fix(self):
        low = CGET.replace(": values=64", ": values=41")
        self.assertEqual(self.fix(1, low), [["amixer", "-c", "1", "cget", "name=PCM Playback Volume"],
                                            ["amixer", "-c", "1", "cset", "name=PCM Playback Volume", "64"]])
        self.assertEqual(self.fix(7, low)[1], ["amixer", "-c", "7", "cset", "name=PCM Playback Volume", "64"])

    def test_already_at_max_only_reads(self):
        self.assertEqual(self.fix(1), [["amixer", "-c", "1", "cget", "name=PCM Playback Volume"]])

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
        moved = dump(lambda o: node(o, SCARLETT).update(id=201))
        self.assertEqual(headset.default_cmd(moved, False, SCARLETT), (["wpctl", "set-default", "201"], None))

    def test_off_with_the_remembered_sink_gone_clears_the_configured_default(self):
        gone = dump(lambda o: o.remove(node(o, SCARLETT)))
        self.assertEqual(headset.default_cmd(gone, False, SCARLETT), (["wpctl", "clear-default", "0"], None))

    def test_off_with_nothing_remembered_clears_the_configured_default(self):
        self.assertEqual(headset.default_cmd(DUMP, False, None), (["wpctl", "clear-default", "0"], None))

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
