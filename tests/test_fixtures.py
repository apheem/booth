"""The public repo must never hold a real device serial: every serial in every fixture is the one shared fake."""
import re
import unittest

from tests.fixture import FIXTURES, FAKE_SERIAL

# udev `ID_SERIAL=…`/`ID_SERIAL_SHORT=…`, pw-dump `"device.serial": "…"` (not PipeWire's own counter "object.serial"),
# serials embedded in v4l by-id names, and in ALSA and PipeWire names (`usb-<vendor>_<model>_<serial>-00`: a serial has
# a digit, so the PRO X's `…_Gaming_Headset-00`, which has none, is not one).
_SERIALS = [re.compile(r"(?i)(?<!object\.)serial\w*[\"']?\s*[=:]\s*[\"']?([\w.-]+)"),
            re.compile(r"usb-\w+?_([0-9A-Za-z]+)-video-index"),
            re.compile(r"usb-\w+?_([0-9A-Za-z]*[0-9][0-9A-Za-z]*)-[0-9][0-9]\b")]


def serials(text):
    return [m for rx in _SERIALS for m in rx.findall(text)]


def foreign(text):
    """Serials in `text` that are not the fake (a full udev ID_SERIAL ends with the short serial)."""
    return [s for s in serials(text) if not s.endswith(FAKE_SERIAL)]


class FixtureSerials(unittest.TestCase):
    def test_no_fixture_holds_another_serial(self):
        files = [p for p in FIXTURES.rglob("*") if p.is_file()]
        self.assertTrue(files)
        found = 0
        for p in files:
            text = p.read_text(errors="replace")
            found += len(serials(text))
            self.assertEqual(foreign(text), [], p.relative_to(FIXTURES))
        self.assertGreater(found, 0, "the serial patterns matched nothing: the check is blind")

    def test_detector_catches_a_real_looking_serial(self):
        self.assertEqual(foreign("ID_SERIAL_SHORT=1B2C3D4E"), ["1B2C3D4E"])
        self.assertEqual(foreign("ID_SERIAL=046d_Logitech_BRIO_1B2C3D4E"), ["046d_Logitech_BRIO_1B2C3D4E"])
        self.assertEqual(foreign('"device.serial": "Focusrite_Scarlett_S9XYZ"'), ["Focusrite_Scarlett_S9XYZ"])
        self.assertEqual(foreign("/dev/v4l/by-id/usb-046d_Logitech_BRIO_1B2C3D4E-video-index0"), ["1B2C3D4E"])
        self.assertEqual(foreign("alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_S9XYZ123-00.analog-stereo"),
                         ["S9XYZ123"])
        self.assertEqual(foreign("alsa_input.usb-046d_Logitech_BRIO_1B2C3D4E-03.analog-stereo"), ["1B2C3D4E"])

    def test_pipewire_object_serials_and_the_pro_x_name_are_not_device_serials(self):
        self.assertEqual(serials('"object.serial": 4730,\nalsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.iec958'),
                         [])

    def test_detector_accepts_the_fake(self):
        self.assertEqual(foreign(f"ID_SERIAL=046d_Logitech_BRIO_{FAKE_SERIAL}\nID_SERIAL_SHORT={FAKE_SERIAL}\n"
                                 f"usb-046d_Logitech_BRIO_{FAKE_SERIAL}-video-index2\n"
                                 f"alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_{FAKE_SERIAL}-00.analog-stereo"), [])


if __name__ == "__main__":
    unittest.main()
