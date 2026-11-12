import subprocess
import threading
import unittest
from unittest import mock

from gi.repository import GLib

from booth import camera, xu
from tests.fixture import read

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"
# what xu.read gives for the BRIO on this desk: FoV at 65°, LED on Auto
XU = {"fov": {"type": "menu", "inactive": False, "min": 0, "max": 2, "step": 1, "default": 0, "value": 2,
              "menu": dict(xu.MENUS["fov"])},
      "led_mode": {"type": "menu", "inactive": False, "min": 0, "max": 3, "step": 1, "default": 3, "value": 3,
                   "menu": dict(xu.MENUS["led_mode"])}}


def brio():
    return camera.parse_ctrls(read("brio/ctrls-index0.txt")) | XU


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

    def test_int_ranges(self):
        wb = self.ctrls["white_balance_temperature"]
        self.assertEqual((wb["type"], wb["min"], wb["max"], wb["step"], wb["default"]), ("int", 2000, 7500, 10, 4000))
        self.assertEqual(self.ctrls["backlight_compensation"]["max"], 1)

    def test_inactive_manual_values_while_auto_is_on(self):
        for name in ("exposure_time_absolute", "white_balance_temperature", "focus_absolute"):
            self.assertTrue(self.ctrls[name]["inactive"], name)
        self.assertFalse(self.ctrls["brightness"]["inactive"])

    def test_disabled_control_is_left_out_with_its_menu(self):
        text = ("power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=2 value=0 (Disabled)\n"
                "\t\t\t\t0: Disabled\n"
                "           led1_mode 0x0a046d05 (menu)   : min=0 max=3 default=0 value=0 flags=disabled\n"
                "\t\t\t\t3: Auto\n")
        self.assertEqual(camera.parse_ctrls(text),
                         {"power_line_frequency": {"type": "menu", "inactive": False, "min": 0, "max": 2,
                                                   "default": 2, "value": 0, "menu": {0: "Disabled"}}})


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

    def test_restore_applies_saved_known_controls_clamped_auto_modes_first(self):
        saved = {"zoom_absolute": 900, "white_balance_temperature": 3006, "not_on_this_camera": 4,
                 "white_balance_automatic": 0}
        self.assertEqual(camera.restore_cmds(DEV, self.ctrls, saved),
                         [["v4l2-ctl", "-d", DEV, "--set-ctrl=white_balance_automatic=0"],
                          ["v4l2-ctl", "-d", DEV, "--set-ctrl=zoom_absolute=500"],
                          ["v4l2-ctl", "-d", DEV, "--set-ctrl=white_balance_temperature=3010"]])

    def test_restore_every_auto_mode_before_every_manual_value(self):
        saved = {"focus_absolute": 50, "exposure_time_absolute": 300, "white_balance_temperature": 5000,
                 "focus_automatic_continuous": 0, "auto_exposure": 1, "white_balance_automatic": 0}
        sets = [cmd[-1] for cmd in camera.restore_cmds(DEV, self.ctrls, saved)]
        self.assertEqual(sets, ["--set-ctrl=focus_automatic_continuous=0", "--set-ctrl=auto_exposure=1",
                                "--set-ctrl=white_balance_automatic=0", "--set-ctrl=focus_absolute=50",
                                "--set-ctrl=exposure_time_absolute=300", "--set-ctrl=white_balance_temperature=5000"])

    def test_restore_skips_a_manual_value_its_auto_mode_holds(self):
        # the camera's own auto modes are on (fixture) and nothing saved turns them off: the driver would refuse these
        saved = {"exposure_time_absolute": 300, "focus_absolute": 50, "white_balance_temperature": 5000,
                 "brightness": 90}
        self.assertEqual([cmd[-1] for cmd in camera.restore_cmds(DEV, self.ctrls, saved)], ["--set-ctrl=brightness=90"])
        saved = {"auto_exposure": 3, "exposure_time_absolute": 300}
        self.assertEqual([cmd[-1] for cmd in camera.restore_cmds(DEV, self.ctrls, saved)],
                         ["--set-ctrl=auto_exposure=3"])

    def test_restore_nothing_saved(self):
        self.assertEqual(camera.restore_cmds(DEV, self.ctrls, {}), [])

    def test_fov_and_led_are_extension_unit_writes(self):
        self.assertEqual(camera.set_cmd(DEV, "fov", 1), [camera.XU_CMD, "-d", DEV, "--set-ctrl=fov=1"])
        self.assertEqual(camera.set_cmd(DEV, "led_mode", 0), [camera.XU_CMD, "-d", DEV, "--set-ctrl=led_mode=0"])

    def test_restore_reapplies_fov_and_led_with_the_others(self):
        saved = {"fov": 1, "led_mode": 0, "zoom_absolute": 250}
        self.assertEqual(camera.restore_cmds(DEV, brio(), saved),
                         [camera.set_cmd(DEV, "fov", 1), camera.set_cmd(DEV, "led_mode", 0),
                          camera.set_cmd(DEV, "zoom_absolute", 250)])
        self.assertEqual(camera.restore_cmds(DEV, self.ctrls, saved), [camera.set_cmd(DEV, "zoom_absolute", 250)])

    def test_restore_a_saved_value_outside_the_menu(self):
        # LED: Auto, never Off unless the user picked Off; FoV and other menus: the camera is left as it is
        for bad in (-1, 4, 99):
            saved = {"led_mode": bad, "fov": bad, "power_line_frequency": bad}
            self.assertEqual(camera.restore_cmds(DEV, brio(), saved), [camera.set_cmd(DEV, "led_mode", 3)])
        self.assertEqual(camera.restore_cmds(DEV, brio(), {"led_mode": 0, "fov": 2}),
                         [camera.set_cmd(DEV, "fov", 2), camera.set_cmd(DEV, "led_mode", 0)])

    def test_restore_sets_fov_before_zoom_pan_and_tilt(self):  # a FoV change on the BRIO may reset zoom and pan
        saved = {"zoom_absolute": 250, "pan_absolute": 3600, "tilt_absolute": -3600, "fov": 1,
                 "white_balance_automatic": 1}
        self.assertEqual([cmd[-1] for cmd in camera.restore_cmds(DEV, brio(), saved)],
                         ["--set-ctrl=white_balance_automatic=1", "--set-ctrl=fov=1", "--set-ctrl=zoom_absolute=250",
                          "--set-ctrl=pan_absolute=3600", "--set-ctrl=tilt_absolute=-3600"])


