"""The Camera page's live preview: GStreamer v4l2src on the BRIO's color node → MJPEG 640×360 → jpegdec → videoconvert →
appsink (one buffer, old frames dropped) → a Gdk.MemoryTexture handed to the page on the main loop. No widgets here, and
no frame ever goes anywhere but memory. The page starts it while it is on screen and stops it otherwise, so the camera
is held only while someone is looking."""
import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gst  # noqa: E402

from booth import camera  # noqa: E402

Gst.init(None)
SOURCE = "v4l2src name=src"  # its device is set from the node discovered by model: never a fixed /dev/videoN
TAIL = ("image/jpeg,width=640,height=360 ! jpegdec ! videoconvert ! video/x-raw,format=RGBx"
        " ! appsink name=sink max-buffers=1 drop=true sync=false emit-signals=true")
IN_USE = "Camera in use by another app"
UNAVAILABLE = "Preview unavailable"


def problem(err):
    """What the page says for a pipeline error: another app streaming (Zoom) is BUSY; anything else (unplugged)."""
    return IN_USE if err.matches(Gst.ResourceError.quark(), Gst.ResourceError.BUSY) else UNAVAILABLE


def overlay(ctrls, values):
    """The small text over the preview, e.g. "2.0× · 78°": zoom, and the field of view when the camera has it."""
    parts = [camera.zoom_label(ctrls[camera.ZOOM], values[camera.ZOOM])]
    if "fov" in ctrls and values.get("fov") in ctrls["fov"]["menu"]:
        parts.append(ctrls["fov"]["menu"][values["fov"]])
    return " · ".join(parts)


def texture(sample):
    """An RGBx sample as a texture (RGBx's stride is width × 4: four-byte pixels are always aligned)."""
    s = sample.get_caps().get_structure(0)
    width, height, buf = s.get_value("width"), s.get_value("height"), sample.get_buffer()
    ok, info = buf.map(Gst.MapFlags.READ)
    try:
        data = GLib.Bytes.new(info.data)
    finally:
        buf.unmap(info)
    return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8X8, data, width * 4)


class Preview:
    """start() / stop() from the main loop. show(texture) and failed(text) are called on the main loop only.
    `source` replaces the camera (tests inject an MJPEG test source)."""

    def __init__(self, node, show, failed, source=None):
        self.node, self.show, self.failed, self.source = node, show, failed, source
        self.pipeline, self.pending = None, False

    def build(self):
        p = Gst.parse_launch(f"{self.source or SOURCE} ! {TAIL}")
        if self.source is None:
            p.get_by_name("src").set_property("device", self.node)
        return p

    def start(self):
        if self.pipeline is not None or (self.node is None and self.source is None):
            return
        try:
            p = self.build()
        except GLib.Error:  # a GStreamer plugin missing
            self.failed(UNAVAILABLE)
            return
        p.get_by_name("sink").connect("new-sample", self.on_sample, p)
        bus = p.get_bus()
        bus.add_signal_watch()  # errors arrive on the main loop, even one posted while set_state() runs
        bus.connect("message::error", self.on_error)
        self.pipeline = p
        p.set_state(Gst.State.PLAYING)

    def stop(self):
        p, self.pipeline = self.pipeline, None
        if p is not None:
            p.get_bus().remove_signal_watch()  # else each page visit leaves a watch on the main loop
            p.set_state(Gst.State.NULL)  # closes the device, and flushes the bus: v4l2src's follow-up error is dropped

    def on_sample(self, sink, pipeline):  # the streaming thread: no widgets, no texture, only a hand-off
        sample = sink.emit("pull-sample")
        if sample is not None and not self.pending:  # one frame queued at most: a busy main loop skips frames
            self.pending = True
            GLib.idle_add(self.deliver, pipeline, sample)
        return Gst.FlowReturn.OK

    def deliver(self, pipeline, sample):
        self.pending = False
        if pipeline is self.pipeline:  # a frame pulled before stop() is dropped
            self.show(texture(sample))
        return GLib.SOURCE_REMOVE

    def on_error(self, _bus, message):
        err, _debug = message.parse_error()
        self.stop()
        self.failed(problem(err))
