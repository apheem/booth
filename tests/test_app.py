"""The launch wiring: Booth finds the BRIO and re-applies the saved controls before the window opens; with
--background it lives with no window and re-applies them at login and on every replug."""
import contextlib
import io
import threading
import unittest
from unittest import mock

from gi.repository import Gio, GLib

from booth import app, camera, service
from booth.ui import window as ui_window
from tests.fixture import read
from tests.test_service import Clock, Timers

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"


class Inline:
    """threading.Thread stand-in that runs its target on start()."""

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


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

    def test_two_restores_never_interleave(self):  # startup, replug and window-open restores run on their own threads
        log, first_in, go = [], threading.Event(), threading.Event()

        def restore(dev, saved):
            log.append(("in", saved))
            if saved == "a":
                first_in.set()
                go.wait(5)
            log.append(("out", saved))
            return []

        with mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(camera, "restore", restore):
            a = threading.Thread(target=app.restore_camera, args=({"camera": "a"},))
            a.start()
            self.assertTrue(first_in.wait(5))
            b = threading.Thread(target=app.restore_camera, args=({"camera": "b"},))
            b.start()
            b.join(0.3)
            self.assertEqual(log, [("in", "a")])
            go.set()
            a.join(5)
            b.join(5)
        self.assertEqual(log, [("in", "a"), ("out", "a"), ("in", "b"), ("out", "b")])


class LaunchOffTheMainLoop(unittest.TestCase):
    def test_restore_and_the_first_read_run_on_a_worker_then_the_window_opens(self):
        threads, idle = [], []
        stub = mock.Mock(window=None, starting=False, background=False, held=False)
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app.service, "run", return_value=True) as is_enabled, \
                mock.patch.object(app, "restore_camera", return_value=DEV) as restore, \
                mock.patch.object(camera, "read_ctrls", return_value={"zoom_absolute": {}}) as read_ctrls:
            app.Application.do_activate(stub)
            restore.assert_not_called()  # nothing touches the camera on the main loop
            read_ctrls.assert_not_called()
            is_enabled.assert_not_called()
            stub.hold.assert_called_once_with()
            app.Application.do_activate(stub)  # activated again while starting: one worker
            self.assertEqual(len(threads), 1)
            threads[0]()
            self.assertEqual(idle, [(stub.show_window, ({"version": 1, "camera": {}}, DEV, {"zoom_absolute": {}}, True))])
        is_enabled.assert_called_once_with(service.IS_ENABLED)  # the switch shows the unit's state as it is now
        restore.assert_called_once_with({"version": 1, "camera": {}})
        read_ctrls.assert_called_once_with(DEV)

    def test_the_background_instance_opens_its_window_without_restoring_again(self):
        threads, idle = [], []
        stub = mock.Mock(window=None, starting=False, background=True, held=True, monitor=mock.Mock())
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app.service, "run", return_value=False), \
                mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(app, "restore_camera") as restore, \
                mock.patch.object(camera, "read_ctrls", return_value={"zoom_absolute": {}}):
            app.Application.do_activate(stub)
            threads[0]()
        restore.assert_not_called()  # it keeps the camera restored itself: a second restore could race a replug's
        stub.watch.assert_not_called()
        self.assertEqual(idle, [(stub.show_window, ({"version": 1, "camera": {}}, DEV, {"zoom_absolute": {}}, False))])

    def test_the_first_read_waits_for_a_restore_already_running(self):
        # At login the background instance's restore may still be writing when the window opens: read after it.
        for held in (True, False):
            stub, idle = mock.Mock(held=held), []
            with mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append(a)), \
                    mock.patch.object(app.devices, "discover", return_value={"camera": DEV}) as discover, \
                    mock.patch.object(camera, "restore", return_value=[]), \
                    mock.patch.object(app.service, "run", return_value=False), \
                    mock.patch.object(camera, "read_ctrls", return_value={"zoom_absolute": {}}):
                with app.RESTORING:  # the other restore
                    worker = threading.Thread(target=app.Application.start, args=(stub, {"camera": {}}), daemon=True)
                    worker.start()
                    worker.join(0.3)
                    discover.assert_not_called()
                worker.join(5)
                self.assertFalse(worker.is_alive(), held)
            self.assertEqual(idle, [({"camera": {}}, DEV, {"zoom_absolute": {}}, False)])

    def test_a_crash_in_the_worker_still_opens_the_window(self):
        threads, idle = [], []
        stub = mock.Mock(window=None, starting=False, background=False, held=False)
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app, "restore_camera", side_effect=RuntimeError("boom")):
            app.Application.do_activate(stub)
            self.assertRaises(RuntimeError, threads[0])
        self.assertEqual(idle, [(stub.show_window, ({"version": 1, "camera": {}}, None, {}, False))])

    def test_activate_again_presents_the_open_window_and_builds_no_second(self):
        threads, stub = [], mock.Mock(window=mock.Mock(), starting=False, background=False)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app, "Window") as window:
            app.Application.do_activate(stub)
        stub.window.present.assert_called_once_with()
        window.assert_not_called()
        stub.hold.assert_not_called()
        self.assertEqual(threads, [])

    def test_a_window_that_fails_to_build_releases_the_app_and_prints_why(self):
        stub, err = mock.Mock(window=None, starting=True), io.StringIO()
        with mock.patch.object(app, "Window", side_effect=RuntimeError("no display")), contextlib.redirect_stderr(err):
            self.assertEqual(app.Application.show_window(stub, {"camera": {}}, None, {}, False), app.GLib.SOURCE_REMOVE)
        stub.release.assert_called_once_with()
        self.assertIs(stub.starting, False)  # the next launch's activate tries again
        self.assertIsNone(stub.window)
        self.assertIn("no display", err.getvalue())

    def test_the_window_shows_the_switch_state_and_forgets_itself_when_closed(self):
        for enabled in (False, True):
            stub = mock.Mock(window=None, starting=True)
            with mock.patch.object(app, "Window") as window:
                app.Application.show_window(stub, {"camera": {}}, None, {}, enabled)
            self.assertIs(stub.run_in_background.set_state.call_args.args[0].get_boolean(), enabled)
        window.return_value.connect.assert_called_once_with("close-request", stub.on_close)
        self.assertIs(app.Application.on_close(stub, stub.window), False)  # False: the window does close
        self.assertIsNone(stub.window)  # so the next activate builds a new one instead of presenting a dead one