class Model(unittest.TestCase):
    """The pure rules behind the Camera page's rows."""

    def setUp(self):
        self.ctrls = camera.parse_ctrls(read("brio/ctrls-index0.txt"))
        self.values = {n: c["value"] for n, c in self.ctrls.items()}

    def test_groups_hold_every_standard_control_once(self):
        names = [n for _, main, advanced in camera.GROUPS for n in main + advanced]
        self.assertEqual(sorted(names), sorted(brio()))
        self.assertIn("fov", camera.GROUPS[0][1])
        self.assertIn("led_mode", camera.GROUPS[1][2])  # Image → Advanced
        self.assertEqual([g for g, _, _ in camera.GROUPS], ["Framing", "Image", "Focus"])
        image = {g: (m, a) for g, m, a in camera.GROUPS}["Image"]
        self.assertEqual(image[1], ["contrast", "saturation", "sharpness", "gain", "power_line_frequency", "led_mode"])
        self.assertIn("brightness", image[0])

    def test_labels(self):
        self.assertEqual(camera.LABELS["exposure_dynamic_framerate"], "Keep 30 fps")
        self.assertEqual(camera.LABELS["backlight_compensation"], "HDR")
        self.assertEqual(camera.LABELS["power_line_frequency"], "Anti-flicker")
        self.assertEqual((camera.LABELS["fov"], camera.LABELS["led_mode"]), ("Field of view", "LED"))

    def test_fov_and_led_rows(self):
        ctrls = brio()
        self.assertEqual(camera.choices(ctrls["fov"]), [(0, "90°"), (1, "78°"), (2, "65°")])
        ctrls["led_mode"].update(value=4, default=5)  # bytes the menu lacks show as themselves, never as item 0 "Off"
        self.assertEqual(camera.choices(ctrls["led_mode"]),
                         [(0, "Off"), (1, "On"), (2, "Blink"), (3, "Auto"), (4, "Unknown (4)"), (5, "Unknown (5)")])
        self.assertEqual((camera.kind("fov", ctrls["fov"]), camera.kind("led_mode", ctrls["led_mode"])),
                         ("combo", "combo"))
        framing = camera.defaults(ctrls, camera.GROUPS[0][1] + camera.GROUPS[0][2])
        self.assertIn(("fov", 0), framing)  # ↺ Framing goes back to the camera's 90°

    def test_keep_30_fps_on_means_no_dynamic_framerate(self):
        self.assertEqual(camera.switch_values("exposure_dynamic_framerate"), (0, 1))
        self.assertEqual(camera.switch_values("backlight_compensation"), (1, 0))
        self.assertEqual(camera.switch_values("white_balance_automatic"), (1, 0))
        self.assertEqual(camera.switch_values("auto_exposure"), (3, 1))  # on: aperture priority, off: manual

    def test_row_kinds(self):
        kinds = {n: camera.kind(n, c) for n, c in self.ctrls.items()}
        self.assertEqual(kinds["backlight_compensation"], "switch")  # an int 0..1 on the BRIO
        self.assertEqual(kinds["exposure_dynamic_framerate"], "switch")
        self.assertEqual(kinds["auto_exposure"], "switch")
        self.assertEqual(kinds["power_line_frequency"], "combo")
        self.assertEqual(kinds["brightness"], "slider")
        self.assertEqual(kinds["zoom_absolute"], "slider")

    def test_anti_flicker_choices(self):
        self.assertEqual(camera.choices(self.ctrls["power_line_frequency"]), [(0, "Off"), (1, "50 Hz"), (2, "60 Hz")])

    def test_manual_values_are_insensitive_while_auto_is_on(self):
        self.values["zoom_absolute"] = 200
        self.assertEqual(camera.insensitive(self.ctrls, self.values),
                         {"exposure_time_absolute", "white_balance_temperature", "focus_absolute"})
        self.values.update(auto_exposure=1, white_balance_automatic=0, focus_automatic_continuous=0)
        self.assertEqual(camera.insensitive(self.ctrls, self.values), set())

    def test_pan_and_tilt_are_disabled_at_1x(self):
        self.values.update(auto_exposure=1, white_balance_automatic=0, focus_automatic_continuous=0)
        self.assertEqual(camera.insensitive(self.ctrls, self.values), {"pan_absolute", "tilt_absolute"})
        self.values["zoom_absolute"] = 101
        self.assertEqual(camera.insensitive(self.ctrls, self.values), set())

    def test_defaults_the_camera_reports_auto_modes_first(self):
        framing, image, focus = (camera.defaults(self.ctrls, main + adv) for _, main, adv in camera.GROUPS)
        self.assertEqual(framing, [("zoom_absolute", 100), ("pan_absolute", 0), ("tilt_absolute", 0)])
        self.assertEqual(focus, [("focus_automatic_continuous", 1), ("focus_absolute", 0)])
        self.assertEqual(image[:2], [("auto_exposure", 3), ("white_balance_automatic", 1)])
        self.assertEqual(dict(image)["exposure_dynamic_framerate"], 0)
        self.assertEqual(dict(image)["power_line_frequency"], 2)
        self.assertEqual(camera.defaults(self.ctrls, ["not_on_this_camera", "gain"]), [("gain", 0)])

    def test_reset_sends_no_manual_value_after_its_auto_mode_goes_on(self):
        for _, main, adv in camera.GROUPS:
            batch = dict(camera.reset_batch(self.ctrls, main + adv))
            for manual, (auto, off) in camera.AUTO.items():
                if manual in batch:  # the driver refuses a manual value while its auto mode is on
                    self.assertEqual(batch.get(auto, off), off, manual)
        self.assertEqual(camera.reset_batch(self.ctrls, ["focus_automatic_continuous", "focus_absolute"]),
                         [("focus_automatic_continuous", 1)])
        image = dict(camera.reset_batch(self.ctrls, camera.GROUPS[1][1] + camera.GROUPS[1][2]))
        self.assertNotIn("exposure_time_absolute", image)
        self.assertNotIn("white_balance_temperature", image)
        self.assertEqual((image["auto_exposure"], image["brightness"]), (3, 128))

    def test_changes_sends_the_value(self):
        self.values["brightness"] = 90
        self.assertEqual(camera.changes(self.ctrls, self.values, "brightness"), [("brightness", 90)])

    def test_changes_holds_a_manual_value_while_auto_is_on(self):
        self.values["exposure_time_absolute"] = 400
        self.assertEqual(camera.changes(self.ctrls, self.values, "exposure_time_absolute"), [])

    def test_turning_auto_off_sends_the_manual_value_shown(self):
        self.values.update(focus_automatic_continuous=0, focus_absolute=40)
        self.assertEqual(camera.changes(self.ctrls, self.values, "focus_automatic_continuous"),
                         [("focus_automatic_continuous", 0), ("focus_absolute", 40)])
        self.values["focus_automatic_continuous"] = 1
        self.assertEqual(camera.changes(self.ctrls, self.values, "focus_automatic_continuous"),
                         [("focus_automatic_continuous", 1)])

    def test_a_value_the_menu_lacks_is_shown_but_never_sent(self):  # "Unknown (4)": an undocumented XU byte
        ctrls = brio()
        for n, v in (("led_mode", 4), ("fov", 7), ("power_line_frequency", 9)):
            self.assertEqual(camera.changes(ctrls, {**self.values, n: v}, n), [], n)
        self.assertEqual(camera.changes(ctrls, {**self.values, "led_mode": 0}, "led_mode"), [("led_mode", 0)])

    def test_pan_is_sent_at_1x(self):  # disabled in the UI, but a reset still centers it
        self.values["pan_absolute"] = 0
        self.assertEqual(camera.changes(self.ctrls, self.values, "pan_absolute"), [("pan_absolute", 0)])

    def test_nudge_pan_and_tilt_one_step_within_range(self):
        pan = self.ctrls["pan_absolute"]
        self.assertEqual(camera.nudge(pan, 0, 1), 3600)
        self.assertEqual(camera.nudge(pan, 0, -1), -3600)
        self.assertEqual(camera.nudge(pan, 36000, 1), 36000)

    def test_status(self):
        self.assertEqual(camera.status(self.ctrls, {"zoom_absolute": 200}), "Ready · 2.0×")
        self.assertEqual(camera.status(self.ctrls, {"zoom_absolute": 100}), "Ready · 1.0×")
        self.assertEqual(camera.status({}, {}), "Not connected")

    def test_zoom_label(self):
        self.assertEqual(camera.zoom_label(self.ctrls["zoom_absolute"], 250), "2.5×")

    def test_resolution_hint(self):
        self.assertIn("picks the resolution", camera.RESOLUTION_HINT)
        self.assertIn("not Booth", camera.RESOLUTION_HINT)


