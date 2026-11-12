"""The PRO X's HID++ battery query: the hidraw node found by vendor/product and usage page in sysfs, the request and
reply bytes ported from HeadsetControl, voltage → an estimated percentage, and no answer within ~1 s meaning off.
Descriptors, uevents and the off reply are captured from this desk's devices."""
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from booth import hidpp
from tests.fixture import read

OFF = bytes.fromhex(read("headset/hidpp-battery-off.hex"))  # captured: the dongle's answer with the headset off
# shortcut: constructed from the ported reply layout (bytes 4-5 mV big-endian, byte 6 state), not captured, because the
# headset was off at capture time; replace with a live capture when the headset is on.
ON = bytes.fromhex("11ff060d0f3c01") + bytes(13)  # 3900 mV, discharging


def node(root, name, device):
    """A sysfs hidraw entry `name` holding the captured uevent and report descriptor of `device`."""
    d = Path(root, name, "device")
    d.mkdir(parents=True)
    (d / "uevent").write_text(read(f"headset/hidraw/{device}.uevent"))
    (d / "report_descriptor").write_bytes(bytes.fromhex(read(f"headset/hidraw/{device}.descriptor.hex").replace("\n", "")))
    return d


def descriptor(device):
    return bytes.fromhex(read(f"headset/hidraw/{device}.descriptor.hex").replace("\n", ""))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_the_pro_x_descriptor_declares_the_hidpp_usage_page(self):
        self.assertIn(0xff43, hidpp.usage_pages(descriptor("pro-x")))
        self.assertIn(0x000c, hidpp.usage_pages(descriptor("pro-x")))  # and its consumer-control collection

    def test_other_descriptors_do_not(self):
        self.assertNotIn(0xff43, hidpp.usage_pages(descriptor("brio")))
        self.assertEqual(hidpp.usage_pages(descriptor("receiver")), {0xff00})

    def test_the_pro_x_node_is_picked_by_vendor_product_and_usage_page(self):
        node(self.root, "hidraw1", "brio")
        node(self.root, "hidraw5", "receiver")
        wrong = node(self.root, "hidraw2", "pro-x")  # the PRO X's ids, but no HID++ collection
        (wrong / "report_descriptor").write_bytes(descriptor("brio"))
        node(self.root, "hidraw7", "pro-x")
        self.assertEqual(hidpp.find_node(self.root), "/dev/hidraw7")

    def test_another_logitech_device_with_hidpp_is_never_picked(self):
        brio = node(self.root, "hidraw0", "brio")
        (brio / "report_descriptor").write_bytes(descriptor("pro-x"))  # the usage page alone is not enough
        self.assertIsNone(hidpp.find_node(self.root))

    def test_no_pro_x_no_node(self):
        node(self.root, "hidraw1", "brio")
        self.assertIsNone(hidpp.find_node(self.root))
        self.assertIsNone(hidpp.find_node(os.path.join(self.root, "missing")))

    def test_an_entry_that_vanishes_mid_scan_is_skipped(self):  # an unplug between listdir and the read
        Path(self.root, "hidraw3").mkdir()
        node(self.root, "hidraw4", "pro-x")
        self.assertEqual(hidpp.find_node(self.root), "/dev/hidraw4")


class Reply(unittest.TestCase):
    def test_the_request_is_headsetcontrols_gpro_battery_query(self):
        self.assertEqual(hidpp.REQUEST, bytes.fromhex("11ff060d") + bytes(16))

    def test_the_captured_error_reply_means_off(self):
        self.assertIs(hidpp.answer(OFF), hidpp.OFF)

    def test_a_battery_reply_gives_voltage_and_charging(self):
        self.assertEqual(hidpp.answer(ON), (3900, False))
        self.assertEqual(hidpp.answer(ON[:6] + b"\x03" + ON[7:]), (3900, True))

    def test_a_voltage_below_the_curve_means_off(self):  # HeadsetControl: offline or an invalid reading
        self.assertIs(hidpp.answer(bytes.fromhex("11ff060d0cf701") + bytes(13)), hidpp.OFF)  # 3319 mV
        self.assertEqual(hidpp.answer(bytes.fromhex("11ff060d0cf801") + bytes(13)), (3320, False))

    def test_other_reports_are_not_the_answer(self):
        self.assertIsNone(hidpp.answer(bytes.fromhex("010200")))  # a consumer-control report (volume key)
        self.assertIsNone(hidpp.answer(bytes.fromhex("11ff050d00") + bytes(15)))  # another feature's reply
        self.assertIsNone(hidpp.answer(bytes.fromhex("11ffff050d05") + bytes(14)))  # another feature's error
        self.assertIsNone(hidpp.answer(b""))


