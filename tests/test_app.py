"""The launch wiring: Booth finds the BRIO and re-applies the saved controls before the window opens; with
--background it lives with no window and re-applies them at login and on every replug."""
import contextlib
import io
import os
import signal
import tempfile
import threading
import unittest
from unittest import mock

from gi.repository import Gio, GLib

from booth import app, camera, routing, service, store, voice
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
            saved = next(iter(saved))
            log.append(("in", saved))
            if saved == "a":
                first_in.set()
                go.wait(5)
            log.append(("out", saved))
            return []

        with mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(camera, "restore", restore):
            a = threading.Thread(target=app.restore_camera, args=({"camera": {"a": 1}},))
            a.start()
            self.assertTrue(first_in.wait(5))
            b = threading.Thread(target=app.restore_camera, args=({"camera": {"b": 1}},))
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
        stub = mock.Mock(window=None, starting=False, background=False, held=False, settings={"version": 1, "camera": {}})
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
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
        stub = mock.Mock(window=None, starting=False, background=True, held=True, monitor=mock.Mock(), settings={"version": 1, "camera": {}})
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
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
        stub = mock.Mock(window=None, starting=False, background=False, held=False, settings={"version": 1, "camera": {}})
        stub.start = lambda settings: app.Application.start(stub, settings)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
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

    def test_a_reopened_window_never_drops_a_value_the_closed_one_saved_after(self):
        # A held Booth: window 1 closes with zoom 300 on the camera (saved later, on the main loop), the window opens
        # again, and the user changes brightness. The last save must keep zoom 300.
        from tests.test_camera import FakeSpawn

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spawn = FakeSpawn()
        stub = mock.Mock(window=None, starting=False, background=False, held=True, monitor=None, closed_page=None)
        stub.watch = lambda: setattr(stub, "monitor", mock.Mock())
        stub.start = lambda settings: app.Application.start(stub, settings)
        stub.show_window = lambda *a: app.Application.show_window(stub, *a)

        def window(_app, settings, node, ctrls):  # the Camera page's Setter, as CameraPage builds it
            return mock.Mock(camera=mock.Mock(ctrls=ctrls, setter=camera.Setter(settings, node, spawn, store.save, {})))

        with mock.patch.dict(os.environ, XDG_CONFIG_HOME=tmp.name), \
                mock.patch.object(app.threading, "Thread", Inline), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: f(*a)), \
                mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(app.service, "run", return_value=True), \
                mock.patch.object(camera, "read_ctrls", return_value=camera.parse_ctrls(read("brio/ctrls-index0.txt"))), \
                mock.patch.object(app, "Window", window):
            store.save({"version": 1, "camera": {"zoom_absolute": 150}})
            app.Application.do_activate(stub)
            p1 = stub.window.camera
            p1.setter.set([("zoom_absolute", 300)])  # on the camera
            app.Application.on_close(stub, stub.window)
            app.Application.do_activate(stub)  # opened again
            p2 = stub.window.camera
            spawn.finish(True)  # window 1's write is done and saved
            self.assertEqual(store.load()["camera"], {"zoom_absolute": 300})
            p2.setter.set([("brightness", 10)])
            spawn.finish(True)
            self.assertEqual(store.load()["camera"], {"zoom_absolute": 300, "brightness": 10})

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

    def test_with_the_unit_enabled_a_window_instance_holds_itself(self):
        # Opened from the app grid with booth.service enabled but not running: "Run in Background" shows ON, so closing
        # the window must leave Booth running, as the switch turned on does.
        for enabled in (False, True):
            stub = mock.Mock(window=None, starting=True, held=False)
            with mock.patch.object(app, "Window"):
                app.Application.show_window(stub, {"camera": {}}, None, {}, enabled)
            self.assertIs(stub.held, enabled)
            self.assertEqual(stub.hold.call_count, int(enabled))
        stub = mock.Mock(window=None, starting=True, held=True)  # the background instance: held already
        with mock.patch.object(app, "Window"):
            app.Application.show_window(stub, {"camera": {}}, None, {}, True)
        stub.hold.assert_not_called()


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
                mock.patch.object(app.store, "load", return_value={"version": 1, "camera": {}}), \
                mock.patch.object(app, "Window") as window:
            app.Application.do_activate(stub)
        self.assertEqual(stub.settings, {"version": 1, "camera": {}})  # loaded once, for the whole process
        self.assertIs(stub.headset_monitor.settings, stub.settings)  # the same dict: sidetone re-sent as last saved
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
        stub = mock.Mock(held=False, background=background, settings={"routing": {"moved": True, "direct": False}})
        folders, idle = {path: mock.Mock(name=path) for path in ("/dev/v4l/by-id", "/dev/snd/by-id")}, []

        def idle_add(f, *a):  # the main loop runs it at once; a source id back, as GLib's
            idle.append((f, a))
            f(*a)
            return 7
        with mock.patch.object(app.Gio.File, "new_for_path", side_effect=folders.get), \
                mock.patch.object(app.GLib, "timeout_add_seconds") as every, \
                mock.patch.object(app.routing, "Executor") as executor, \
                mock.patch.object(app.GLib, "unix_signal_add") as signal_add, \
                mock.patch.object(app.GLib, "idle_add", idle_add):
            app.Application.watch(stub)
            # logout stops booth.service with SIGTERM: quit, so shutdown stops a backup selector (#20)
            signal_add.assert_called_once_with(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stub.quit)
            # the routing policy follows the PRO X in every primary instance, subscribed before the first poll, from
            # the saved move (a restart while moved still puts it back); its notifications and saves go to the main loop
            (settings, notify, save), = [c.args for c in executor.call_args_list]
            self.assertIs(stub.routing, executor.return_value)
            # and the mic fallback (#20): the PRO X on or off pokes a heal, also from the first reading
            self.assertEqual(stub.headset_monitor.mock_calls[:3],
                             [mock.call.listeners.append(executor.return_value.listener),
                              mock.call.listeners.append(stub.on_headset), mock.call.poll()])
            notify("PRO X off")
            self.assertIs(save({}), stub.save_routing.return_value)  # waited for: the write-ahead needs the answer
        self.assertIs(settings, stub.settings)  # the saved record and the user's switch, read at each step
        self.assertEqual(idle[0], (stub.routed, ("PRO X off",)))
        stub.save_routing.assert_called_once_with({})
        stub.headset_monitor.poll.assert_called_once_with()  # the battery now, in every primary instance
        # then every 10 s, window or not: the battery, a routing rule not settled yet (the PRO X sink not back, a failed
        # write) run again, and Booth Mic's selector restarted if its Scarlett is back
        self.assertEqual(every.call_args_list, [mock.call(10, stub.headset_monitor.poll),
                                                mock.call(10, executor.return_value.retry), mock.call(10, stub.heal_voice)])
        monitor = folders["/dev/v4l/by-id"].monitor_directory.return_value
        self.assertIs(stub.monitor, monitor)
        monitor.connect.assert_called_once_with("changed", stub.on_by_id)
        self.assertIsInstance(stub.replug, service.Debounce)
        dongle = folders["/dev/snd/by-id"].monitor_directory.return_value  # the PRO X dongle's ALSA control link
        self.assertIs(stub.snd_monitor, dongle)
        dongle.connect.assert_called_once_with("changed", stub.on_snd_by_id)
        self.assertIsInstance(stub.dongle, service.Debounce)
        self.assertIsInstance(stub.mic_poke, service.Debounce)  # a burst of sound-card events: one heal after it
        self.assertIs(stub.mic_poke.fire, stub.heal_voice)
        stub.fix_headset.assert_called_once_with()  # on service start, and in every primary instance
        stub.sync_voice.assert_called_once_with()  # Booth Mic's config written and its service started, window or not
        stub.watch_mute.assert_called_once_with()  # #21: the mute's record kept, window or not
        calls = [c[0] for c in stub.mock_calls]  # the mic running now said before sync_voice may move the selector
        self.assertLess(calls.index("mic_at_start"), calls.index("sync_voice"))
        return stub

    def test_sync_voice_writes_booth_mics_config_and_starts_its_service_off_the_main_loop(self):
        threads, stub = [], mock.Mock(settings={"voice": {}})
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "sync") as sync:
            app.Application.sync_voice(stub)
            sync.assert_not_called()
            threads[0]()
        sync.assert_called_once_with(stub.settings)

    def test_monitoring_follows_its_switch_at_start_off_the_main_loop_window_or_not(self):
        threads, stub = [], mock.Mock(settings={"voice": {"monitor": True}})
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "sync"), mock.patch.object(app.monitor, "sync") as sync:
            app.Application.sync_voice(stub)
            sync.assert_not_called()
            threads[0]()
        sync.assert_called_once_with(stub.settings, on=stub.headset_monitor.on)  # paused while the PRO X is off

    def test_every_heal_tick_brings_monitoring_back_even_when_booth_mics_heal_fails(self):
        threads, stub = [], mock.Mock(settings={"voice": {"monitor": True}}, healing=False)
        stub.heal_once = lambda: app.Application.heal_once(stub)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "heal", side_effect=OSError("boom")), \
                mock.patch.object(app.monitor, "sync") as sync:
            app.Application.heal_voice(stub)
            with self.assertRaises(OSError):
                threads[0]()
        # a closed window, the background service: every 10 s, paused while the PRO X is off
        sync.assert_called_once_with(stub.settings, on=stub.headset_monitor.on)
        self.assertFalse(stub.healing)

    def test_booth_stereo_is_started_at_start_and_brought_back_every_heal_tick_off_the_main_loop(self):  # #19
        threads, stub = [], mock.Mock(settings={"voice": {}}, healing=False)
        stub.heal_once = lambda: app.Application.heal_once(stub)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "sync"), mock.patch.object(app.monitor, "sync"), \
                mock.patch.object(app.voice, "heal", side_effect=OSError("boom")), \
                mock.patch.object(app.stereo, "sync") as sync:
            app.Application.sync_voice(stub)
            app.Application.heal_voice(stub)
            sync.assert_not_called()
            threads[0]()
            with self.assertRaises(OSError):  # Booth Mic's heal failed: Booth Stereo is still looked after
                threads[1]()
        self.assertEqual(sync.call_args_list, [mock.call(stub.settings)] * 2)
        self.assertFalse(stub.healing)

    def test_heal_voice_checks_booth_mics_selector_off_the_main_loop_every_time(self):
        threads, stub = [], mock.Mock(settings={"voice": {}}, healing=False, mic={})
        stub.heal_once = lambda: app.Application.heal_once(stub)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "heal") as heal:
            self.assertIs(app.Application.heal_voice(stub), True)  # True: GLib keeps the timer
            heal.assert_not_called()
            threads[0]()
        (settings,), kwargs = heal.call_args  # the chosen input: heal reads the Scarlett's routing itself
        self.assertIs(settings, stub.settings)
        for on in (True, False, None):  # None: not answered yet, which choose() tells from off
            stub.headset_monitor.on = on
            self.assertIs(kwargs["on"](), on)

    def test_a_slow_heal_is_never_overlapped_by_the_next_tick(self):
        threads, stub = [], mock.Mock(settings={"voice": {}}, healing=False, mic={})
        stub.heal_once = lambda: app.Application.heal_once(stub)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.voice, "heal") as heal:
            for tick in range(3):  # amixer, pw-dump and systemctl still running: the ticks are skipped
                self.assertIs(app.Application.heal_voice(stub), True)  # True: GLib keeps the timer
            self.assertEqual(len(threads), 1)
            threads[0]()
            heal.side_effect = OSError("boom")  # a heal that fails still lets the next one run
            app.Application.heal_voice(stub)
            with self.assertRaises(OSError):
                threads[1]()
            app.Application.heal_voice(stub)
        self.assertEqual((len(threads), heal.call_count), (3, 2))

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
        timers, ran, stub = Timers(), [], mock.Mock(window=None, closed_page=None, settings=self.SAVED)
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
            self.assertEqual(ran, [])
            timers.fire()
        load.assert_not_called()  # the app's one settings dict: every page save is in it already
        self.assertEqual(ran, ["--set-ctrl=auto_exposure=1", "--set-ctrl=zoom_absolute=250",
                               "--set-ctrl=exposure_time_absolute=300"])
        self.assertEqual(timers.pending, {})

    def test_udevs_rename_alone_restores(self):  # another camera keeps by-id present: no folder event comes
        stub = mock.Mock()
        self.event(stub, "/dev/v4l/by-id/.#tmp", DEV, Gio.FileMonitorEvent.RENAMED)
        stub.replug.poke.assert_called_once_with()

    def test_a_color_node_moved_into_by_id_restores(self):  # Gio's MOVED_IN: `other` is the old path, outside the folder
        stub = mock.Mock()
        self.event(stub, DEV, "/dev/v4l/.tmp-link", Gio.FileMonitorEvent.MOVED_IN)
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

    def test_a_held_instance_whose_window_opened_after_a_replug_restores_the_saved_values(self):
        # The BRIO came back at its power-on defaults (zoom 100, auto exposure on); the window opened and read them
        # before the debounced restore fired. The restore must write the saved values and leave the file as it was.
        from booth.ui.camera import CameraPage
        from tests.test_camera import FakeSpawn

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        timers, idle, spawn = Timers(), [], FakeSpawn()
        stub = mock.Mock(held=True)
        stub.replug = service.Debounce(app.REPLUG_MS, lambda: app.Application.restore_saved(stub), timers.add,
                                       timers.remove)
        with mock.patch.dict(os.environ, XDG_CONFIG_HOME=tmp.name):
            store.save(store.defaults() | {"camera": {"zoom_absolute": 250, "auto_exposure": 1}})
            before = store.path().read_bytes()
            self.event(stub, "/dev/v4l/by-id/.#tmp", DEV, Gio.FileMonitorEvent.RENAMED)  # the replug
            with mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append(a)), \
                    mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                    mock.patch.object(app.service, "run", return_value=True), \
                    mock.patch.object(camera, "read_ctrls", return_value=camera.parse_ctrls(read("brio/ctrls-index0.txt"))):
                app.Application.start(stub, store.load())  # the window opens: it reads the defaults
            settings, node, ctrls, _ = idle[0]
            page = mock.Mock(ctrls=ctrls, values={n: c["value"] for n, c in ctrls.items() if "value" in c})
            page.setter = camera.Setter(settings, node, spawn, store.save, page.values)
            page.reapply = lambda: CameraPage.reapply(page)
            stub.window.camera = page
            timers.fire()  # the debounce
            while spawn.done:
                spawn.finish(True)
            self.assertEqual([c[-1] for c in spawn.cmds],
                             ["--set-ctrl=auto_exposure=1", "--set-ctrl=zoom_absolute=250"])
            self.assertEqual(store.path().read_bytes(), before)
        self.assertEqual((page.values["zoom_absolute"], page.values["auto_exposure"]), (250, 1))  # the page shows them

    def test_a_worker_restore_waits_for_a_write_in_flight_then_goes_through_a_page_that_opened(self):
        threads, idle, stub = [], [], mock.Mock(window=None, closed_page=None)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()):
            app.Application.restore_saved(stub)
        with mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append(f)), \
                mock.patch.object(app, "restore_camera") as restore, \
                mock.patch.object(app.store, "load", return_value=self.SAVED) as load:
            with app.RESTORING:  # the page's write of zoom 300 is on the camera
                worker = threading.Thread(target=threads[0], daemon=True)
                worker.start()
                worker.join(0.3)
                load.assert_not_called()  # the saved values are read once that write is done, not before
                stub.window = mock.Mock()
                stub.window.camera.ctrls = {camera.ZOOM: {}}  # and the window opened meanwhile
            worker.join(5)
        restore.assert_not_called()  # no second writer: the page's Setter restores, behind its own writes
        self.assertEqual(idle, [stub.window.camera.reapply])

    def test_fsk_restore_alone_reads_the_file_before_the_write_it_waited_for_is_saved(self):
        # A held instance: the user dragged zoom to 300 and closed the window while that write was on the camera (its
        # save comes after, on the main loop). A replug restore then must not write the older saved 150 after it.
        from booth.ui.camera import CameraPage
        from tests.test_camera import FakeSpawn

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spawn, ctrls = FakeSpawn(), camera.parse_ctrls(read("brio/ctrls-index0.txt"))
        with mock.patch.dict(os.environ, XDG_CONFIG_HOME=tmp.name), \
                mock.patch.object(app.threading, "Thread", Inline), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: f(*a)), \
                mock.patch.object(app.devices, "discover", return_value={"camera": DEV}), \
                mock.patch.object(camera, "read_ctrls", return_value=ctrls), \
                mock.patch.object(camera, "run", lambda cmd: spawn.cmds.append(cmd) or True):
            store.save({"version": 1, "camera": {"zoom_absolute": 150}})
            page = mock.Mock(ctrls=ctrls, values={n: c["value"] for n, c in ctrls.items() if "value" in c})
            page.setter = camera.Setter(store.load(), DEV, spawn, store.save, page.values)
            page.reapply = lambda: CameraPage.reapply(page)
            stub = mock.Mock(window=mock.Mock(camera=page), closed_page=None)
            page.setter.set([("zoom_absolute", 300)])  # on the camera
            app.Application.on_close(stub, stub.window)
            app.Application.restore_saved(stub)  # the replug
            while spawn.done:
                spawn.finish(True)
            self.assertEqual([c[-1] for c in spawn.cmds if "zoom" in c[-1]][-1], "--set-ctrl=zoom_absolute=300")
            self.assertEqual(store.load()["camera"]["zoom_absolute"], 300)

    def test_a_window_closed_while_the_worker_waited_restores_through_its_setter(self):
        threads, idle, stub = [], [], mock.Mock(window=None, closed_page=None)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()):
            app.Application.restore_saved(stub)
        stub.closed_page = mock.Mock()  # a window opened, wrote and closed before the worker got the camera
        with mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append(f)), \
                mock.patch.object(app, "restore_camera") as restore:
            threads[0]()
        restore.assert_not_called()  # its last write may be unsaved: the file could be older than the camera
        self.assertEqual(idle, [stub.closed_page.reapply])

    def test_with_no_window_or_no_camera_on_the_page_a_worker_restores(self):
        idle_page = mock.Mock(setter=mock.Mock(batch=[], queue=[]))  # a closed page with every write saved
        for stub in (mock.Mock(window=None, closed_page=idle_page, settings=self.SAVED),
                     mock.Mock(closed_page=None, settings=self.SAVED)):
            if stub.window:
                stub.window.camera.ctrls = {}  # the page opened while the BRIO was unplugged: it can't write
            with mock.patch.object(app.threading, "Thread", Inline), \
                    mock.patch.object(app, "restore_camera") as restore:
                app.Application.restore_saved(stub)
            restore.assert_called_once_with(self.SAVED)
            if stub.window:
                stub.window.camera.reapply.assert_not_called()

    def test_an_ir_node_or_unplug_event_never_restores(self):
        stub = mock.Mock()
        self.event(stub, DEV.replace("index0", "index2"), None, Gio.FileMonitorEvent.CREATED)
        self.event(stub, DEV, None, Gio.FileMonitorEvent.DELETED)
        stub.replug.poke.assert_not_called()


