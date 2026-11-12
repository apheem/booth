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