class Estimate(unittest.TestCase):
    def test_the_gpro_curve_points(self):
        for mv, pct in zip(hidpp.VOLTAGES, hidpp.PERCENTAGES):
            self.assertEqual(hidpp.estimate(mv), pct)

    def test_between_points_it_interpolates_and_truncates(self):
        self.assertEqual(hidpp.estimate(3990), 75)
        self.assertEqual(hidpp.estimate(3805), 40)
        self.assertEqual(hidpp.estimate(3831), 50)  # 50.15 → 50, as HeadsetControl's int cast

    def test_outside_the_curve_it_clamps(self):
        self.assertEqual(hidpp.estimate(4300), 100)
        self.assertEqual(hidpp.estimate(3000), 0)


class Exchange(unittest.TestCase):
    """read_reply on a socket pair standing in for the hidraw fd: one report per read, as hidraw gives them."""

    def setUp(self):
        self.ours, self.dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.ours.setblocking(False)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.dev.close)

    def test_no_reply_within_the_timeout_is_off_and_returns_in_time(self):
        t0 = time.monotonic()
        self.assertIs(hidpp.read_reply(self.ours.fileno(), 0.2), hidpp.OFF)
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_unrelated_reports_are_skipped_until_the_answer(self):
        self.dev.send(bytes.fromhex("010200"))
        self.dev.send(ON)
        self.assertEqual(hidpp.read_reply(self.ours.fileno(), 1), (3900, False))

    def test_a_stream_of_unrelated_reports_still_ends_at_the_deadline(self):
        for _ in range(50):
            self.dev.send(bytes.fromhex("010200"))
        t0 = time.monotonic()
        self.assertIs(hidpp.read_reply(self.ours.fileno(), 0.2), hidpp.OFF)
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_the_captured_error_reply_is_off_at_once(self):
        self.dev.send(OFF)
        self.assertIs(hidpp.read_reply(self.ours.fileno(), 5), hidpp.OFF)


class Battery(unittest.TestCase):
    def test_the_node_is_resolved_on_every_query(self):
        found = []
        hidpp.battery(find=lambda: found.append(1))
        hidpp.battery(find=lambda: found.append(1))
        self.assertEqual(found, [1, 1])

    def test_no_node_is_off(self):
        self.assertIsNone(hidpp.battery(find=lambda: None))

    def test_a_node_that_fails_to_open_is_off(self):  # unplugged after the scan, or permissions tightened
        self.assertIsNone(hidpp.battery(find=lambda: "/nonexistent/hidraw9"))

    def test_the_request_is_written_and_the_reply_read_with_a_timeout(self):
        ours, dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(dev.close)
        sent = []

        def opener(path, flags):
            self.assertEqual(path, "/dev/hidraw7")
            self.assertTrue(flags & os.O_NONBLOCK and flags & os.O_RDWR)
            dev.send(ON)
            return os.dup(ours.fileno())
        try:
            reading = hidpp.battery(find=lambda: "/dev/hidraw7", open_=opener,
                                    write=lambda fd, data: sent.append(data) or len(data))
        finally:
            ours.close()
        self.assertEqual((reading, sent), ((3900, False), [hidpp.REQUEST]))

    def test_an_off_reply_is_none(self):
        ours, dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(dev.close)
        self.addCleanup(ours.close)
        dev.send(OFF)
        self.assertIsNone(hidpp.battery(find=lambda: "/dev/hidraw0", open_=lambda p, f: os.dup(ours.fileno()),
                                        write=lambda fd, data: len(data)))


if __name__ == "__main__":
    unittest.main()
