import unittest

from booth import camera
from tests.fixture import read

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"


class ParseControls(unittest.TestCase):
    def setUp(self):
        self.ctrls = camera.parse_ctrls(read("brio/ctrls-index0.txt"))

    def test_zoom_range_comes_from_the_listing(self):
        zoom = self.ctrls["zoom_absolute"]
        self.assertEqual((zoom["min"], zoom["max"], zoom["step"], zoom["default"], zoom["value"]),
                         (100, 500, 1, 100, 100))
        self.assertEqual(zoom["type"], "int")

    def test_every_control_is_parsed(self):
        self.assertEqual(len(self.ctrls), 17)
        self.assertEqual(self.ctrls["pan_absolute"]["min"], -36000)
        self.assertEqual(self.ctrls["pan_absolute"]["step"], 3600)

    def test_bool_has_no_range(self):
        self.assertEqual(self.ctrls["white_balance_automatic"], {"type": "bool", "inactive": False, "default": 1, "value": 1})

    def test_menus(self):
        self.assertEqual(self.ctrls["power_line_frequency"]["menu"], {0: "Disabled", 1: "50 Hz", 2: "60 Hz"})
        self.assertEqual(self.ctrls["auto_exposure"]["menu"], {1: "Manual Mode", 3: "Aperture Priority Mode"})
        self.assertEqual(self.ctrls["auto_exposure"]["value"], 3)

    def test_inactive_flag(self):
        self.assertTrue(self.ctrls["focus_absolute"]["inactive"])
        self.assertFalse(self.ctrls["zoom_absolute"]["inactive"])

    def test_empty_listing(self):
        self.assertEqual(camera.parse_ctrls(""), {})


class SetCommands(unittest.TestCase):
    def setUp(self):
        self.ctrls = camera.parse_ctrls(read("brio/ctrls-index0.txt"))

    def test_set_command(self):
        self.assertEqual(camera.set_cmd(DEV, "zoom_absolute", 250),
                         ["v4l2-ctl", "-d", DEV, "--set-ctrl=zoom_absolute=250"])

    def test_clamp_to_device_range_and_step(self):
        zoom, pan = self.ctrls["zoom_absolute"], self.ctrls["pan_absolute"]
        self.assertEqual(camera.clamp(zoom, 900), 500)
        self.assertEqual(camera.clamp(zoom, 3), 100)
        self.assertEqual(camera.clamp(zoom, 250.6), 251)
        self.assertEqual(camera.clamp(pan, 5000), 3600)
        self.assertEqual(camera.clamp(pan, -36000), -36000)

    def test_restore_applies_saved_known_controls_clamped(self):
        saved = {"zoom_absolute": 900, "not_on_this_camera": 4, "white_balance_automatic": 0}
        self.assertEqual(camera.restore_cmds(DEV, self.ctrls, saved),
                         [["v4l2-ctl", "-d", DEV, "--set-ctrl=zoom_absolute=500"],
                          ["v4l2-ctl", "-d", DEV, "--set-ctrl=white_balance_automatic=0"]])

    def test_restore_nothing_saved(self):
        self.assertEqual(camera.restore_cmds(DEV, self.ctrls, {}), [])


if __name__ == "__main__":
    unittest.main()
