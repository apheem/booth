"""The Scarlett Solo 4th Gen: its card found by name, its controls parsed by name, the routing that maps each input to
its capture channel and PipeWire channel position, and the amixer writes, where 48V can only ever be turned on with the
user's confirmation. Fixtures are the live device's output."""
import unittest

from booth import devices, scarlett
from tests.fixture import read
from tests.test_headset import DUMP, dump, node

CONTENTS, CARDS = read("scarlett/amixer-contents.txt"), read("headset/cards.txt")
SOURCE = "alsa_input.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-surround-40"


def ctrls(**values):
    """The captured controls, with {control name: values} replaced."""
    parsed = scarlett.parse_contents(CONTENTS)
    for name, v in values.items():
        parsed[name.replace("_", " ")]["values"] = v
    return parsed


class Card(unittest.TestCase):
    def test_the_scarlett_is_found_by_its_full_name_as_its_alsa_id(self):
        self.assertEqual(devices.scarlett_card(lambda path: CARDS), "Gen")

    def test_a_renumbered_card_is_still_found_by_name(self):
        moved = CARDS.replace(" 6 [Gen            ]", " 9 [Gen_1          ]")
        self.assertEqual(devices.scarlett_card(lambda path: moved), "Gen_1")

    def test_unplugged_is_none(self):
        gone = "\n".join(line for line in CARDS.splitlines() if "Scarlett" not in line)
        self.assertIsNone(devices.scarlett_card(lambda path: gone))


class Controls(unittest.TestCase):
    def test_every_control_the_page_shows_is_parsed_by_its_full_name(self):
        c = scarlett.parse_contents(CONTENTS)
        self.assertEqual(c[scarlett.PHANTOM]["values"], [False])
        self.assertEqual(c[scarlett.DIRECT]["values"], [False])
        self.assertEqual(c[scarlett.AIR], {"items": ["Off", "Presence", "Presence + Drive"], "values": [1]})
        self.assertEqual(c[scarlett.LEVEL], {"items": ["Line", "Inst"], "values": [0]})
        self.assertEqual(c[scarlett.FIRMWARE]["values"], [2188])
        self.assertEqual(c[scarlett.SYNC], {"items": ["Unlocked", "Locked"], "values": [1]})
        self.assertEqual(len(c["Level Meter"]["values"]), 12)

    def test_value_reads_the_first_value_or_none(self):
        c = scarlett.parse_contents(CONTENTS)
        self.assertEqual(scarlett.value(c, scarlett.AIR), 1)
        self.assertIsNone(scarlett.value(c, "No Such Control"))

    def test_text_shows_an_enum_by_its_item_name(self):
        c = scarlett.parse_contents(CONTENTS)
        self.assertEqual(scarlett.text(c, scarlett.SYNC), "Locked")
        self.assertEqual(scarlett.text(c, scarlett.FIRMWARE), "2188")
        self.assertEqual(scarlett.text(c, "No Such Control"), "Unknown")

    def test_switches_on_and_off(self):
        on = scarlett.parse_contents(CONTENTS.replace(": values=off", ": values=on"))
        self.assertEqual((on[scarlett.PHANTOM]["values"], on[scarlett.DIRECT]["values"]), ([True], [True]))

    def test_empty_output_is_no_controls(self):
        self.assertEqual(scarlett.parse_contents(""), {})


class Routing(unittest.TestCase):
    """XLR is Analogue 2, through DSP 2, on capture channel 2; the instrument jack is Analogue 1 on channel 1."""

    def test_the_captured_routing(self):
        self.assertEqual(scarlett.routing(scarlett.parse_contents(CONTENTS)), {"Instrument": 1, "XLR": 2})

    def test_dsp_inputs_are_followed_to_their_analogue_input(self):
        swapped = ctrls(DSP_Input_1_Capture_Enum=[2], DSP_Input_2_Capture_Enum=[1])
        self.assertEqual(scarlett.routing(swapped), {"XLR": 1, "Instrument": 2})

    def test_an_analogue_input_routed_directly(self):
        self.assertEqual(scarlett.routing(ctrls(PCM_01_Capture_Enum=[0], PCM_03_Capture_Enum=[2]))["XLR"], 2)
        self.assertEqual(scarlett.routing(ctrls(PCM_02_Capture_Enum=[0], PCM_04_Capture_Enum=[2]))["XLR"], 4)

    def test_loopback_mixes_are_never_the_mic(self):
        self.assertEqual(scarlett.routing(ctrls(PCM_02_Capture_Enum=[5])), {"Instrument": 1})

    def test_no_routing_controls_is_no_routing(self):
        self.assertEqual(scarlett.routing({}), {})


