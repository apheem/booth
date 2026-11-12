import subprocess
import unittest
from unittest import mock

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


class Commands(unittest.TestCase):
    def test_run_reports_the_exit_status(self):
        self.assertIs(camera.run(["true"]), True)
        self.assertIs(camera.run(["false"]), False)
        self.assertIs(camera.run(["/nonexistent/v4l2-ctl"]), False)

    def test_read_ctrls_asks_for_controls_with_menus(self):
        out = subprocess.CompletedProcess([], 0, stdout=read("brio/ctrls-index0.txt"))
        with mock.patch("booth.camera.subprocess.run", return_value=out) as run:
            ctrls = camera.read_ctrls(DEV)
        self.assertEqual(run.call_args.args[0], ["v4l2-ctl", "-d", DEV, "--list-ctrls-menus"])
        self.assertEqual(ctrls["power_line_frequency"]["menu"], {0: "Disabled", 1: "50 Hz", 2: "60 Hz"})

    def test_read_ctrls_unreadable_camera(self):
        with mock.patch("booth.camera.subprocess.run", side_effect=subprocess.TimeoutExpired("v4l2-ctl", 5)):
            self.assertEqual(camera.read_ctrls(DEV), {})


class Restore(unittest.TestCase):
    """camera.restore: what Booth re-applies at launch."""

    def restore(self, device, saved, accept=True):
        ran = []

        def run(cmd):
            ran.append(cmd)
            return accept

        with mock.patch.object(camera, "read_ctrls", return_value=camera.parse_ctrls(read("brio/ctrls-index0.txt"))), \
                mock.patch.object(camera, "run", run):
            return camera.restore(device, saved), ran

    def test_applies_saved_controls_clamped(self):
        failed, ran = self.restore(DEV, {"zoom_absolute": 900})
        self.assertEqual(ran, [["v4l2-ctl", "-d", DEV, "--set-ctrl=zoom_absolute=500"]])
        self.assertEqual(failed, [])

    def test_reports_what_the_camera_refused(self):
        failed, _ = self.restore(DEV, {"zoom_absolute": 250}, accept=False)
        self.assertEqual(failed, [["v4l2-ctl", "-d", DEV, "--set-ctrl=zoom_absolute=250"]])

    def test_no_camera_runs_nothing(self):
        self.assertEqual(self.restore(None, {"zoom_absolute": 250}), ([], []))


class FakeSpawn:
    """Holds each spawned command until the test finishes it, like a v4l2-ctl still running on its thread."""

    def __init__(self):
        self.cmds, self.done = [], []

    def __call__(self, cmd, done):
        self.cmds.append(cmd)
        self.done.append(done)

    def finish(self, ok):
        self.done.pop(0)(ok)


def zoom(v):
    return ["v4l2-ctl", "-d", DEV, f"--set-ctrl=zoom_absolute={v}"]


class SetterTest(unittest.TestCase):
    def setUp(self):
        self.settings = {"version": 1, "camera": {}}
        self.saved = []
        self.spawn = FakeSpawn()
        self.setter = camera.Setter(self.settings, DEV, "zoom_absolute", self.spawn,
                                    lambda s: self.saved.append(dict(s["camera"])))

    def test_one_set_in_flight_and_the_last_value_wins(self):
        for v in (200, 300, 400):
            self.setter.set(v)
        self.assertEqual(self.spawn.cmds, [zoom(200)])
        self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [zoom(200), zoom(400)])
        self.spawn.finish(True)
        self.assertEqual(self.spawn.done, [])
        self.assertEqual(self.saved, [{"zoom_absolute": 200}, {"zoom_absolute": 400}])
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 400})

    def test_a_refused_value_is_not_saved(self):
        self.setter.set(250)
        self.spawn.finish(False)
        self.assertEqual((self.saved, self.settings["camera"]), ([], {}))
        self.setter.set(260)  # a failure doesn't wedge the setter
        self.spawn.finish(True)
        self.assertEqual(self.saved, [{"zoom_absolute": 260}])

    def test_a_failed_save_does_not_wedge_the_setter(self):
        def save(settings):
            raise OSError(28, "No space left on device")

        self.setter.save = save
        self.setter.set(250)
        with self.assertRaises(OSError):  # surfaced to the page, which says so
            self.spawn.finish(True)
        self.setter.set(300)
        self.assertEqual(self.spawn.cmds, [zoom(250), zoom(300)])


if __name__ == "__main__":
    unittest.main()