def options(background):
    opts = GLib.VariantDict.new(None)
    if background:
        opts.insert_value("background", GLib.Variant.new_boolean(True))
    return opts


class BackgroundLaunch(unittest.TestCase):
    def test_a_plain_launch_registers_and_activates_as_usual(self):
        # GApplication then activates here, or in the running instance over D-Bus: it shows its window there.
        stub = mock.Mock(background=False)
        self.assertEqual(app.Application.do_handle_local_options(stub, options(False)), -1)
        stub.register.assert_not_called()
        self.assertIs(stub.background, False)

    def test_background_launch_becomes_the_background_instance(self):
        stub = mock.Mock(background=False)
        stub.get_is_remote.return_value = False
        self.assertEqual(app.Application.do_handle_local_options(stub, options(True)), -1)
        stub.register.assert_called_once_with(None)
        self.assertIs(stub.background, True)

    def test_background_launch_while_booth_runs_exits_cleanly_so_the_service_never_loops(self):
        # The running instance already restores on replug; a failure here would restart every RestartSec.
        stub, err = mock.Mock(background=False), io.StringIO()
        stub.get_is_remote.return_value = True
        with contextlib.redirect_stderr(err):
            self.assertEqual(app.Application.do_handle_local_options(stub, options(True)), 0)
        self.assertIs(stub.background, False)
        stub.activate.assert_not_called()  # the running instance opens no window for it
        self.assertIn("already running", err.getvalue())

    def test_the_background_activate_watches_and_opens_no_window(self):
        threads, stub = [], mock.Mock(background=True, monitor=None, window=None, starting=False)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app, "Window") as window:
            app.Application.do_activate(stub)
        stub.watch.assert_called_once_with()
        window.assert_not_called()
        self.assertEqual(threads, [])

    def test_a_window_instance_also_watches_then_opens_its_window(self):
        threads, stub = [], mock.Mock(background=False, monitor=None, window=None, starting=False)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}):
            app.Application.do_activate(stub)
        stub.watch.assert_called_once_with()  # opened from the app grid, it still restores on replug
        self.assertEqual(len(threads), 1)  # and start(): the launch restore, then the window

    def watch(self, background):
        stub = mock.Mock(held=False, background=background)
        with mock.patch.object(app.Gio.File, "new_for_path") as new_for_path:
            app.Application.watch(stub)
        new_for_path.assert_called_once_with("/dev/v4l/by-id")
        monitor = new_for_path.return_value.monitor_directory.return_value
        self.assertIs(stub.monitor, monitor)
        monitor.connect.assert_called_once_with("changed", stub.on_by_id)
        self.assertIsInstance(stub.replug, service.Debounce)
        return stub

    def test_watch_in_the_background_instance_holds_for_good_and_restores_now(self):
        stub = self.watch(background=True)
        stub.hold.assert_called_once_with()  # no window, and closing one later never quits
        self.assertIs(stub.held, True)
        stub.restore_saved.assert_called_once_with()  # the restore at login

    def test_watch_in_a_window_instance_leaves_the_launch_restore_to_start(self):
        stub = self.watch(background=False)
        stub.hold.assert_not_called()  # it lives with its window unless Run in Background is switched on
        self.assertIs(stub.held, False)
        stub.restore_saved.assert_not_called()


