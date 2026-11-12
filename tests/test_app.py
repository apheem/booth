"""The launch wiring: Booth finds the BRIO and re-applies the saved controls before the window opens."""
import contextlib
import io
import unittest
from unittest import mock

from booth import app, camera
from tests.fixture import read

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"


class RestoreAtLaunch(unittest.TestCase):
    def launch(self, node, accept=True):
        ran, err = [], io.StringIO()

        def run(cmd):
            ran.append(cmd[-1])
            return accept

        with mock.patch.object(app.devices, "discover", return_value={"camera": node}), \
                mock.patch.object(camera, "read_ctrls", return_value=camera.parse_ctrls(read("brio/ctrls-index0.txt"))), \
                mock.patch.object(camera, "run", run), contextlib.redirect_stderr(err):
            found = app.restore_camera({"version": 1, "camera": {"zoom_absolute": 250}})
        return found, ran, err.getvalue()

    def test_saved_zoom_is_applied_at_launch(self):
        self.assertEqual(self.launch(DEV), (DEV, ["--set-ctrl=zoom_absolute=250"], ""))

    def test_a_refused_restore_is_reported(self):
        _, _, err = self.launch(DEV, accept=False)
        self.assertIn("zoom_absolute=250", err)

    def test_unplugged_camera(self):
        self.assertEqual(self.launch(None), (None, [], ""))


if __name__ == "__main__":
    unittest.main()
