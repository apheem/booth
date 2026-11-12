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


class LaunchOffTheMainLoop(unittest.TestCase):
    def test_restore_and_the_first_read_run_on_a_worker_then_the_window_opens(self):
        threads, idle = [], []
        stub = mock.Mock(window=None, starting=False)
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app, "restore_camera", return_value=DEV) as restore, \
                mock.patch.object(camera, "read_ctrls", return_value={"zoom_absolute": {}}) as read_ctrls:
            app.Application.do_activate(stub)
            restore.assert_not_called()  # nothing touches the camera on the main loop
            read_ctrls.assert_not_called()
            stub.hold.assert_called_once_with()
            app.Application.do_activate(stub)  # activated again while starting: one worker
            self.assertEqual(len(threads), 1)
            threads[0]()
            self.assertEqual(idle, [(stub.show_window, ({"version": 1, "camera": {}}, DEV, {"zoom_absolute": {}}))])
        restore.assert_called_once_with({"version": 1, "camera": {}})
        read_ctrls.assert_called_once_with(DEV)

    def test_a_crash_in_the_worker_still_opens_the_window(self):
        threads, idle = [], []
        stub = mock.Mock(window=None, starting=False)
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app, "restore_camera", side_effect=RuntimeError("boom")):
            app.Application.do_activate(stub)
            self.assertRaises(RuntimeError, threads[0])
        self.assertEqual(idle, [(stub.show_window, ({"version": 1, "camera": {}}, None, {}))])


if __name__ == "__main__":
    unittest.main()
