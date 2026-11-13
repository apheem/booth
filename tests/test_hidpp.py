"""The PRO X's HID++ battery query: the hidraw node found by vendor/product and usage page in sysfs, the request and
reply bytes ported from HeadsetControl, voltage → an estimated percentage, the dongle's error reply meaning off and no
answer within ~1 s being a miss (None).
Descriptors, uevents and the off reply are captured from this desk's devices."""
import errno
import os
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from booth import hidpp
from tests.fixture import read

OFF = bytes.fromhex(read("headset/hidpp-battery-off.hex"))  # captured: the dongle's answer with the headset off
# shortcut: constructed from the ported reply layout (bytes 4-5 mV big-endian, byte 6 state), not captured, because the
# headset was off at capture time; replace with a live capture when the headset is on.
ON = bytes.fromhex("11ff060d0f3c01") + bytes(13)  # 3900 mV, discharging
# captured with the headset off: the dongle's error replies to sidetone 0 and auto power-off 30 min
SIDETONE_OFF = bytes.fromhex(read("headset/hidpp-sidetone-off.hex"))
AUTO_OFF_OFF = bytes.fromhex(read("headset/hidpp-auto-off-off.hex"))
# shortcut: HID++ 2.0's reply layout (the request's feature index and function/software-id echoed), not captured: the
# headset was off at capture time; replace with a live capture when the headset is on.
SIDETONE_ACK = bytes.fromhex("11ff051a00") + bytes(15)
AUTO_OFF_ACK = bytes.fromhex("11ff07211e") + bytes(15)


def info(vendor=0x046D, product=0x0ABA):
    """An injected ioctl answering HIDIOCGRAWINFO for a USB device with these ids."""
    return lambda fd, request, buf: struct.pack("=Ihh", 3, vendor, product)


PRO_X = info()


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
        self.assertEqual(hidpp.usage_pages(descriptor("pro-x")), {0x0001, 0x000a, 0x000c, 0xff00, 0xff43})

    def test_a_4_byte_item_is_stepped_over_whole(self):  # Logical Maximum 0x05000000, then Usage Page 0x0c
        self.assertEqual(hidpp.usage_pages(bytes.fromhex("2700000005050c")), {0x000c})

    def test_a_long_item_is_skipped_whole(self):  # 0xfe, data size, long tag, data: its data is never parsed as items
        self.assertEqual(hidpp.usage_pages(bytes.fromhex("fe02100501050c")), {0x000c})

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

    def test_a_reply_for_another_device_index_is_not_the_answer(self):
        self.assertIsNone(hidpp.answer(bytes.fromhex("1101060d0f3c01") + bytes(13)))

    def test_an_unsolicited_event_of_the_same_feature_index_is_not_a_level(self):  # function 0, not getBattery's 0x0d
        self.assertIsNone(hidpp.answer(bytes.fromhex("11ff06000f3c01") + bytes(13)))


