"""The Camera page's live preview, headless: the pipeline's elements, its state as the page is shown and left, the
"in use" error, and one frame pulled into a texture with no window (an injected test source, never a camera)."""
import gc
import threading
import time
import unittest
from unittest import mock

from gi.repository import GLib, GObject

from booth import preview
from booth.preview import Gdk, Gst
from tests.test_camera import XU, brio

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"
TEST_SOURCE = "videotestsrc name=src is-live=true ! jpegenc"  # MJPEG like the BRIO's: the rest is the real pipeline


def fake(_node):
    return f"{TEST_SOURCE} ! {preview.TAIL}"


# No test can reach a camera, even through a guard that regressed: v4l2src is gone from this process's registry.
_v4l2 = Gst.Registry.get().find_feature("v4l2src", Gst.ElementFactory)
if _v4l2 is not None:
    Gst.Registry.get().remove_feature(_v4l2)


def drain():
    """Wait until the state-change worker has run every job queued so far."""
    done = threading.Event()
    preview.JOBS.put(done.set)
    assert done.wait(5), "the worker is stuck"


def until(check, seconds=5):
    """Run the main loop until check() is true; False on timeout."""
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    return check()


def state(pipeline):
    drain()  # its state change runs on the worker
    return pipeline.get_state(2 * Gst.SECOND)[1]


def chain(pipeline):
    """The pipeline's elements from the source down, by factory."""
    e, out = pipeline.get_by_name("src"), []
    while e is not None:
        out.append(e)
        pad = e.get_static_pad("src")
        e = pad.get_peer().get_parent_element() if pad and pad.get_peer() else None
    return out


def error(code, text="x"):
    return GLib.Error.new_literal(Gst.ResourceError.quark(), text, code)


class Root(GObject.Object):  # the page's window: only its `suspended` matters here
    suspended = GObject.Property(type=bool, default=False)

    def is_suspended(self):
        return self.suspended


class Recorder:
    def __init__(self):
        self.frames, self.threads, self.failures = [], [], []

    def show(self, texture):
        self.frames.append(texture)
        self.threads.append(threading.current_thread())


class Pipeline(unittest.TestCase):
    def test_streams_mjpeg_640x360_from_the_node_it_is_given(self):
        self.assertEqual(preview.launch(DEV), f'v4l2src name=src device="{DEV}" ! {preview.TAIL}')
        p = Gst.parse_launch(fake(DEV))  # the same tail behind a test source: the camera is never built
        elements = chain(p)[2:]  # after videotestsrc ! jpegenc
        self.assertEqual([e.get_factory().get_name() for e in elements], ["capsfilter", "jpegdec", "videoconvert", "capsfilter", "appsink"])
        self.assertEqual(elements[0].get_property("caps").to_string(), "image/jpeg, width=(int)640, height=(int)360")
        self.assertEqual(len(p.children), len(elements) + 2)  # nothing else in it: no tee, no filesink

    def test_the_appsink_keeps_one_buffer_and_drops_old_frames(self):
        sink = Gst.parse_launch(fake(DEV)).get_by_name("sink")
        self.assertEqual((sink.get_property("max-buffers"), sink.get_property("drop")), (1, True))

    def test_start_opens_the_node_it_was_given(self):
        given = []
        page = preview.Preview(DEV, print, print, launch=lambda node: given.append(node) or fake(node))
        self.addCleanup(drain)
        self.addCleanup(page.stop)
        page.start()
        self.assertEqual(given, [DEV])

    def test_no_camera_no_pipeline(self):
        page = preview.Preview(None, print, print, launch=fake)
        page.start()
        self.assertIsNone(page.pipeline)

    def test_tests_can_never_build_the_camera_source(self):
        self.assertIsNone(Gst.ElementFactory.find("v4l2src"))
        failures = []
        preview.Preview(DEV, print, failures.append).start()  # the real launch(): no element, so nothing opens
        self.assertEqual(failures, [preview.UNAVAILABLE])