class Mic(unittest.TestCase):
    """The record the meters and the voice chain consume: input → capture channel → PipeWire channel position."""

    def test_xlr_is_capture_channel_2_fr_on_the_4_channel_source(self):
        self.assertEqual(scarlett.mic(scarlett.parse_contents(CONTENTS), DUMP, "XLR"),
                         {"input": "XLR", "channel": 2, "position": "FR", "node": SOURCE, "channels": 4})

    def test_instrument_is_channel_1_fl(self):
        rec = scarlett.mic(scarlett.parse_contents(CONTENTS), DUMP, "Instrument")
        self.assertEqual((rec["channel"], rec["position"]), (1, "FL"))

    def test_the_position_comes_from_the_source_node(self):
        pro = dump(lambda o: node(o, SOURCE)["info"]["props"].update({"audio.position": "AUX0,AUX1,AUX2,AUX3"}))
        self.assertEqual(scarlett.mic(scarlett.parse_contents(CONTENTS), pro, "XLR")["position"], "AUX1")

    def test_the_channel_count_comes_from_the_source_node(self):  # the meter takes every channel, unmixed
        stereo = dump(lambda o: node(o, SOURCE)["info"]["props"].update({"audio.position": "FL,FR"}))
        self.assertEqual(scarlett.mic(scarlett.parse_contents(CONTENTS), stereo, "XLR")["channels"], 2)

    def test_no_source_node_still_gives_the_channel(self):
        gone = dump(lambda o: o.remove(node(o, SOURCE)))
        self.assertEqual(scarlett.mic(scarlett.parse_contents(CONTENTS), gone, "XLR"),
                         {"input": "XLR", "channel": 2, "position": None, "node": None, "channels": None})
        self.assertEqual(scarlett.mic(scarlett.parse_contents(CONTENTS), "", "XLR")["channel"], 2)

    def test_an_unrouted_input_is_none(self):
        self.assertIsNone(scarlett.mic(ctrls(PCM_02_Capture_Enum=[5]), DUMP, "XLR"))

    def test_describe(self):
        self.assertEqual(scarlett.describe(scarlett.mic(scarlett.parse_contents(CONTENTS), DUMP, "XLR")),
                         "Capture channel 2 (FR)")
        self.assertEqual(scarlett.describe(None), "Not routed to a capture channel")


class Read(unittest.TestCase):
    def test_reads_the_controls_by_the_card_id_of_now_and_the_mic_record(self):
        ran = []

        def run(*cmd):
            ran.append(list(cmd))
            return DUMP if cmd == ("pw-dump",) else CONTENTS
        state = scarlett.read("XLR", run, lambda: "Gen")
        self.assertEqual(ran, [["amixer", "-c", "Gen", "contents"], ["pw-dump"]])
        self.assertEqual(state["mic"]["channel"], 2)
        self.assertEqual(scarlett.value(state["ctrls"], scarlett.AIR), 1)

    def test_unplugged_is_none_and_runs_nothing(self):
        ran = []
        self.assertIsNone(scarlett.read("XLR", lambda *cmd: ran.append(cmd), lambda: None))
        self.assertEqual(ran, [])

    def test_a_card_gone_between_the_list_and_amixer_is_none(self):
        self.assertIsNone(scarlett.read("XLR", lambda *cmd: "", lambda: "Gen"))


class Writes(unittest.TestCase):
    def test_each_control_is_set_by_name_on_the_card_id(self):
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.AIR, 2),
                         ["amixer", "-c", "Gen", "cset", "name=Line In 2 Air Capture Enum", "2"])
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.LEVEL, 1),
                         ["amixer", "-c", "Gen", "cset", "name=Line In 1 Level Capture Enum", "1"])
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.DIRECT, True),
                         ["amixer", "-c", "Gen", "cset", "name=Direct Monitor Playback Switch", "on"])
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.DIRECT, False)[-1], "off")

    def test_48v_off_never_needs_confirming(self):
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.PHANTOM, False),
                         ["amixer", "-c", "Gen", "cset", "name=Line In 2 Phantom Power Capture Switch", "off"])

    def test_48v_on_is_refused_unless_the_dialog_confirmed_it(self):
        for confirmed in (False, None, 1, "yes"):
            self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.PHANTOM, True, confirmed=confirmed), confirmed)
        self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.PHANTOM, True))
        self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.PHANTOM, 1, confirmed=True))  # a switch takes a bool
        self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.PHANTOM, "on", confirmed=True))

    def test_48v_on_from_the_dialog(self):
        self.assertEqual(scarlett.cset_cmd("Gen", scarlett.PHANTOM, True, confirmed=True)[-2:],
                         ["name=Line In 2 Phantom Power Capture Switch", "on"])

    def test_routing_and_other_controls_are_never_written(self):
        for name in ("PCM 02 Capture Enum", "DSP Input 2 Capture Enum", "Mixer Input 01 Capture Enum",
                     "Mix A Input 01 Playback Volume", scarlett.FIRMWARE):
            self.assertIsNone(scarlett.cset_cmd("Gen", name, 0, confirmed=True), name)

    def test_an_enum_takes_an_index(self):
        self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.AIR, "Presence"))
        self.assertIsNone(scarlett.cset_cmd("Gen", scarlett.AIR, True))

    def send(self, make, card):
        ran = []
        scarlett.send(make, lambda *cmd: ran.append(list(cmd)), lambda: card)
        return ran

    def test_send_resolves_the_card_now_and_runs_the_command(self):
        self.assertEqual(self.send(lambda card: scarlett.cset_cmd(card, scarlett.AIR, 0), "Gen_1"),
                         [["amixer", "-c", "Gen_1", "cset", "name=Line In 2 Air Capture Enum", "0"]])

    def test_send_unplugged_or_refused_runs_nothing(self):
        self.assertEqual(self.send(lambda card: scarlett.cset_cmd(card, scarlett.AIR, 0), None), [])
        self.assertEqual(self.send(lambda card: scarlett.cset_cmd(card, scarlett.PHANTOM, True), "Gen"), [])


if __name__ == "__main__":
    unittest.main()