class Settings(unittest.TestCase):
    """Sidetone and auto power-off: HeadsetControl's LogitechGPro::setSidetone and setInactiveTimeHIDPP."""

    def test_sidetone_requests_map_0_to_128_onto_the_headsets_0_to_100(self):
        self.assertEqual(hidpp.sidetone_request(0), bytes.fromhex("11ff051a00") + bytes(15))  # the bytes sent live
        self.assertEqual(hidpp.sidetone_request(128)[:5], bytes.fromhex("11ff051a64"))
        self.assertEqual(hidpp.sidetone_request(64)[4], 50)
        self.assertEqual(hidpp.sidetone_request(127)[4], 99)  # map<uint8_t> truncates
        self.assertEqual(hidpp.sidetone_request(200)[4], 100)  # clamped, as map() clamps
        self.assertEqual(hidpp.sidetone_request(-5)[4], 0)

    def test_auto_off_requests_carry_the_minutes(self):
        self.assertEqual(hidpp.auto_off_request(30), bytes.fromhex("11ff07211e") + bytes(15))  # the bytes sent live
        self.assertEqual(hidpp.auto_off_request(0)[4], 0)  # never
        self.assertEqual(hidpp.auto_off_request(90)[4], 90)
        self.assertEqual(hidpp.auto_off_request(91)[4], 90)
        self.assertEqual(hidpp.auto_off_request(-1)[4], 0)

    def test_every_request_is_one_long_report(self):
        for request in (hidpp.sidetone_request(64), hidpp.auto_off_request(30)):
            self.assertEqual(len(request), 20)

    def test_the_echo_is_the_acknowledgement(self):
        self.assertIs(hidpp.acked(SIDETONE_ACK, hidpp.SIDETONE), True)
        self.assertIs(hidpp.acked(AUTO_OFF_ACK, hidpp.AUTO_OFF), True)

    def test_the_captured_error_replies_are_not_acknowledged(self):
        self.assertIs(hidpp.acked(SIDETONE_OFF, hidpp.SIDETONE), False)
        self.assertIs(hidpp.acked(AUTO_OFF_OFF, hidpp.AUTO_OFF), False)

    def test_other_reports_are_not_the_answer(self):
        for report in (OFF, ON, AUTO_OFF_ACK, AUTO_OFF_OFF, bytes.fromhex("010200"), b"",
                       bytes.fromhex("1101051a00") + bytes(15),  # another device index
                       bytes.fromhex("11ff051000") + bytes(15)):  # an unsolicited event of the feature (sw id 0)
            self.assertIsNone(hidpp.acked(report, hidpp.SIDETONE), report.hex())
        self.assertIsNone(hidpp.answer(SIDETONE_ACK))  # nor is an acknowledgement a battery answer
        self.assertIsNone(hidpp.answer(SIDETONE_OFF))


class Estimate(unittest.TestCase):
    def test_the_gpro_curve_points(self):
        for mv, pct in zip(hidpp.VOLTAGES, hidpp.PERCENTAGES):
            self.assertEqual(hidpp.estimate(mv), pct)

    def test_between_points_it_interpolates_and_truncates(self):
        self.assertEqual(hidpp.estimate(3990), 75)
        self.assertEqual(hidpp.estimate(3805), 40)
        self.assertEqual(hidpp.estimate(3970), 71)  # 71.875 → 71, not 72: HeadsetControl's int cast truncates

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

    def test_no_reply_within_the_timeout_is_a_miss_not_off_and_returns_in_time(self):
        t0 = time.monotonic()
        self.assertIsNone(hidpp.read_reply(self.ours.fileno(), 0.2))
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_unrelated_reports_are_skipped_until_the_answer(self):
        self.dev.send(bytes.fromhex("010200"))
        self.dev.send(ON)
        self.assertEqual(hidpp.read_reply(self.ours.fileno(), 1), (3900, False))

    def test_a_stream_of_unrelated_reports_still_ends_at_the_deadline(self):
        for _ in range(50):
            self.dev.send(bytes.fromhex("010200"))
        t0 = time.monotonic()
        self.assertIsNone(hidpp.read_reply(self.ours.fileno(), 0.2))
        self.assertLess(time.monotonic() - t0, 0.6)

    def test_a_wake_up_with_nothing_to_read_keeps_waiting_for_the_answer(self):
        self.dev.send(ON)
        real, calls = os.read, []

        def read(fd, n):  # the first read finds nothing (EAGAIN) although select said readable
            calls.append(fd)
            if len(calls) == 1:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            return real(fd, n)
        with mock.patch.object(hidpp.os, "read", read):
            self.assertEqual(hidpp.read_reply(self.ours.fileno(), 1), (3900, False))
        self.assertEqual(len(calls), 2)

    def test_the_captured_error_reply_is_off_at_once(self):
        self.dev.send(OFF)
        self.assertIs(hidpp.read_reply(self.ours.fileno(), 5), hidpp.OFF)