class Replug(unittest.TestCase):
    SAVED = {"version": 1, "camera": {"zoom_absolute": 250, "exposure_time_absolute": 300, "auto_exposure": 1}}

    def event(self, stub, file, other, event):
        app.Application.on_by_id(stub, None, Gio.File.new_for_path(file), other and Gio.File.new_for_path(other), event)

    def test_ten_fast_replugs_restore_the_current_saved_state_once_auto_modes_first(self):
        timers, ran, stub = Timers(), [], mock.Mock(window=None)
        stub.restore_saved = lambda: app.Application.restore_saved(stub)
        stub.replug = service.Debounce(app.REPLUG_MS, stub.restore_saved, timers.add, timers.remove)

        def run(cmd):
            ran.append(cmd[-1])
            return True

        with mock.patch.object(app.threading, "Thread", Inline), \
                mock.patch.object(app.store, "load", return_value=self.SAVED) as load, \
                mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(camera, "read_ctrls", return_value=camera.parse_ctrls(read("brio/ctrls-index0.txt"))), \
                mock.patch.object(camera, "run", run):
            for _ in range(5):
                self.event(stub, "/dev/v4l/by-id", None, Gio.FileMonitorEvent.CREATED)  # the folder came back
                self.event(stub, "/dev/v4l/by-id/.#tmp", DEV, Gio.FileMonitorEvent.RENAMED)  # udev's link
                self.event(stub, DEV, None, Gio.FileMonitorEvent.DELETED)  # an unplug restores nothing
            load.assert_not_called()
            self.assertEqual(ran, [])
            timers.fire()
        load.assert_called_once_with()  # read when the restore runs, so it is the saved state as it is then
        self.assertEqual(ran, ["--set-ctrl=auto_exposure=1", "--set-ctrl=zoom_absolute=250",
                               "--set-ctrl=exposure_time_absolute=300"])
        self.assertEqual(timers.pending, {})

    def test_udevs_rename_alone_restores(self):  # another camera keeps by-id present: no folder event comes
        stub = mock.Mock()
        self.event(stub, "/dev/v4l/by-id/.#tmp", DEV, Gio.FileMonitorEvent.RENAMED)
        stub.replug.poke.assert_called_once_with()

    def test_a_burst_inside_the_debounce_window_restores_once(self):
        clock, restores, stub = Clock(), [], mock.Mock()
        stub.replug = service.Debounce(app.REPLUG_MS, lambda: restores.append(clock.now), clock.add, clock.remove)
        for _ in range(10):  # a flapping cable: an index0 link every 150 ms
            self.event(stub, "/dev/v4l/by-id/.#tmp", DEV, Gio.FileMonitorEvent.RENAMED)
            clock.advance(150)
        clock.advance(10_000)
        self.assertEqual(restores, [1350 + app.REPLUG_MS])  # once, after the last event
        self.assertGreaterEqual(app.REPLUG_MS, 1000)  # long enough to span a replug's burst of udev events

    def test_with_the_window_open_the_page_restores_behind_its_own_writes(self):
        stub = mock.Mock()
        stub.window.camera.ctrls = {camera.ZOOM: {}}
        with mock.patch.object(app.threading, "Thread") as thread:
            app.Application.restore_saved(stub)
        stub.window.camera.reapply.assert_called_once_with()  # one writer: no thread races the page's Setter
        thread.assert_not_called()

    def test_with_no_window_or_no_camera_on_the_page_a_worker_restores(self):
        for stub in (mock.Mock(window=None), mock.Mock()):
            if stub.window:
                stub.window.camera.ctrls = {}  # the page opened while the BRIO was unplugged: it can't write
            with mock.patch.object(app.threading, "Thread", Inline), \
                    mock.patch.object(app, "restore_camera") as restore, \
                    mock.patch.object(app.store, "load", return_value=self.SAVED):
                app.Application.restore_saved(stub)
            restore.assert_called_once_with(self.SAVED)
            if stub.window:
                stub.window.camera.reapply.assert_not_called()

    def test_an_ir_node_or_unplug_event_never_restores(self):
        stub = mock.Mock()
        self.event(stub, DEV.replace("index0", "index2"), None, Gio.FileMonitorEvent.CREATED)
        self.event(stub, DEV, None, Gio.FileMonitorEvent.DELETED)
        stub.replug.poke.assert_not_called()


