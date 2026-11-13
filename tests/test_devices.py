import os
import unittest

from booth import devices
from tests.fixture import FAKE_SERIAL, read, brio_nodes


class FakeHost:
    """Serves udev properties and format listings from the captured BRIO fixtures; records every node probed."""

    def __init__(self, nodes=None, udev_edit=None):
        self.nodes = brio_nodes() if nodes is None else nodes
        self.udev_edit = udev_edit or {}
        self.probed = []

    def udev(self, path):
        text = read(f"brio/udev-index{self.nodes[path]}.txt")
        for old, new in self.udev_edit.get(self.nodes[path], []):
            text = text.replace(old, new)
        return text

    def formats(self, path):
        self.probed.append(self.nodes[path])
        return read(f"brio/formats-index{self.nodes[path]}.txt")

    def find(self, order=None):
        return devices.find_camera(order or list(self.nodes), self.udev, self.formats)


class FindCamera(unittest.TestCase):
    def test_finds_color_node_by_model(self):
        host = FakeHost()
        self.assertEqual(host.nodes[host.find()], 0)

    def test_never_ir_even_when_ir_is_listed_first(self):
        # The IR node (index2, GREY only) is a capture node of the same model; it must never come back.
        host = FakeHost()
        found = host.find(sorted(host.nodes, key=lambda p: host.nodes[p] != 2))
        self.assertEqual(host.nodes[found], 0)

    def test_none_when_only_ir_offers_capture(self):
        host = FakeHost(nodes={p: n for p, n in brio_nodes().items() if n != 0})
        self.assertIsNone(host.find())

    def test_metadata_nodes_are_never_probed(self):
        host = FakeHost()
        host.find(sorted(host.nodes, reverse=True))
        self.assertNotIn(1, host.probed)
        self.assertNotIn(3, host.probed)

    def test_ir_not_probed_when_color_comes_first(self):
        host = FakeHost()
        host.find(sorted(host.nodes))
        self.assertEqual(host.probed, [0])

    def test_other_model_is_ignored(self):
        host = FakeHost(udev_edit={0: [("ID_MODEL_ID=085e", "ID_MODEL_ID=0825")]})
        self.assertIsNone(host.find())

    def test_other_vendor_is_ignored(self):
        host = FakeHost(udev_edit={0: [("ID_VENDOR_ID=046d", "ID_VENDOR_ID=1234")]})
        self.assertIsNone(host.find())


class Discover(unittest.TestCase):
    """discover() against a fake by-id folder and a fake command runner that records every node it was asked about."""

    def discover(self, formats_index0=None):
        nodes = brio_nodes()
        asked = []

        def run(*cmd):
            path = next(a for a in cmd if a.startswith(devices.BY_ID))
            asked.append(nodes[path])
            if cmd[0] == "udevadm":
                return read(f"brio/udev-index{nodes[path]}.txt")
            if nodes[path] == 0 and formats_index0 is not None:
                return formats_index0
            return read(f"brio/formats-index{nodes[path]}.txt")

        # Listed IR-first, so only the code (never the folder's order) keeps the other nodes unqueried.
        names = [os.path.basename(p) for p in sorted(nodes, key=lambda p: -nodes[p])]
        found = devices.discover(run=run, listdir=lambda _d: names)["camera"]
        return (nodes[found] if found else None), asked

    def test_finds_the_color_node_asking_only_about_it(self):
        self.assertEqual(self.discover(), (0, [0, 0]))

    def test_color_query_failing_never_reaches_the_ir_node(self):
        # A timeout or unplug makes index0's format query return nothing: the IR node must still never be opened.
        found, asked = self.discover(formats_index0="")
        self.assertIsNone(found)
        self.assertEqual(set(asked), {0})

    def test_no_by_id_folder(self):
        def gone(_d):
            raise FileNotFoundError

        self.assertEqual(devices.discover(run=lambda *c: self.fail(c), listdir=gone), {"camera": None})


class Parsers(unittest.TestCase):
    def test_mjpeg_detection(self):
        self.assertTrue(devices.offers_mjpeg(read("brio/formats-index0.txt")))
        self.assertFalse(devices.offers_mjpeg(read("brio/formats-index2.txt")))
        self.assertFalse(devices.offers_mjpeg(""))

    def test_udev_properties(self):
        props = devices.parse_udev(read("brio/udev-index0.txt"))
        self.assertEqual((props["ID_VENDOR_ID"], props["ID_MODEL_ID"]), ("046d", "085e"))
        self.assertEqual(props["ID_V4L_CAPABILITIES"], ":capture:")


class Replugged(unittest.TestCase):
    """By-id folder events as Gio reports them: (the new name, the event's nick)."""
    INDEX0 = f"usb-046d_Logitech_BRIO_{FAKE_SERIAL}-video-index0"

    def test_a_color_node_appearing_is_a_replug(self):
        self.assertTrue(devices.replugged(self.INDEX0, "created"))
        self.assertTrue(devices.replugged(self.INDEX0, "renamed"))  # udev links a temp name, then renames it
        self.assertTrue(devices.replugged(self.INDEX0, "moved-in"))

    def test_the_folder_appearing_is_a_replug(self):
        # udev removes by-id with the last camera; Gio then reports only the folder's return, never its entries.
        self.assertTrue(devices.replugged("by-id", "created"))

    def test_other_events_are_not(self):
        for name, event in [(self.INDEX0, "deleted"), (self.INDEX0, "changes-done-hint"),
                            (self.INDEX0, "attribute-changed"), (self.INDEX0, "moved-out"), ("by-id", "deleted"),
                            (self.INDEX0.replace("index0", "index2"), "created"), (".#udev-tmp", "created"),
                            (self.INDEX0.replace("index0", "index1"), "renamed")]:
            self.assertFalse(devices.replugged(name, event), (name, event))


