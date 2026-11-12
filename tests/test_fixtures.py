"""The public repo must never hold a real device serial: every serial in every fixture is the one shared fake."""
import re
import unittest

from tests.fixture import FIXTURES, FAKE_SERIAL

# udev `ID_SERIAL=…`/`ID_SERIAL_SHORT=…`, pw-dump `"device.serial": "…"`, and serials embedded in v4l by-id names.
_SERIALS = [re.compile(r"(?i)serial\w*[\"']?\s*[=:]\s*[\"']?([\w.-]+)"),
            re.compile(r"usb-\w+?_([0-9A-Za-z]+)-video-index")]


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

    def test_detector_accepts_the_fake(self):
        self.assertEqual(foreign(f"ID_SERIAL=046d_Logitech_BRIO_{FAKE_SERIAL}\nID_SERIAL_SHORT={FAKE_SERIAL}\n"
                                 f"usb-046d_Logitech_BRIO_{FAKE_SERIAL}-video-index2"), [])


if __name__ == "__main__":
    unittest.main()
