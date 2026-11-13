"""The Microphone page's raw "before" meter: GStreamer pipewiresrc on the Scarlett's capture node (by name) → all its
channels unmixed, in node order → level every 50 ms → only the mic channel's peak and decay, handed to the widgets on
the main loop. Nothing is recorded or kept. It runs only while the page or the sidebar meter is on screen, so the
Scarlett is held open only while someone is looking. The scale and the target zone are pure helpers here."""
import math

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from booth import scarlett  # noqa: E402
from booth.preview import JOBS  # noqa: E402  one state-change worker per process, shared with the preview

Gst.init(None)
FLOOR = -60.0  # dB at the bottom of the bar
ZONE = (-18.0, -6.0)  # peaks around −12 dB
COACHING = "Turn the Gain knob on the Scarlett until peaks land in the zone."
NOT_ROUTED = "The mic isn't routed to a capture channel"
NO_SOURCE = "The Scarlett's input isn't in PipeWire"
UNAVAILABLE = "Level meter unavailable"
TAIL = f"level name=level interval={50 * Gst.MSECOND} post-messages=true ! fakesink sync=false"


def position(db):
    """dB → 0..1 along the bar: linear from FLOOR to 0 dB, clamped. Silence, -inf and NaN are 0."""
    if db is None or math.isnan(db):
        return 0.0
    return min(1.0, max(0.0, (db - FLOOR) / -FLOOR))


def zone():
    """The target zone's (low, high) along the bar."""
    return position(ZONE[0]), position(ZONE[1])


def label(name, db):
    """A meter's accessible label: its name and the dB value, e.g. "Before processing: −12 dB"."""
    if db is None or math.isnan(db) or db <= FLOOR:
        return f"{name}: silent"
    return f"{name}: {round(db)} dB".replace("-", "−")


def pick(structure, channel):
    """A level message → the mic channel's (peak, decay) in dB (channel is 1-based), or None."""
    if structure.get_name() != "level":
        return None
    peak, decay = structure.get_value("peak"), structure.get_value("decay")
    return (peak[channel - 1], decay[channel - 1]) if channel <= min(len(peak), len(decay)) else None


def source(state):
    """A Microphone page poll (scarlett.read) → (the mic record to meter, or None; what to say instead, or None)."""
    if state is None:
        return None, scarlett.UNPLUGGED
    mic = state["mic"]
    if mic is None:
        return None, NOT_ROUTED
    if mic["node"] is None:
        return None, NO_SOURCE
    return mic, None


def wanted(page, sidebar, suspended):
    """Run while the page or the sidebar meter is mapped, never while the window is minimised (GTK 4.14 keeps a
    minimised window's widgets mapped)."""
    return bool(page or sidebar) and not suspended


def launch(node, channels):
    """The Scarlett's capture node by name; every channel as the node has them (unpositioned: no downmix), so the mic
    channel is level's entry channel − 1. dont-reconnect: a missing node never falls back to the default mic."""
    return (f'pipewiresrc name=src target-object="{node}" stream-properties="props,node.dont-reconnect=(string)true"'
            f" ! audio/x-raw,channels={channels},channel-mask=(bitmask)0x0 ! {TAIL}")


class Meter:
    """want(record, visible) from the main loop after each poll and visibility change. post(peak, decay) and
    failed(text) are called on the main loop only. `launch(node, channels)` gives the pipeline's description (tests
    inject a test source: never the Scarlett)."""

    def __init__(self, post, failed, launch=launch):
        self.post, self.failed, self.launch = post, failed, launch
        self.pipeline, self.key = None, None

    def want(self, record, visible):
        key = (record["node"], record["channel"], record["channels"]) if record and visible else None
        if key != self.key:
            self.stop()
            if key:
                self.start(key)

    def start(self, key):
        try:
            p = Gst.parse_launch(self.launch(key[0], key[2]))
        except GLib.Error:  # a GStreamer plugin missing
            self.failed(UNAVAILABLE)
            return
        bus = p.get_bus()
        bus.add_signal_watch()  # levels and errors arrive on the main loop
        bus.connect("message::element", self.on_message)
        bus.connect("message::error", self.on_error)
        self.pipeline, self.key = p, key
        JOBS.put(lambda: self.change(p, Gst.State.PLAYING))

    def stop(self):
        p, self.pipeline, self.key = self.pipeline, None, None
        if p is not None:
            p.get_bus().remove_signal_watch()
            JOBS.put(lambda: p.set_state(Gst.State.NULL))  # closes the stream, after any start still running

    def change(self, p, state):  # the worker
        if p is self.pipeline and p.set_state(state) == Gst.StateChangeReturn.FAILURE:
            GLib.idle_add(self.start_failed, p)

    def start_failed(self, p):
        if p is self.pipeline:
            self.stop()
            self.failed(UNAVAILABLE)
        return GLib.SOURCE_REMOVE

    def on_message(self, bus, message):
        if self.pipeline is not None and bus is self.pipeline.get_bus():  # never a level from a stopped pipeline
            values = pick(message.get_structure(), self.key[1])
            if values:
                self.post(*values)

    def on_error(self, _bus, _message):
        self.stop()
        self.failed(UNAVAILABLE)
