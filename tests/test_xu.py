"""The BRIO's extension-unit controls: descriptor parsing, payloads, and the read/write paths over a fake ioctl."""
import ctypes
import unittest
import uuid
from unittest import mock

from booth import xu
from tests.fixture import FIXTURES

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"
FOV_GUID = "49e40215-f434-47fe-b158-0e885023e51b"
LED_GUID = "ffe52d21-8030-4e2c-82d9-f587d00540bd"


def desc():
    """The BRIO's raw USB config descriptor as sysfs gives it (no serial: that lives in a string descriptor)."""
    return (FIXTURES / "brio/descriptors.bin").read_bytes()


def with_unit(data, guid, unit):
    """`data` with the extension unit of `guid` renumbered to `unit`."""
    at = data.index(uuid.UUID(guid).bytes_le) - 4
    return data[:at + 3] + bytes([unit]) + data[at + 4:]


class Descriptors(unittest.TestCase):
    def test_units_by_guid_with_their_selectors(self):
        units = xu.units(desc())
        self.assertEqual(units[FOV_GUID], (10, {2, 4, 5, 6, 8, 9, 10, 11, 12, 14, 15}))
        self.assertEqual(units[LED_GUID], (11, {9, 15, 17}))
        self.assertEqual(len(units), 7)

    def test_only_the_video_control_interface_counts(self):
        # 0x24/0x06 also starts an MJPEG format (streaming) and an audio descriptor: neither is an extension unit
        self.assertNotIn("00030101-0409-0004-0001-020000090404", xu.units(desc()))
        self.assertNotIn("00010114-0000-4200-2407-01008002e001", xu.units(desc()))

    def test_an_extension_unit_shape_outside_video_control_is_ignored(self):
        xu_desc = desc()[desc().index(uuid.UUID(FOV_GUID).bytes_le) - 4:]
        xu_desc = xu_desc[:xu_desc[0]]
        iface = bytes([9, 0x04, 1, 0, 0, 0x0E, 0x01, 0, 0])  # interface descriptor: class, subclass at bytes 5, 6
        self.assertIn(FOV_GUID, xu.units(iface + xu_desc))
        streaming = iface[:6] + b"\x02" + iface[7:]  # a VideoStreaming interface: 0x24/0x06 there is a format
        self.assertEqual(xu.units(streaming + xu_desc), {})
        self.assertEqual(xu.units(xu_desc), {})  # before any interface
        self.assertEqual(xu.units(iface + xu_desc[:24]), {})  # cut short: skipped, not an IndexError

    def test_unit_id_is_discovered_never_hardcoded(self):
        self.assertEqual(xu.find(desc(), "fov"), 10)
        self.assertEqual(xu.find(with_unit(desc(), FOV_GUID, 13), "fov"), 13)
        self.assertEqual(xu.find(with_unit(desc(), LED_GUID, 3), "led_mode"), 3)

    def test_missing_unit_or_selector(self):
        no_led = desc().replace(uuid.UUID(LED_GUID).bytes_le, bytes(16))
        self.assertIsNone(xu.find(no_led, "led_mode"))
        self.assertEqual(xu.find(no_led, "fov"), 10)
        with mock.patch.dict(xu.CONTROLS, fov=(FOV_GUID, 0x03, 1, 0)):  # selector 3 isn't in unit 10's bmControls
            self.assertIsNone(xu.find(desc(), "fov"))
        self.assertEqual(xu.units(b""), {})

    def test_descriptors_are_read_from_the_usb_device_behind_the_node(self):
        with mock.patch("os.path.realpath", return_value="/dev/video0"), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"\x12\x01")) as opened:
            self.assertEqual(xu.descriptors(DEV), b"\x12\x01")
        self.assertEqual(opened.call_args.args, ("/sys/class/video4linux/video0/device/../descriptors", "rb"))


class Payloads(unittest.TestCase):
    def test_fov(self):
        self.assertEqual(xu.MENUS["fov"], {0: "90°", 1: "78°", 2: "65°"})
        self.assertEqual(xu.CONTROLS["fov"], (FOV_GUID, 0x05, 1, 0))
        self.assertEqual(xu.decode("fov", b"\x02"), 2)
        self.assertEqual(xu.encode("fov", b"\x02", 1), b"\x01")

    def test_led_mode_is_byte_1_and_the_rest_is_kept(self):
        self.assertEqual(xu.CONTROLS["led_mode"], (LED_GUID, 0x09, 5, 1))
        self.assertEqual(xu.MENUS["led_mode"], {0: "Off", 1: "On", 2: "Blink", 3: "Auto"})
        self.assertEqual(xu.decode("led_mode", bytes.fromhex("0103010002")), 3)
        self.assertEqual(xu.encode("led_mode", bytes.fromhex("0103010002"), 0), bytes.fromhex("0100010002"))

    def test_the_ioctl_request(self):
        self.assertEqual(xu.CTRL_QUERY, 0xC0000000 | ctypes.sizeof(xu.Query) << 16 | 0x7521)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual((ctypes.sizeof(xu.Query), xu.CTRL_QUERY), (16, 0xC0107521))
        self.assertEqual((xu.SET_CUR, xu.GET_CUR, xu.GET_DEF), (0x01, 0x81, 0x87))