class Commands(unittest.TestCase):
    def test_run_reports_the_exit_status(self):
        self.assertIs(camera.run(["true"]), True)
        self.assertIs(camera.run(["false"]), False)
        self.assertIs(camera.run(["/nonexistent/v4l2-ctl"]), False)

    def test_read_ctrls_asks_for_controls_with_menus(self):
        out = subprocess.CompletedProcess([], 0, stdout=read("brio/ctrls-index0.txt"))
        with mock.patch("booth.camera.subprocess.run", return_value=out) as run, \
                mock.patch.object(xu, "read", return_value=XU) as xread:
            ctrls = camera.read_ctrls(DEV)
        self.assertEqual(run.call_args.args[0], ["v4l2-ctl", "-d", DEV, "--list-ctrls-menus"])
        self.assertEqual(ctrls["power_line_frequency"]["menu"], {0: "Disabled", 1: "50 Hz", 2: "60 Hz"})
        self.assertEqual((ctrls["fov"]["value"], ctrls["led_mode"]["value"]), (2, 3))  # read back at page open
        xread.assert_called_once_with(DEV)

    def test_run_sends_an_extension_unit_write_through_the_ioctl(self):
        with mock.patch.object(xu, "write", return_value=True) as write, \
                mock.patch("booth.camera.subprocess.run") as sub:
            self.assertIs(camera.run(camera.set_cmd(DEV, "fov", 1)), True)
        write.assert_called_once_with(DEV, "fov", 1)
        sub.assert_not_called()
        for refusal in (mock.Mock(return_value=False), mock.Mock(side_effect=OSError(32, "Broken pipe"))):
            with mock.patch.object(xu, "write", refusal):
                self.assertIs(camera.run(camera.set_cmd(DEV, "led_mode", 0)), False)

    def test_read_ctrls_when_the_node_cannot_be_opened(self):  # unplugged between discovery and page open
        out = subprocess.CompletedProcess([], 0, stdout=read("brio/ctrls-index0.txt"))
        with mock.patch("booth.camera.subprocess.run", return_value=out), \
                mock.patch.object(xu, "descriptors", return_value=b""), \
                mock.patch.object(xu.os, "open", side_effect=FileNotFoundError(2, "No such file or directory")):
            ctrls = camera.read_ctrls(DEV)
        self.assertIn("zoom_absolute", ctrls)
        self.assertNotIn("fov", ctrls)

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