class RunInBackgroundSwitch(unittest.TestCase):
    def flip(self, stub, on, ok):
        idle, ran = [], []
        with mock.patch.object(app.threading, "Thread", Inline), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.service, "run", lambda cmd: ran.append(cmd) or ok):
            app.Application.on_background(stub, stub.action, GLib.Variant.new_boolean(on))
        return ran, idle

    def test_the_command_runs_off_the_main_loop_and_reports_back(self):
        for background in (False, True):
            stub = mock.Mock(background=background)
            ran, idle = self.flip(stub, False, True)
            self.assertEqual(ran, [service.switch(False)])
            self.assertEqual(idle, [(stub.switched, (stub.action, False, True))])

    def switched(self, background, held, on, ok=True):
        stub, err = mock.Mock(background=background, held=held), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(app.Application.switched(stub, stub.action, on, ok), GLib.SOURCE_REMOVE)
        return stub, err.getvalue()

    def test_off_inside_the_service_lets_go_so_it_exits_when_the_window_closes(self):
        stub, _ = self.switched(background=True, held=True, on=False)
        self.assertIs(stub.action.set_state.call_args.args[0].get_boolean(), False)
        stub.release.assert_called_once_with()
        stub.hold.assert_not_called()
        self.assertIs(stub.held, False)

    def test_on_again_inside_the_service_holds_again(self):
        stub, _ = self.switched(background=True, held=False, on=True)
        self.assertIs(stub.action.set_state.call_args.args[0].get_boolean(), True)
        stub.hold.assert_called_once_with()
        self.assertIs(stub.held, True)

    def test_on_in_a_window_instance_holds_it_so_it_outlives_its_window(self):
        stub, _ = self.switched(background=False, held=False, on=True)
        self.assertIs(stub.action.set_state.call_args.args[0].get_boolean(), True)
        stub.hold.assert_called_once_with()  # it goes on as the background instance; the unit takes over at login
        self.assertIs(stub.held, True)

    def test_off_in_a_window_instance_lets_go(self):
        stub, _ = self.switched(background=False, held=True, on=False)
        stub.release.assert_called_once_with()
        self.assertIs(stub.held, False)

    def test_the_same_state_again_neither_holds_nor_releases(self):
        for on in (False, True):
            stub, _ = self.switched(background=False, held=on, on=on)
            stub.hold.assert_not_called()
            stub.release.assert_not_called()

    def test_a_failed_command_leaves_the_switch_as_it_was_and_says_so(self):
        stub, err = self.switched(background=True, held=True, on=False, ok=False)
        stub.action.set_state.assert_not_called()
        stub.release.assert_not_called()
        self.assertIn("booth.service", err)


class QuitAction(unittest.TestCase):
    def test_app_quit_quits_even_a_held_app(self):  # install.sh asks a running Booth to quit before it restarts the unit
        booth = app.Application()
        booth.hold()
        with mock.patch.object(booth, "quit") as quit_:
            booth.lookup_action("quit").activate(None)
        quit_.assert_called_once_with()  # GApplication.quit ignores the hold count; release() would not


class MenuWiring(unittest.TestCase):
    def test_the_menu_item_names_an_action_the_app_registers(self):
        menu = ui_window.main_menu()
        name = menu.get_item_attribute_value(0, "action", GLib.VariantType.new("s")).get_string()
        self.assertTrue(name.startswith("app."), name)
        action = app.Application().lookup_action(name.removeprefix("app."))
        self.assertIsNotNone(action)
        self.assertEqual(action.get_state().get_type_string(), "b")  # a check item


if __name__ == "__main__":
    unittest.main()