class FakeCamera:
    """The XU side of a BRIO: {(unit, selector): payload}; GET_DEF is all zeros, like the BRIO's LED."""

    def __init__(self, cur):
        self.cur, self.calls = dict(cur), []

    def __call__(self, unit, selector, query, data):
        self.calls.append((unit, selector, query, bytes(data)))
        if (unit, selector) not in self.cur:
            raise OSError(2, "No such file or directory")
        cur = self.cur[(unit, selector)]
        if query == xu.SET_CUR:
            self.cur[(unit, selector)] = bytes(data)
            return bytes(data)
        if len(data) != len(cur):  # uvcvideo checks the size against the camera's GET_LEN
            raise OSError(105, "No buffer space available")
        return cur if query == xu.GET_CUR else bytes(len(cur))


class Model(unittest.TestCase):
    def test_current_values_are_read_back(self):
        cam = FakeCamera({(10, 5): b"\x02", (11, 9): bytes.fromhex("0103010002")})
        ctrls = xu.model(desc(), cam)
        self.assertEqual(ctrls["fov"], {"type": "menu", "inactive": False, "min": 0, "max": 2, "step": 1,
                                        "default": 0, "value": 2, "menu": {0: "90°", 1: "78°", 2: "65°"}})
        self.assertEqual((ctrls["led_mode"]["value"], ctrls["led_mode"]["default"]), (3, 3))  # Auto, its power-on mode
        self.assertEqual((ctrls["led_mode"]["min"], ctrls["led_mode"]["max"]), (0, 3))
        self.assertIn((10, 5, xu.GET_CUR, b"\x00"), cam.calls)

    def test_a_control_the_camera_does_not_answer_is_left_out(self):
        self.assertEqual(list(xu.model(desc(), FakeCamera({(10, 5): b"\x01"}))), ["fov"])

    def test_a_control_of_another_length_is_left_out(self):
        self.assertEqual(list(xu.model(desc(), FakeCamera({(10, 5): b"\x01", (11, 9): b"\x01\x02"}))), ["fov"])

    def test_no_extension_unit_no_rows(self):
        cam = FakeCamera({(10, 5): b"\x01", (11, 9): bytes(5)})
        self.assertEqual(xu.model(b"", cam), {})
        self.assertEqual(cam.calls, [])


class ReadWrite(unittest.TestCase):
    def live(self, cam):
        return mock.patch.multiple(xu, descriptors=mock.Mock(return_value=desc()),
                                   ioctl=lambda fd, *a: cam(*a)), \
            mock.patch.multiple(xu.os, open=mock.Mock(return_value=7), close=mock.Mock())

    def test_write_keeps_the_rest_of_the_payload(self):
        cam = FakeCamera({(10, 5): b"\x02", (11, 9): bytes.fromhex("0103010002")})
        a, b = self.live(cam)
        with a, b:
            self.assertTrue(xu.write(DEV, "led_mode", 0))
            self.assertTrue(xu.write(DEV, "fov", 1))
            xu.os.open.assert_called_with(DEV, xu.os.O_RDWR)
        self.assertEqual(cam.cur, {(10, 5): b"\x01", (11, 9): bytes.fromhex("0100010002")})
        self.assertEqual([c[2] for c in cam.calls], [xu.GET_CUR, xu.SET_CUR] * 2)

    def test_write_to_a_camera_without_the_control(self):
        a, b = self.live(FakeCamera({}))
        with a, b, mock.patch.object(xu, "descriptors", return_value=b""):
            self.assertFalse(xu.write(DEV, "fov", 1))

    def test_read_opens_the_node_and_closes_it(self):
        a, b = self.live(FakeCamera({(10, 5): b"\x00"}))
        with a, b:
            self.assertEqual(xu.read(DEV)["fov"]["value"], 0)
            xu.os.close.assert_called_once_with(7)

    def test_read_an_unplugged_camera(self):
        with mock.patch.object(xu, "descriptors", side_effect=FileNotFoundError):
            self.assertEqual(xu.read(DEV), {})


if __name__ == "__main__":
    unittest.main()