class DongleReplug(unittest.TestCase):
    """The PRO X dongle's hardware volume is set to full whenever it appears in /dev/snd/by-id."""
    DONGLE = "/dev/snd/by-id/usb-Logitech_PRO_X_Wireless_Gaming_Headset-00"

    def event(self, stub, file, other, event):
        app.Application.on_snd_by_id(stub, None, Gio.File.new_for_path(file), other and Gio.File.new_for_path(other), event)

    def test_a_burst_of_dongle_events_fixes_once_after_the_last(self):
        clock, fixes, stub = Clock(), [], mock.Mock()
        stub.dongle = service.Debounce(app.REPLUG_MS, lambda: fixes.append(clock.now), clock.add, clock.remove)
        for _ in range(5):
            self.event(stub, self.DONGLE, None, Gio.FileMonitorEvent.CREATED)
            self.event(stub, "/dev/snd/by-id/.#tmp", self.DONGLE, Gio.FileMonitorEvent.RENAMED)
            self.event(stub, self.DONGLE, "/dev/snd/.tmp", Gio.FileMonitorEvent.MOVED_IN)
            self.event(stub, self.DONGLE, None, Gio.FileMonitorEvent.DELETED)
            clock.advance(100)
        clock.advance(10_000)
        self.assertEqual(fixes, [400 + app.REPLUG_MS])

    def test_udevs_rename_alone_fixes(self):  # the link is made under a temp name, then renamed: `other` is the new name
        stub = mock.Mock()
        self.event(stub, "/dev/snd/by-id/.#tmp", self.DONGLE, Gio.FileMonitorEvent.RENAMED)
        stub.dongle.poke.assert_called_once_with()

    def test_another_cards_link_or_an_unplug_fixes_nothing(self):
        stub = mock.Mock()
        self.event(stub, "/dev/snd/by-id/usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00", None,
                   Gio.FileMonitorEvent.CREATED)
        self.event(stub, self.DONGLE, None, Gio.FileMonitorEvent.DELETED)
        stub.dongle.poke.assert_not_called()

    def test_the_fix_runs_on_a_worker(self):
        threads = []

        class Recorded(Inline):
            def start(self):
                threads.append(self)  # started, not run: the caller (the main loop) never runs amixer itself
        with mock.patch.object(app.threading, "Thread", Recorded), \
                mock.patch.object(app.headset, "fix_hw_volume") as fix:
            app.Application.fix_headset(mock.Mock())
            fix.assert_not_called()
            self.assertEqual(len(threads), 1)
            threads[0].target()
        fix.assert_called_once_with()