class Headset(unittest.TestCase):
    """The PRO X: its ALSA card by name (/proc/asound/cards) and its PipeWire sink by node name (pw-dump), both live."""
    CARDS, DUMP = read("headset/cards.txt"), read("headset/pw-dump.json")
    SINK = "alsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.iec958-stereo"

    def test_the_card_by_name_gives_its_alsa_id_not_its_number(self):
        self.assertEqual(devices.find_card(self.CARDS, devices.PRO_X), "Headset")

    def test_a_new_id_after_a_replug(self):
        cards = self.CARDS.replace(" 1 [Headset        ]", " 7 [Headset_1      ]")
        self.assertEqual(devices.find_card(cards, devices.PRO_X), "Headset_1")

    def test_no_card(self):
        self.assertIsNone(devices.find_card(self.CARDS.replace(devices.PRO_X, "Other Headset"), devices.PRO_X))
        self.assertIsNone(devices.find_card("", devices.PRO_X))
        # the name exactly: a longer one that holds it is another card
        self.assertIsNone(devices.find_card(self.CARDS.replace(devices.PRO_X, devices.PRO_X + " 2"), devices.PRO_X))

    def test_headset_card_reads_the_card_list_each_time(self):
        lists = iter([self.CARDS, self.CARDS.replace(" 1 [Headset        ]", " 3 [Headset_1      ]"), ""])
        read_cards = lambda path: path == devices.CARDS and next(lists)  # noqa: E731
        self.assertEqual([devices.headset_card(read_cards) for _ in range(3)], ["Headset", "Headset_1", None])

    def test_sinks_by_node_name_with_ids_and_priorities_and_the_default(self):
        sinks, default = devices.parse_sinks(self.DUMP)
        self.assertEqual(sinks[self.SINK], (33, 1008))
        self.assertEqual(sinks[default], (176, 1009))
        self.assertIn("Scarlett", default)
        self.assertEqual(len(sinks), 3)  # sinks only: no source, no mono-fallback mic

    def test_the_pro_x_sink(self):
        sinks, _ = devices.parse_sinks(self.DUMP)
        self.assertEqual(devices.find_sink(sinks), self.SINK)
        self.assertIsNone(devices.find_sink({n: v for n, v in sinks.items() if n != self.SINK}))

    def test_no_pipewire(self):
        self.assertEqual(devices.parse_sinks(""), ({}, None))
        self.assertEqual(devices.parse_sinks("{}"), ({}, None))


class DongleAppeared(unittest.TestCase):
    """/dev/snd/by-id events: the dongle's control link appearing (its card number is new on every replug)."""
    DONGLE = "usb-Logitech_PRO_X_Wireless_Gaming_Headset-00"

    def test_the_dongle_link_or_the_folder_appearing(self):
        for name, event in [(self.DONGLE, "created"), (self.DONGLE, "renamed"), (self.DONGLE, "moved-in"),
                            ("by-id", "created")]:
            self.assertTrue(devices.dongle_appeared(name, event), (name, event))

    def test_other_events_are_not(self):
        for name, event in [(self.DONGLE, "deleted"), (self.DONGLE, "changes-done-hint"), ("by-id", "deleted"),
                            (f"usb-Focusrite_Scarlett_Solo_4th_Gen_{FAKE_SERIAL}-00", "created")]:
            self.assertFalse(devices.dongle_appeared(name, event), (name, event))


class Presence(unittest.TestCase):
    """#22: the not-connected pages follow the by-id folders, re-resolved on every event."""
    BRIO = f"usb-046d_Logitech_BRIO_{FAKE_SERIAL}-video-index0"

    def test_a_color_node_or_the_folder_coming_or_going_may_move_the_brio(self):
        for name in (self.BRIO, "by-id"):
            for event in ("created", "deleted", "moved-in", "moved-out", "renamed"):
                self.assertTrue(devices.camera_moved(name, event), (name, event))

    def test_the_ir_node_or_another_event_never_does(self):
        self.assertFalse(devices.camera_moved(self.BRIO.replace("index0", "index2"), "deleted"))
        self.assertFalse(devices.camera_moved(self.BRIO, "changes-done-hint"))
        self.assertFalse(devices.camera_moved(self.BRIO, "attribute-changed"))

    def test_the_dongle_is_there_while_its_by_id_link_is(self):
        scarlett = f"usb-Focusrite_Scarlett_Solo_4th_Gen_{FAKE_SERIAL}-00"
        asked = []
        self.assertTrue(devices.dongle_present(lambda p: asked.append(p) or [scarlett, devices.PRO_X_USB + "00"]))
        self.assertEqual(asked, [devices.SND_BY_ID])
        self.assertFalse(devices.dongle_present(lambda p: [scarlett]))

    def test_no_folder_is_no_dongle(self):  # udev removes /dev/snd/by-id with the last USB sound card
        def gone(_path):
            raise FileNotFoundError
        self.assertFalse(devices.dongle_present(gone))


if __name__ == "__main__":
    unittest.main()
