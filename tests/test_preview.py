"""The Camera page's live preview, headless: the pipeline's elements, its state as the page is shown and left, the
"in use" error, and one frame pulled into a texture with no window (an injected test source, never a camera)."""
import threading
import time
import unittest

from gi.repository import GLib

from booth import preview
from booth.preview import Gdk, Gst
from tests.test_camera import XU, brio

DEV = "/dev/v4l/by-id/usb-046d_Logitech_BRIO_FAKESER1-video-index0"
TEST_SOURCE = "videotestsrc is-live=true ! jpegenc"  # MJPEG like the BRIO's, so the rest of the pipeline is the real one


def until(check, seconds=5):
    """Run the main loop until check() is true; False on timeout."""
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)
    return check()


def state(pipeline):
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


class Recorder:
    def __init__(self):
        self.frames, self.threads, self.failures = [], [], []

    def show(self, texture):
        self.frames.append(texture)
        self.threads.append(threading.current_thread())


class Pipeline(unittest.TestCase):
    def test_streams_mjpeg_640x360_from_the_node_it_is_given(self):
        p = preview.Preview(DEV, print, print).build()  # stays in NULL: the device is never opened
        self.addCleanup(p.set_state, Gst.State.NULL)
        elements = chain(p)
        self.assertEqual([e.get_factory().get_name() for e in elements], ["v4l2src", "capsfilter", "jpegdec", "videoconvert", "capsfilter", "appsink"])
        self.assertEqual(p.get_by_name("src").get_property("device"), DEV)
        self.assertEqual(elements[1].get_property("caps").to_string(), "image/jpeg, width=(int)640, height=(int)360")
        self.assertEqual(len(p.children), len(elements))  # nothing else in it: no tee, no filesink

    def test_the_appsink_keeps_one_buffer_and_drops_old_frames(self):
        p = preview.Preview(DEV, print, print).build()
        self.addCleanup(p.set_state, Gst.State.NULL)
        sink = p.get_by_name("sink")
        self.assertEqual((sink.get_property("max-buffers"), sink.get_property("drop")), (1, True))

    def test_no_camera_no_pipeline(self):
        page = preview.Preview(None, print, print)
        page.start()
        self.assertIsNone(page.pipeline)


class Visibility(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.preview = preview.Preview(DEV, self.rec.show, self.rec.failures.append, source=TEST_SOURCE)
        self.addCleanup(self.preview.stop)

    def test_one_frame_reaches_the_ui_as_a_texture_on_the_main_loop(self):
        self.preview.start()
        self.assertTrue(until(lambda: self.rec.frames), "no frame reached show()")
        texture = self.rec.frames[0]
        self.assertIsInstance(texture, Gdk.MemoryTexture)
        self.assertEqual((texture.get_width(), texture.get_height()), (640, 360))
        self.assertIs(self.rec.threads[0], threading.main_thread())

    def test_shown_plays_and_left_releases_the_device(self):
        self.preview.start()
        playing = self.preview.pipeline
        self.assertEqual(state(playing), Gst.State.PLAYING)
        self.preview.stop()
        self.assertIsNone(self.preview.pipeline)
        self.assertEqual(state(playing), Gst.State.NULL)

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