class HeadsetBattery(unittest.TestCase):
    def test_the_monitor_queries_on_a_worker_and_reports_on_the_main_loop(self):
        idle, threads = [], []

        class Recorded(Inline):
            def start(self):
                threads.append(self)
        with mock.patch.object(app.threading, "Thread", Recorded), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))):
            app.off_main(lambda: (3990, False), "done")
            self.assertEqual(idle, [])  # nothing on the caller's (main) loop yet
            threads[0].target()
        self.assertEqual(idle, [("done", ((3990, False),))])

    def test_a_query_that_raises_still_reports_back_as_off(self):  # else the monitor stays busy and never polls again
        idle = []
        with mock.patch.object(app.threading, "Thread", Inline), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                self.assertRaises(ZeroDivisionError):
            app.off_main(lambda: 1 / 0, "done")
        self.assertEqual(idle, [("done", (None,))])

    def test_on_main_runs_on_the_main_loop_and_waits_for_its_answer(self):  # the routing's write-ahead save
        with mock.patch.object(app.GLib, "idle_add", lambda f, *a: threading.Timer(0.05, f, a).start()):
            self.assertEqual(app.on_main(lambda x: x + 1, 1), 2)  # the main loop busy for a moment: waited for
        with mock.patch.object(app.GLib, "idle_add", lambda f, *a: None), mock.patch.object(app, "ON_MAIN_S", 0):
            self.assertIs(app.on_main(lambda: True), False)  # the main loop gone (quitting): not saved

    def test_the_application_owns_one_monitor_wired_to_its_notification_and_worker(self):
        booth = app.Application()
        self.assertIs(booth.headset_monitor.spawn, app.off_main)
        with mock.patch.object(booth, "send_notification") as send:
            booth.headset_monitor.update((3710, False))
        (nid, note), = [c.args for c in send.call_args_list]
        self.assertEqual(nid, "headset-battery")
        self.assertIsInstance(note, Gio.Notification)

    def test_a_routing_notification_replaces_the_last_one(self):
        with mock.patch.object(app.Gio, "Notification") as notification:
            stub = mock.Mock()
            self.assertIs(app.Application.routed(stub, "PRO X off — sound moved to the Scarlett"), GLib.SOURCE_REMOVE)
        notification.new.assert_called_once_with("PRO X off — sound moved to the Scarlett")
        stub.send_notification.assert_called_once_with("routing", notification.new.return_value)

    def test_the_routing_state_is_saved_in_the_shared_settings(self):
        stub = mock.Mock(settings={"version": 1, "routing": {"moved": True, "direct": False, "direct_monitor": False}})
        with mock.patch.object(app.store, "save") as save:
            self.assertIs(app.Application.save_routing(stub, {}), True)
        self.assertEqual(stub.settings["routing"], {"direct_monitor": False})  # the user's switch is kept
        save.assert_called_once_with(stub.settings)
        stub.send_notification.assert_not_called()

    def test_a_routing_state_that_cant_be_saved_is_kept_and_said(self):  # disk full: lost at the next restart
        stub = mock.Mock(settings={"version": 1, "routing": {}})
        with mock.patch.object(app.store, "save", side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(app.Gio, "Notification") as notification:
            self.assertIs(app.Application.save_routing(stub, {"moved": True, "direct": False}), False)
        self.assertEqual(stub.settings["routing"], {"moved": True, "direct": False})
        notification.new.assert_called_once_with("Sound routing set, but not saved: No space left on device")
        stub.send_notification.assert_called_once_with("routing-unsaved", notification.new.return_value)

    def test_the_low_battery_notification_says_the_estimate(self):
        with mock.patch.object(app.Gio, "Notification") as notification:
            stub = mock.Mock()
            app.Application.low_battery(stub, 13)
        notification.new.assert_called_once_with("Headset battery low")
        notification.new.return_value.set_body.assert_called_once_with("PRO X Wireless is at ~13 % (est.).")
        stub.send_notification.assert_called_once_with("headset-battery", notification.new.return_value)


class MicFallback(unittest.TestCase):
    """#20: after each heal the routing policy's mic rule sees what heal chose; a move is notified, shown in the sidebar
    and on the Microphone page's banner at once; a return to the Scarlett not settled yet is looked at again ~2 s later."""

    def heal_once(self, mic, chosen, running=True):
        stub, idle = mock.Mock(settings={}, mic=mic, healing=True), []
        with mock.patch.object(app.voice, "heal"), mock.patch.object(app.monitor, "sync"), \
                mock.patch.object(app.voice, "running", return_value=running), \
                mock.patch.object(app.voice, "CHOSEN", chosen, create=True), \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))):
            app.Application.heal_once(stub)
        self.assertFalse(stub.healing)
        return stub, idle

    def test_a_first_sight_of_the_return_pokes_another_heal_soon(self):
        stub, idle = self.heal_once({"said": voice.PRO_X_MIC}, voice.SCARLETT)
        self.assertEqual(stub.mic, {"said": voice.PRO_X_MIC, "seen": voice.SCARLETT})
        self.assertEqual(idle, [(stub.mic_poke.poke, ())])  # Debounce: a GLib timer, so on the main loop

    def test_a_move_to_a_backup_goes_to_the_main_loop_on_the_heal_that_made_it(self):
        stub, idle = self.heal_once({}, voice.PRO_X_MIC)
        self.assertEqual(stub.mic, {"said": voice.PRO_X_MIC})
        self.assertEqual(idle, [(stub.mic_moved, ([("notify", routing.MIC_SAID[voice.PRO_X_MIC]),
                                                   ("label", voice.PRO_X_MIC)],))])

    def test_running_reads_the_pw_dump_heal_just_read(self):  # never a third pw-dump on an idle tick
        with mock.patch.object(app.voice, "SEEN", "heal's dump", create=True), \
                mock.patch.object(app.voice, "heal"), mock.patch.object(app.monitor, "sync"), \
                mock.patch.object(app.voice, "running", return_value=True) as running, \
                mock.patch.object(app.GLib, "idle_add"):
            app.Application.heal_once(mock.Mock(settings={}, mic={}, healing=True))
        running.assert_called_once_with("heal's dump")

    def test_booth_mic_not_running_is_its_own_source(self):  # #15's seam: a call may have moved to another mic
        stub, _ = self.heal_once({}, voice.SCARLETT, running=False)
        self.assertEqual(stub.mic["seen"], routing.DOWN)

    def test_a_change_is_notified_replacing_the_last_and_shown_in_the_window(self):
        stub = mock.Mock()
        with mock.patch.object(app.Gio, "Notification") as notification:
            app.Application.mic_moved(stub, [("notify", "said"), ("label", voice.BRIO_MIC)])
        notification.new.assert_called_once_with("said")
        stub.send_notification.assert_called_once_with("booth-mic", notification.new.return_value)
        self.assertEqual(stub.mic_source, voice.BRIO_MIC)  # a window opened later shows it too
        stub.window.show_mic.assert_called_once_with(voice.BRIO_MIC, True)
        stub.window = None
        app.Application.mic_moved(stub, [("label", voice.SCARLETT)])  # no window: nothing else to show

    def test_a_backup_left_running_is_said_and_shown_at_start(self):  # the rule starts from what runs
        for backup in (voice.PRO_X_MIC, voice.BRIO_MIC):
            stub = mock.Mock(mic={})
            with mock.patch.object(app.voice, "current", return_value=backup, create=True):
                app.Application.mic_at_start(stub)
            self.assertEqual(stub.mic, {"said": backup})  # a later return to the Scarlett is said
            stub.mic_moved.assert_called_once_with([("notify", routing.MIC_SAID[backup]), ("label", backup)])

    def test_the_scarlett_or_no_selector_at_start_says_nothing(self):
        for running in (voice.SCARLETT, None):
            stub = mock.Mock(mic={})
            with mock.patch.object(app.voice, "current", return_value=running, create=True):
                app.Application.mic_at_start(stub)
            self.assertEqual(stub.mic, {})
            stub.mic_moved.assert_not_called()

    def test_quitting_stops_a_backup_selector_in_every_primary_instance(self):
        stub = mock.Mock()
        with mock.patch.object(app.voice, "release", create=True) as release, \
                mock.patch.object(app.Adw.Application, "do_shutdown") as shutdown:
            app.Application.do_shutdown(stub)
        release.assert_called_once_with()
        shutdown.assert_called_once_with(stub)

    def test_quitting_ends_the_mute_watchers_pw_dump(self):  # #21: no pw-dump --monitor outlives Booth
        with mock.patch.object(app.voice, "release"), mock.patch.object(app.Adw.Application, "do_shutdown"), \
                mock.patch.object(app.mute, "stop") as stop:
            app.Application.do_shutdown(mock.Mock())
        stop.assert_called_once_with()

    def test_the_bus_name_the_selector_unit_asks_for_is_the_apps(self):
        self.assertEqual(app.APP_ID, service.APP_ID)

    def test_the_headset_on_or_off_pokes_a_heal(self):
        stub = mock.Mock()
        for event in ("battery", "ack"):
            app.Application.on_headset(stub, event)
        stub.mic_poke.poke.assert_not_called()
        for event in ("on", "off"):
            app.Application.on_headset(stub, event)
        self.assertEqual(stub.mic_poke.poke.call_count, 2)

    def test_any_sound_card_coming_or_going_pokes_a_heal(self):
        stub = mock.Mock()
        scarlett = Gio.File.new_for_path("/dev/snd/by-id/usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00")
        for event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_OUT):
            app.Application.on_snd_by_id(stub, None, scarlett, None, event)
        app.Application.on_snd_by_id(stub, None, Gio.File.new_for_path("/dev/snd/by-id/.#tmp"), scarlett,
                                     Gio.FileMonitorEvent.RENAMED)  # udev's link, made under a temp name
        app.Application.on_snd_by_id(stub, None, scarlett, Gio.File.new_for_path("/dev/snd/.tmp"),
                                     Gio.FileMonitorEvent.MOVED_IN)
        self.assertEqual(stub.mic_poke.poke.call_count, 5)
        app.Application.on_snd_by_id(stub, None, scarlett, None, Gio.FileMonitorEvent.CHANGES_DONE_HINT)
        self.assertEqual(stub.mic_poke.poke.call_count, 5)

    def test_a_window_opened_while_on_a_backup_shows_it_in_the_sidebar_and_the_banner(self):
        stub = mock.Mock(window=None, starting=True, mic_source=voice.BRIO_MIC)
        with mock.patch.object(app, "Window") as window:
            app.Application.show_window(stub, {"camera": {}}, None, {}, False)
        window.return_value.show_mic.assert_called_once_with(voice.BRIO_MIC)  # show_mic: the label and the banner

    def test_the_window_shows_the_mic_in_the_sidebar_and_a_change_in_the_banner(self):
        win = mock.Mock()
        ui_window.Window.show_mic(win, voice.PRO_X_MIC)  # opened while on a backup: the banner says so
        win.mic_label.set_label.assert_called_once_with("Booth Mic · from PRO X mic")
        win.mic_banner.set_title.assert_called_once_with(routing.MIC_SAID[voice.PRO_X_MIC])
        win.mic_banner.set_revealed.assert_called_once_with(True)
        win.reset_mock()
        ui_window.Window.show_mic(win, voice.SCARLETT)  # opened on the Scarlett: nothing to say
        win.mic_banner.set_revealed.assert_called_once_with(False)
        win.reset_mock()
        ui_window.Window.show_mic(win, voice.SCARLETT, True)  # the return: said too
        win.mic_banner.set_title.assert_called_once_with(routing.MIC_SAID[voice.SCARLETT])
        win.mic_banner.set_revealed.assert_called_once_with(True)