class OneWriter(unittest.TestCase):
    def test_a_write_waits_for_a_restore_holding_the_camera(self):
        ran = []
        with mock.patch("booth.camera.subprocess.run", lambda c, **kw: ran.append(c) or subprocess.CompletedProcess(c, 0)):
            with camera.WRITING:  # a restore on another thread is writing its batch
                writer = threading.Thread(target=camera.run, args=(zoom(300),))
                writer.start()
                writer.join(0.3)
                self.assertEqual(ran, [])
            writer.join(5)
        self.assertEqual(ran, [zoom(300)])


class RestoreXU(unittest.TestCase):
    def test_saved_fov_is_reapplied_at_launch_through_the_ioctl(self):
        with mock.patch.object(camera, "read_ctrls", return_value=brio()), \
                mock.patch.object(xu, "write", return_value=True) as write, \
                mock.patch("booth.camera.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(camera.restore(DEV, {"fov": 1, "zoom_absolute": 250}), [])
        write.assert_called_once_with(DEV, "fov", 1)


class FakeSpawn:
    """Holds each spawned command until the test finishes it, like a v4l2-ctl still running on its thread."""

    def __init__(self):
        self.cmds, self.done = [], []

    def __call__(self, name, cmd, done):
        self.cmds.append(cmd)
        self.done.append(done)

    def finish(self, ok):
        return self.done.pop(0)(ok)


def cmd(name, v):
    return ["v4l2-ctl", "-d", DEV, f"--set-ctrl={name}={v}"]


def zoom(v):
    return cmd("zoom_absolute", v)


AF, FOCUS = "focus_automatic_continuous", "focus_absolute"


class SetterTest(unittest.TestCase):
    def setUp(self):
        self.settings = {"version": 1, "camera": {}}
        self.saved = []
        self.spawn = FakeSpawn()
        self.setter = camera.Setter(self.settings, DEV, self.spawn, lambda s: self.saved.append(dict(s["camera"])),
                                    {"zoom_absolute": 100, AF: 1, FOCUS: 0})

    def test_one_set_in_flight_and_the_last_value_wins(self):
        for v in (200, 300, 400):
            self.setter.set([("zoom_absolute", v)])
        self.assertEqual(self.spawn.cmds, [zoom(200)])
        self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [zoom(200), zoom(400)])
        self.spawn.finish(True)
        self.assertEqual(self.spawn.done, [])
        self.assertEqual(self.saved, [{"zoom_absolute": 200}, {"zoom_absolute": 400}])
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 400})
        self.assertEqual(self.setter.taken["zoom_absolute"], 400)

    def test_a_refused_value_is_not_saved(self):
        self.setter.set([("zoom_absolute", 250)])
        self.assertEqual(self.spawn.finish(False), ["zoom_absolute"])  # the page goes back to the camera's value
        self.assertEqual((self.saved, self.settings["camera"]), ([], {}))
        self.assertEqual(self.setter.taken["zoom_absolute"], 100)
        self.setter.set([("zoom_absolute", 260)])  # a failure doesn't wedge the setter
        self.assertEqual(self.spawn.finish(True), [])
        self.assertEqual(self.saved, [{"zoom_absolute": 260}])

    def test_a_failed_save_does_not_wedge_the_setter(self):
        def save(settings):
            raise OSError(28, "No space left on device")

        self.setter.save = save
        self.setter.set([(AF, 0), (FOCUS, 40)])
        with self.assertRaises(OSError):  # surfaced to the page, which says so
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0), cmd(FOCUS, 40)])  # the camera took the auto-off: go on
        with self.assertRaises(OSError):
            self.spawn.finish(True)
        self.setter.set([("zoom_absolute", 300)])
        self.assertEqual(self.spawn.cmds[-1], zoom(300))

    def test_the_manual_value_waits_for_its_auto_off(self):
        self.setter.set([(AF, 0), (FOCUS, 40)])
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0)])  # nothing races the auto-off
        self.setter.set([("zoom_absolute", 200)])
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0)])  # nor another control's write
        self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0), cmd(FOCUS, 40)])
        self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds[-1], zoom(200))
        self.assertEqual(self.settings["camera"], {AF: 0, FOCUS: 40})

    def test_a_refused_auto_off_stops_its_batch(self):
        self.setter.set([(AF, 0), (FOCUS, 40)])
        self.setter.set([("zoom_absolute", 200)])
        self.assertEqual(self.spawn.finish(False), [AF, FOCUS])  # both go back to what the camera has
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0), zoom(200)])  # the manual value is never sent
        self.assertEqual((self.saved, self.setter.taken[AF], self.setter.taken[FOCUS]), ([], 1, 0))

    def test_a_refused_value_asked_for_again_is_not_reverted(self):
        self.setter.set([("zoom_absolute", 200)])
        self.setter.set([("zoom_absolute", 300)])
        self.assertEqual(self.spawn.finish(False), [])  # 300 is still coming: the page keeps showing it
        self.assertEqual(self.spawn.cmds, [zoom(200), zoom(300)])

    def test_a_queued_auto_off_survives_a_later_value_for_its_manual_control(self):
        self.setter.set([("zoom_absolute", 200)])  # a write is out
        self.setter.set([(AF, 0), (FOCUS, 40)])
        self.setter.set([(FOCUS, 50)])
        while self.spawn.done:
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [zoom(200), cmd(AF, 0), cmd(FOCUS, 40), cmd(FOCUS, 50)])
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 200, AF: 0, FOCUS: 50})

    def test_a_refusal_puts_the_page_values_back_to_what_the_camera_has(self):
        values = {"zoom_absolute": 100, AF: 0, FOCUS: 40}  # the page after the user turned autofocus off
        self.setter.set([(AF, 0), (FOCUS, 40)])
        missed = self.spawn.finish(False)
        self.assertEqual(camera.revert(values, self.setter.taken, missed), [AF, FOCUS])
        self.assertEqual(values, {"zoom_absolute": 100, AF: 1, FOCUS: 0})
        self.assertIn(FOCUS, camera.held(values))  # so the focus row is insensitive again

    def test_fov_goes_through_the_same_queue_and_is_saved_once_taken(self):
        self.setter.set([("zoom_absolute", 200)])
        self.setter.set([("fov", 1)])
        self.assertEqual(self.spawn.finish(True), [])
        self.assertEqual(self.spawn.cmds, [zoom(200), camera.set_cmd(DEV, "fov", 1)])
        self.assertEqual(self.spawn.finish(False), ["fov"])  # refused: not saved, the row goes back
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 200})
        self.setter.set([("fov", 0)])
        self.spawn.finish(True)
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 200, "fov": 0})

    def test_a_replug_restore_never_lands_an_older_value_after_a_newer_one(self):
        self.settings["camera"].update({"zoom_absolute": 150, AF: 0, FOCUS: 40})  # saved before the replug
        self.setter.set([("zoom_absolute", 300)])  # a drag just after the replug: in flight, not saved yet
        self.setter.set([(AF, 0), (FOCUS, 45)])  # and a newer focus, queued behind it
        self.assertEqual(self.setter.reapply(brio()), [(AF, 0), ("zoom_absolute", 300), (FOCUS, 45)])
        self.setter.set([("brightness", 140)])  # and a write after the restore was queued
        while self.spawn.done:
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [zoom(300), cmd(AF, 0), cmd(FOCUS, 45), cmd(AF, 0), zoom(300), cmd(FOCUS, 45),
                                           cmd("brightness", 140)])
        self.assertEqual(self.settings["camera"], {"zoom_absolute": 300, AF: 0, FOCUS: 45, "brightness": 140})

    def test_fsk_inflight_then_newer_queued_same_control(self):
        self.settings["camera"]["zoom_absolute"] = 150  # saved before the replug
        self.setter.set([("zoom_absolute", 300)])  # in flight
        self.setter.set([("zoom_absolute", 350)])  # newer, queued behind it
        self.assertEqual(self.setter.reapply(brio()), [("zoom_absolute", 350)])
        while self.spawn.done:
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds[-1], zoom(350))
        self.assertEqual(self.settings["camera"]["zoom_absolute"], 350)
        self.assertEqual(self.saved[-1]["zoom_absolute"], 350)

    def test_a_replug_restore_writes_the_saved_values_never_what_the_camera_reads_back(self):
        # The page read the camera between the replug and the restore: it took the power-on defaults (zoom 100, AF on).
        self.settings["camera"].update({"zoom_absolute": 250, AF: 0, FOCUS: 40})
        before = dict(self.settings["camera"])
        self.assertEqual(self.setter.reapply(brio()), [(AF, 0), ("zoom_absolute", 250), (FOCUS, 40)])
        while self.spawn.done:
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [cmd(AF, 0), zoom(250), cmd(FOCUS, 40)])
        self.assertEqual(self.settings["camera"], before)
        self.assertTrue(all(saved == before for saved in self.saved))

    def test_batches_for_other_controls_do_not_collapse(self):
        self.setter.set([("zoom_absolute", 200)])
        self.setter.set([])  # nothing to send (a held manual value) must not stall the queue
        self.setter.set([("pan_absolute", 0)])
        self.setter.set([("tilt_absolute", 0)])
        for _ in range(3):
            self.spawn.finish(True)
        self.assertEqual(self.spawn.cmds, [zoom(200), cmd("pan_absolute", 0), cmd("tilt_absolute", 0)])
        self.assertEqual(self.spawn.done, [])


