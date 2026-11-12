import os
import unittest

from booth import devices
from tests.fixture import read, brio_nodes


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


if __name__ == "__main__":
    unittest.main()