class Visibility(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.preview = preview.Preview(DEV, self.rec.show, self.rec.failures.append, launch=fake)
        self.addCleanup(drain)
        self.addCleanup(self.preview.stop)

    def test_one_frame_reaches_the_ui_as_a_texture_on_the_main_loop(self):
        self.preview.start()
        self.assertTrue(until(lambda: self.rec.frames), "no frame reached show()")
        texture = self.rec.frames[0]
        self.assertIsInstance(texture, Gdk.MemoryTexture)
        self.assertEqual((texture.get_width(), texture.get_height()), (640, 360))
        self.assertIs(self.rec.threads[0], threading.main_thread())

    def test_frames_keep_coming_after_the_first_is_shown(self):
        self.preview.start()
        self.assertTrue(until(lambda: len(self.rec.frames) >= 3), "the preview froze on its first frame")

    def test_at_most_one_frame_waits_for_the_main_loop(self):
        pulled, queued = [], []
        on_sample = self.preview.on_sample
        self.preview.on_sample = lambda sink: pulled.append(1) or on_sample(sink)
        real_idle_add = GLib.idle_add
        with mock.patch.object(preview.GLib, "idle_add",
                               lambda f, *a: queued.append(f) if f == self.preview.deliver else real_idle_add(f, *a)):
            self.preview.start()
            self.assertTrue(until(lambda: len(pulled) >= 4), "no frames")  # the main loop never ran deliver()
            self.assertEqual(len(queued), 1)

    def test_a_second_start_keeps_the_one_pipeline(self):
        self.preview.start()
        first = self.preview.pipeline
        self.preview.start()
        self.assertIs(self.preview.pipeline, first)

    def test_shown_plays_and_left_releases_the_device(self):
        self.preview.start()
        playing = self.preview.pipeline
        self.assertEqual(state(playing), Gst.State.PLAYING)
        self.preview.stop()
        self.assertIsNone(self.preview.pipeline)
        self.assertEqual(state(playing), Gst.State.NULL)

    def test_left_removes_the_bus_watch(self):
        self.preview.start()
        p = self.preview.pipeline
        self.preview.stop()
        state(p)
        p.get_bus().set_flushing(False)  # NULL flushed the bus: open it again, so only a watch could be missing
        p.post_message(Gst.Message.new_error(p.get_by_name("sink"), error(Gst.ResourceError.BUSY), "debug"))
        until(lambda: self.rec.failures, 0.3)
        self.assertEqual(self.rec.failures, [])

    def test_a_stopped_pipeline_is_freed(self):
        freed = []
        self.preview.start()
        self.assertTrue(until(lambda: self.rec.frames), "no frame")  # the sample handler has run
        self.preview.pipeline.weak_ref(lambda: freed.append(1))
        self.preview.stop()
        drain()
        until(lambda: False, 0.1)  # any frame still queued for the main loop
        gc.collect()
        self.assertEqual(freed, [1])

    def test_a_frame_pulled_before_stop_is_never_shown(self):
        self.preview.start()
        self.assertTrue(until(lambda: self.preview.pending), "no frame was pulled")
        self.preview.stop()
        until(lambda: not self.preview.pending, 1)
        self.assertEqual(self.rec.frames, [])

    def post(self, code):
        p = self.preview.pipeline
        p.post_message(Gst.Message.new_error(p.get_by_name("sink"), error(code), "debug"))
        self.assertTrue(until(lambda: self.rec.failures), "the error never reached failed()")
        return p

    def test_busy_says_in_use_stops_and_retries_when_shown_again(self):
        self.preview.start()
        busy = self.post(Gst.ResourceError.BUSY)
        self.assertEqual(self.rec.failures, ["Camera in use by another app"])
        self.assertIsNone(self.preview.pipeline)
        self.assertEqual(state(busy), Gst.State.NULL)
        self.preview.start()  # the page is shown again
        self.assertEqual(state(self.preview.pipeline), Gst.State.PLAYING)

    def test_unplugged_mid_stream_stops_without_saying_in_use(self):
        self.preview.start()
        gone = self.post(Gst.ResourceError.READ)
        self.assertEqual(self.rec.failures, [preview.UNAVAILABLE])
        self.assertEqual(state(gone), Gst.State.NULL)

    def test_only_the_first_error_counts(self):  # v4l2src follows "busy" with "Internal data stream error"
        self.preview.start()
        p = self.post(Gst.ResourceError.BUSY)
        p.post_message(Gst.Message.new_error(p.get_by_name("sink"), error(Gst.ResourceError.FAILED), "debug"))
        until(lambda: len(self.rec.failures) > 1, 0.3)
        self.assertEqual(self.rec.failures, [preview.IN_USE])


class StateChanges(unittest.TestCase):
    """set_state() runs on the one worker, in order: a stalled camera never blocks the main loop, and a stop wins."""

    def setUp(self):
        self.calls, self.busy, self.overlap, self.gate = [], [], [], threading.Event()
        self.preview = preview.Preview(DEV, print, print, launch=fake)
        self.preview.set_state = self.set_state
        self.addCleanup(drain)
        self.addCleanup(self.gate.set)

    def set_state(self, p, state):  # a camera that takes its time to open
        self.overlap.append(len(self.busy))
        self.busy.append(1)
        self.calls.append((state, threading.current_thread() is threading.main_thread()))
        if state == Gst.State.PLAYING:
            self.gate.wait(5)
        self.busy.pop()
        return Gst.StateChangeReturn.SUCCESS

    def test_a_stop_while_starting_ends_in_null_after_the_start(self):
        self.preview.start()
        self.assertTrue(until(lambda: self.calls), "the start never ran")
        self.preview.stop()  # returns at once: the start is still blocked on the camera
        until(lambda: len(self.calls) > 1, 0.3)
        self.assertEqual(self.calls, [(Gst.State.PLAYING, False)])  # the stop waits for it: never two at once
        self.gate.set()
        drain()
        self.assertEqual(self.calls, [(Gst.State.PLAYING, False), (Gst.State.NULL, False)])
        self.assertEqual(self.overlap, [0, 0])  # never two at once

    def test_a_stop_before_the_start_ran_never_opens_the_camera(self):
        hold = threading.Event()
        preview.JOBS.put(lambda: hold.wait(5))  # the worker is busy elsewhere
        self.preview.start()
        self.preview.stop()
        hold.set()
        drain()
        self.assertEqual(self.calls, [(Gst.State.NULL, False)])

    def test_a_failed_start_says_why(self):
        failures = []
        self.preview.failed = failures.append
        self.preview.set_state = lambda p, s: Gst.StateChangeReturn.FAILURE if s == Gst.State.PLAYING else None
        self.preview.start()
        self.assertTrue(until(lambda: failures), "a failed start said nothing")
        self.assertEqual(failures, [preview.UNAVAILABLE])
        self.assertIsNone(self.preview.pipeline)


class Shown(unittest.TestCase):
    """The page's preview runs only while it is shown and its window is not minimised: wired on a stub, no window."""

    def test_wanted(self):
        self.assertEqual([preview.wanted(m, s) for m in (True, False) for s in (False, True)], [True, False, False, False])

    def test_the_preview_is_16_9_at_the_width_it_gets(self):  # 576: the page's column in a wide window
        self.assertEqual([preview.height(w) for w in (640, 576, 336)], [360, 324, 189])

    def page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from booth.ui.camera import CameraPage

        page, shown = mock.Mock(live=False), []
        page.get_root.return_value = Root()
        page.preview_shown = shown.append
        page.preview_update = lambda *a: CameraPage.preview_update(page, *a)
        return CameraPage, page, shown

    def test_minimised_stops_and_restored_starts_again(self):
        CameraPage, page, shown = self.page()
        root = page.get_root()
        page.get_mapped.return_value = True
        CameraPage.preview_mapped(page, page)
        root.props.suspended = True  # minimised: GTK 4.14 keeps the page mapped
        root.props.suspended = False
        root.notify("suspended")  # nothing changed: the running preview is left alone
        self.assertEqual(shown, [True, False, True])

    def test_left_stops_and_stops_watching_the_window(self):
        CameraPage, page, shown = self.page()
        root = page.get_root()
        page.get_mapped.return_value = True
        CameraPage.preview_mapped(page, page)
        page.get_mapped.return_value = False
        CameraPage.preview_unmapped(page, page)
        self.assertFalse(root.handler_is_connected(page.suspend_watch))  # no handler left on the window
        root.props.suspended = True
        root.props.suspended = False
        self.assertEqual(shown, [True, False])

    def test_a_replug_restarts_a_shown_preview_that_gave_up(self):
        CameraPage = self.page()[0]
        for live, pipeline, restarted in ((True, None, [True]), (False, None, []), (True, object(), [])):
            page, shown = mock.Mock(live=live, ctrls={}), []
            page.setter.reapply.return_value = []
            page.preview.pipeline, page.preview_shown = pipeline, shown.append
            CameraPage.reapply(page)
            self.assertEqual(shown, restarted, (live, pipeline))

    def test_unplugged_says_disconnected_not_that_the_controls_work(self):
        self.assertEqual(preview.notice(preview.UNAVAILABLE, False)[0], preview.DISCONNECTED)
        self.assertNotIn("still work", preview.notice(preview.UNAVAILABLE, False)[1])
        self.assertEqual(preview.notice(preview.IN_USE, True), (preview.IN_USE, "The controls below still work"))
        CameraPage, page = self.page()[0], mock.Mock(node=DEV)  # no such by-id link here: unplugged
        CameraPage.preview_failed(page, preview.UNAVAILABLE)
        page.problem.set_title.assert_called_once_with(preview.DISCONNECTED)


class Wired(unittest.TestCase):
    """The page's real preview wiring (preview_group's signals down to preview_shown) on a plain object: a widget
    needs a display, so Gtk and Adw are stubbed and Preview records what the page asks of it."""

    def test_the_page_previews_its_own_node_from_map_to_unmap(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from booth.ui import camera as ui
        calls = []

        class Rec:
            def __init__(self, node, show, failed, launch=None):
                calls.append(node)

            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")

        class Page:
            P = ui.CameraPage
            preview_group, preview_mapped, preview_unmapped = P.preview_group, P.preview_mapped, P.preview_unmapped
            preview_update, preview_shown, preview_failed = P.preview_update, P.preview_shown, P.preview_failed
            node, handlers, mapped, window = DEV, {}, False, Root()

            def connect(self, signal, handler):
                self.handlers[signal] = handler

            def emit(self, signal):  # what GTK does as the page is shown and left
                self.mapped = signal == "map"
                self.handlers.get(signal, lambda _page: None)(self)

            def get_mapped(self):
                return self.mapped

            def get_root(self):
                return self.window

        page = Page()
        with mock.patch.object(ui, "Gtk"), mock.patch.object(ui, "Adw"), mock.patch.object(preview, "Preview", Rec):
            page.preview_group()
        page.emit("map")
        page.emit("unmap")
        self.assertEqual(calls, [DEV, "start", "stop"])


class Problem(unittest.TestCase):
    def test_busy_is_in_use(self):
        self.assertEqual(preview.problem(error(Gst.ResourceError.BUSY)), "Camera in use by another app")

    def test_anything_else_is_unavailable(self):
        self.assertEqual(preview.problem(error(Gst.ResourceError.NOT_FOUND)), preview.UNAVAILABLE)


class Overlay(unittest.TestCase):
    def test_zoom_and_field_of_view(self):
        ctrls = brio()
        self.assertEqual(preview.overlay(ctrls, {"zoom_absolute": 200, "fov": 1}), "2.0× · 78°")

    def test_without_the_extension_units_only_zoom(self):
        ctrls = {n: c for n, c in brio().items() if n not in XU}
        self.assertEqual(preview.overlay(ctrls, {"zoom_absolute": 100}), "1.0×")


if __name__ == "__main__":
    unittest.main()