class Mute(unittest.TestCase):
    """#21: one mute, Booth Mic's node mute. The header button and Ctrl+M are app.mute's state; the watcher's sightings
    of booth_mic keep the record, the chain's silence, the button and the red sidebar dot on it, window or not."""

    def spawned(self, f, settings=None, **kw):
        stub, threads = mock.Mock(**{"settings": settings or {"voice": {}}, "mute_known": None, **kw}), []
        stub.muted = lambda: app.Application.muted(stub)
        stub.record_mute = lambda on: app.Application.record_mute(stub, on)
        with mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()), \
                mock.patch.object(app.store, "save") as save:
            f(stub)
        return stub, threads, save

    def test_the_app_registers_a_stateful_mute_action_on_ctrl_m(self):
        booth = app.Application()
        action = booth.lookup_action("mute")
        self.assertEqual(action.get_state().get_type_string(), "b")  # the header toggle button's state
        self.assertEqual(booth.get_accels_for_action("app.mute"), ["<Control>m"])

    def test_every_primary_instance_watches_the_mute_on_a_worker(self):
        stub, threads, _ = self.spawned(app.Application.watch_mute)
        self.assertEqual(len(threads), 1)
        idle = []
        with mock.patch.object(app.mute, "watch") as watch, \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))):
            threads[0]()
            (found,), _ = watch.call_args
            found((70, 9001, True))
        self.assertEqual(idle, [(stub.on_mute_seen, ((70, 9001, True),))])  # to the main loop

    def test_gnome_muting_booth_mic_is_recorded_saved_silences_the_chain_and_shows(self):
        stub, threads, save = self.spawned(lambda s: app.Application.on_mute_seen(s, (70, 9001, True)),
                                           mute_known=9001)
        self.assertIs(stub.settings["voice"]["muted"], True)
        save.assert_called_once_with(stub.settings)
        stub.mute_action.set_state.assert_called_once_with(GLib.Variant.new_boolean(True))
        stub.window.show_mute.assert_called_once_with(True)
        with mock.patch.object(app.voice, "apply") as apply:
            for t in threads:
                t()
        apply.assert_called_once_with(stub.settings, None)  # the chain's master silenced live

    def test_a_recreated_booth_mic_gets_the_mute_put_back(self):
        stub, threads, save = self.spawned(lambda s: app.Application.on_mute_seen(s, (80, 9050, False)),
                                           settings={"voice": {"muted": True}}, mute_known=9001)
        self.assertIs(stub.settings["voice"]["muted"], True)  # never unmuted by its new node
        save.assert_not_called()
        with mock.patch.object(app.mute, "write") as write:
            for t in threads:
                t()
        write.assert_called_once_with(True)
        self.assertEqual(stub.mute_known, 9001)  # followed once it shows the mute

    def test_the_same_mute_again_saves_and_applies_nothing(self):
        stub, threads, save = self.spawned(lambda s: app.Application.on_mute_seen(s, (70, 9001, False)),
                                           settings={"voice": {"muted": False}}, mute_known=9001)
        save.assert_not_called()
        self.assertEqual(threads, [])

    def test_the_button_and_ctrl_m_set_the_node_mute_and_the_record(self):
        for on in (True, False):
            stub, threads, save = self.spawned(
                lambda s: app.Application.on_mute(s, None, GLib.Variant.new_boolean(on)),
                settings={"voice": {"muted": not on}})
            self.assertIs(stub.settings["voice"]["muted"], on)
            save.assert_called_once_with(stub.settings)
            stub.mute_action.set_state.assert_called_once_with(GLib.Variant.new_boolean(on))
            with mock.patch.object(app.mute, "write") as write, mock.patch.object(app.voice, "apply") as apply:
                for t in threads:
                    t()
            write.assert_called_once_with(on)
            apply.assert_called_once_with(stub.settings, None)

    def test_a_mute_that_cant_be_saved_is_said(self):  # a booth-voice restart would start Booth Mic unmuted
        stub = mock.Mock(settings={"voice": {}}, window=None, muted=lambda: False)
        with mock.patch.object(app.threading, "Thread"), \
                mock.patch.object(app.store, "save", side_effect=OSError(28, "No space left on device")), \
                mock.patch.object(app.Gio, "Notification") as notification:
            app.Application.record_mute(stub, True)
        self.assertIn("not saved", notification.new.call_args.args[0])
        stub.send_notification.assert_called_once()

    def test_a_window_opened_shows_the_mute(self):
        stub = mock.Mock(window=None, starting=True, mic_source=voice.SCARLETT, settings={"voice": {"muted": True}})
        stub.muted = lambda: app.Application.muted(stub)
        with mock.patch.object(app, "Window") as window:
            app.Application.show_window(stub, stub.settings, None, {}, False)
        window.return_value.show_mute.assert_called_once_with(True)

    def test_the_red_sidebar_dot_shows_only_while_muted(self):
        win = mock.Mock()
        ui_window.Window.show_mute(win, True)
        win.mute_dot.set_visible.assert_called_once_with(True)
        win.reset_mock()
        ui_window.Window.show_mute(win, False)
        win.mute_dot.set_visible.assert_called_once_with(False)

    def test_the_header_button_is_bound_to_app_mute(self):
        self.assertEqual(ui_window.MUTE_ACTION, "app.mute")
        self.assertIsNotNone(app.Application().lookup_action(ui_window.MUTE_ACTION.removeprefix("app.")))
        with mock.patch.object(ui_window, "Adw") as adw, mock.patch.object(ui_window, "Gtk") as gtk:
            ui_window.page("Booth", mock.Mock(), ui_window.main_menu())
        self.assertEqual(gtk.ToggleButton.call_args.kwargs["action_name"], "app.mute")
        adw.HeaderBar.return_value.pack_start.assert_called_once_with(gtk.ToggleButton.return_value)

    def failed(self, on, said="live"):
        """The button (or Ctrl+M) set to `on` with wpctl refusing it, voice.apply saying `said`; its workers and
        main-loop calls run → the stub and the notifications sent, as their texts."""
        idle = []

        def press(s):
            s.mute_failed = lambda on, said=None: app.Application.mute_failed(s, on, said)
            app.Application.on_mute(s, None, GLib.Variant.new_boolean(on))
        stub, threads, _ = self.spawned(press, settings={"voice": {"muted": not on}}, mute_known=9001)
        with mock.patch.object(app.mute, "write", return_value=False), \
                mock.patch.object(app.voice, "apply", return_value=said) as self.applied, \
                mock.patch.object(app.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(app.store, "save"), mock.patch.object(app.Gio.Notification, "new", lambda text: text), \
                mock.patch.object(app.threading, "Thread", lambda target, daemon: threads.append(target) or mock.Mock()):
            for t in threads:
                t()
            for f, a in idle:
                f(*a)
        return stub, [c.args[1] for c in stub.send_notification.call_args_list]  # the texts sent

    def test_a_failed_mute_keeps_the_chain_silent_shows_the_node_and_says_so(self):
        stub, notes = self.failed(True)
        self.assertIs(stub.settings["voice"]["muted"], True)  # fail closed: the chain stays silent
        self.assertEqual(stub.mute_action.set_state.call_args, mock.call(GLib.Variant.new_boolean(False)))  # GNOME's
        self.assertEqual(notes, ["Couldn't mute Booth Mic in GNOME — the chain is silenced; try again"])
        with mock.patch.object(app.threading, "Thread", Inline), mock.patch.object(app.store, "save"), \
                mock.patch.object(app.mute, "write") as write:
            app.Application.on_mute_seen(stub, (70, 9001, False))  # booth_mic's next info event: still unmuted
        self.assertIs(stub.settings["voice"]["muted"], True)  # never turned live by it
        write.assert_called_once_with(True)  # the mute tried again

    def test_skeptic_a_failed_mute_says_the_chain_is_silenced_only_when_it_is(self):
        for said in ("unchanged", voice.HELD, "restarted"):
            self.assertEqual(self.failed(True, said)[1],
                             ["Couldn't mute Booth Mic in GNOME — the chain is silenced; try again"], said)
        for said in ("not applied: Booth Mic didn't take the change", "not restarted: Booth Mic's service didn't start"):
            stub, notes = self.failed(True, said)
            self.assertEqual(notes, [f"Couldn't mute Booth Mic in GNOME, and its chain couldn't be silenced ({said}); "
                                     "try again"])
            self.assertIs(stub.settings["voice"]["muted"], True)

    def test_a_failed_unmute_keeps_the_chain_silent_and_says_so(self):  # the node keeps its mute
        stub, notes = self.failed(False)
        self.assertEqual(self.applied.call_count, 1)  # the unmute's own: never one more with the record still live
        self.assertIs(stub.settings["voice"]["muted"], True)
        self.assertEqual(stub.mute_action.set_state.call_args, mock.call(GLib.Variant.new_boolean(True)))
        self.assertEqual(len(notes), 1)
        self.assertIn("Couldn't unmute Booth Mic", notes[0])


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