class Battery(unittest.TestCase):
    def setUp(self):
        self.ours, self.dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(self.ours.close)
        self.addCleanup(self.dev.close)
        self.fds, self.sent = [], []

    def open_(self, path, flags):
        self.fds.append(os.dup(self.ours.fileno()))
        return self.fds[-1]

    def write(self, fd, data):
        self.sent.append(data)
        return len(data)

    def query(self, **kw):
        return hidpp.battery(**{"find": lambda: "/dev/hidraw0", "open_": self.open_, "write": self.write,
                                "ioctl": PRO_X, "timeout": 0.1, **kw})

    def assertClosed(self):
        self.assertEqual(len(self.fds), 1)
        with self.assertRaises(OSError):
            os.fstat(self.fds[0])

    def test_the_node_is_resolved_on_every_query(self):
        found = []
        hidpp.battery(find=lambda: found.append(1))
        hidpp.battery(find=lambda: found.append(1))
        self.assertEqual(found, [1, 1])

    def test_no_node_is_off(self):  # the dongle is unplugged: nothing could ever answer
        self.assertIs(hidpp.battery(find=lambda: None), hidpp.OFF)

    def test_a_node_that_fails_to_open_is_a_miss(self):  # unplugged after the scan, or permissions tightened
        self.assertIsNone(hidpp.battery(find=lambda: "/nonexistent/hidraw9"))

    def test_the_request_is_written_and_the_reply_read_with_a_timeout(self):
        def opener(path, flags):
            self.assertEqual(path, "/dev/hidraw7")
            self.assertTrue(flags & os.O_NONBLOCK and flags & os.O_RDWR)
            self.dev.send(ON)
            return self.open_(path, flags)
        reading = self.query(find=lambda: "/dev/hidraw7", open_=opener, timeout=1)
        self.assertEqual((reading, self.sent), ((3900, False), [hidpp.REQUEST]))
        self.assertClosed()

    def test_the_opened_fd_is_rechecked_as_the_pro_x_before_the_write(self):  # another device took the number
        requests = []

        def brio(fd, request, buf):
            requests.append((fd, request, len(buf)))
            return info(0x046D, 0x085E)(fd, request, buf)
        self.assertIsNone(self.query(ioctl=brio))
        self.assertEqual((self.sent, requests), ([], [(self.fds[0], 0x80084803, 8)]))  # _IOR('H', 0x03, 8 bytes)
        self.assertClosed()

    def test_no_reply_is_a_miss_and_closes_the_fd(self):
        self.assertIsNone(self.query())
        self.assertEqual(self.sent, [hidpp.REQUEST])
        self.assertClosed()

    def test_an_unplug_mid_query_is_a_miss_and_closes_the_fd(self):  # EIO from the write: the node went away
        def write(fd, data):
            raise OSError(errno.EIO, "Input/output error")
        self.assertIsNone(self.query(write=write))
        self.assertClosed()

    def test_the_captured_error_reply_is_off_not_a_miss(self):
        self.dev.send(OFF)
        self.assertIs(self.query(), hidpp.OFF)
        self.assertClosed()


class Setting(Battery):
    """A settings command takes the battery query's path: the same node checks, one write, the echo read back."""

    def set(self, request, **kw):
        return hidpp.setting(request, **{"find": lambda: "/dev/hidraw0", "open_": self.open_, "write": self.write,
                                         "ioctl": PRO_X, "timeout": 0.1, **kw})

    def test_the_request_is_written_and_its_echo_skips_other_reports(self):
        self.dev.send(ON)  # the battery poll of another Booth: every reader of the node gets it
        self.dev.send(SIDETONE_ACK)
        self.assertIs(self.set(hidpp.sidetone_request(0), timeout=1), True)
        self.assertEqual(self.sent, [hidpp.sidetone_request(0)])
        self.assertClosed()

    def test_the_captured_error_reply_is_not_acknowledged(self):
        self.dev.send(AUTO_OFF_OFF)
        self.assertIs(self.set(hidpp.auto_off_request(30)), False)
        self.assertClosed()

    def test_another_settings_echo_is_not_this_ones(self):
        self.dev.send(SIDETONE_ACK)
        self.assertIsNone(self.set(hidpp.auto_off_request(30)))

    def test_no_reply_is_none_and_no_dongle_is_not_acknowledged(self):
        self.assertIsNone(self.set(hidpp.sidetone_request(0)))
        self.assertClosed()
        self.assertIs(self.set(hidpp.sidetone_request(0), find=lambda: None), False)

    def test_another_device_on_the_number_gets_no_write(self):
        self.assertIsNone(self.set(hidpp.sidetone_request(0), ioctl=info(0x046D, 0x085E)))
        self.assertEqual(self.sent, [])
        self.assertClosed()


if __name__ == "__main__":
    unittest.main()