class PageQuietTest(unittest.TestCase):
    """The page's own widget moves (a reset, a revert) send nothing: driven on a stub, no window."""

    def test_show_moves_widgets_without_sending(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from booth.ui.camera import CameraPage

        sent = []
        page = mock.Mock(quiet=False, values={"zoom_absolute": 300}, ctrls=brio())
        page.setter.set = sent.append
        page.widgets = {"zoom_absolute": lambda v: CameraPage.changed(page, "zoom_absolute", v)}  # a widget's signal
        CameraPage.show(page, ["zoom_absolute"])
        self.assertEqual(sent, [])
        CameraPage.changed(page, "zoom_absolute", 250)  # the user moving it still sends
        self.assertEqual(sent, [[("zoom_absolute", 250)]])

    def test_a_replug_reapplies_the_saved_values_and_the_page_shows_them(self):
        from booth.ui.camera import CameraPage

        page = mock.Mock(values={"zoom_absolute": 100, AF: 1}, ctrls=brio())  # the camera's power-on defaults
        page.setter.reapply.return_value = [(AF, 0), ("zoom_absolute", 250)]
        CameraPage.reapply(page)
        page.setter.reapply.assert_called_once_with(page.ctrls)  # never the page's values: they may be the defaults
        self.assertEqual(page.values, {"zoom_absolute": 250, AF: 0})
        self.assertEqual(list(page.show.call_args.args[0]), [AF, "zoom_absolute"])

    def test_a_finished_write_for_a_control_with_no_row_goes_on_to_the_next(self):
        from booth.ui.camera import CameraPage

        spawn, settings = FakeSpawn(), {"version": 1, "camera": {}}
        page = mock.Mock(rows={}, errors={}, values={}, ctrls=brio())  # led_mode is saved but has no row on this page
        page.setter = camera.Setter(settings, DEV, spawn, lambda s: None, {})
        page.setter.set([("led_mode", 3)])
        page.setter.set([("zoom_absolute", 200)])
        self.assertEqual(CameraPage.finished(page, "led_mode", True, spawn.done.pop(0)), GLib.SOURCE_REMOVE)
        self.assertEqual(spawn.cmds[-1], zoom(200))  # the queue did not stall
        self.assertEqual(settings["camera"], {"led_mode": 3})


if __name__ == "__main__":
    unittest.main()
