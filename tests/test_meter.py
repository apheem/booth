"""The before-meter, headless: dB → position on the bar, the target zone around −12 dB, the mic channel's value picked
out of a level message, what runs when, and a level pipeline over an injected test source (never the Scarlett)."""
import math
import threading
import unittest

from gi.repository import GLib

from booth import meter
from booth.meter import Gst
from tests.test_preview import until
from tests.test_scarlett import SOURCE

# No test can open a PipeWire stream, even through a guard that regressed: pipewiresrc is gone from this registry.
_pw = Gst.Registry.get().find_feature("pipewiresrc", Gst.ElementFactory)
if _pw is not None:
    Gst.Registry.get().remove_feature(_pw)

XLR = {"input": "XLR", "channel": 2, "position": "FR", "node": SOURCE, "channels": 4}
# A 4-channel source with a −12 dB sine on channel 2 only, like the XLR on the Scarlett's FR; the rest silent.
ONLY_2 = ('audiotestsrc name=src is-live=true volume=0.25 ! audio/x-raw,channels=1 ! audioconvert'
          ' mix-matrix="<<(float)0.0>, <(float)1.0>, <(float)0.0>, <(float)0.0>>"'
          ' ! audio/x-raw,channels=4')


def fake(_node, _channels):
    return f"{ONLY_2} ! {meter.TAIL}"


def drain():
    done = threading.Event()
    meter.JOBS.put(done.set)
    assert done.wait(5), "the worker is stuck"


def state(p):
    drain()
    return p.get_state(2 * Gst.SECOND)[1]


class Scale(unittest.TestCase):
    def test_db_maps_linearly_from_the_floor_to_full_scale(self):
        self.assertEqual(meter.position(meter.FLOOR), 0.0)
        self.assertEqual(meter.position(0.0), 1.0)
        self.assertAlmostEqual(meter.position(-30.0), 0.5)
        self.assertAlmostEqual(meter.position(-12.0), 0.8)

    def test_below_the_floor_or_above_zero_is_clamped(self):
        self.assertEqual(meter.position(-349.99), 0.0)  # what level posts for digital silence
        self.assertEqual(meter.position(3.0), 1.0)

    def test_silence_and_nonsense_are_the_bottom(self):
        for db in (-math.inf, math.nan, None):
            self.assertEqual(meter.position(db), 0.0)

    def test_the_zone_is_6_db_either_side_of_minus_12(self):
        self.assertEqual(meter.ZONE, (-18.0, -6.0))
        self.assertAlmostEqual(meter.zone()[0], 0.7)
        self.assertAlmostEqual(meter.zone()[1], 0.9)
        low, high = meter.zone()
        self.assertTrue(low < meter.position(-12.0) < high)

    def test_the_label_says_the_db_value(self):
        self.assertEqual(meter.label(-12.4), "Input level −12 dB")
        self.assertEqual(meter.label(-0.2), "Input level 0 dB")
        self.assertEqual(meter.label(-75.0), "Input level silent")  # below the bottom of the bar
        self.assertEqual(meter.label(-349.99), "Input level silent")
        self.assertEqual(meter.label(None), "Input level silent")


class Pick(unittest.TestCase):
    def message(self, peak, decay):
        s = Gst.Structure.new_empty("level")
        s.set_value("peak", peak)
        s.set_value("decay", decay)
        return s

    def test_only_the_mic_channels_peak_and_decay(self):
        s = self.message([-80.0, -14.0, -350.0, -350.0], [-79.0, -10.0, -350.0, -350.0])
        self.assertEqual(meter.pick(s, 2), (-14.0, -10.0))
        self.assertEqual(meter.pick(s, 1), (-80.0, -79.0))

    def test_a_channel_the_message_lacks_is_none(self):
        self.assertIsNone(meter.pick(self.message([-80.0], [-80.0]), 2))

    def test_another_message_is_none(self):
        self.assertIsNone(meter.pick(Gst.Structure.new_empty("GstMessageStreamStatus"), 2))


class Source(unittest.TestCase):
    def test_the_routed_mic_runs(self):
        self.assertEqual(meter.source({"ctrls": {}, "mic": XLR}), (XLR, None))

    def test_unplugged_says_so(self):
        self.assertEqual(meter.source(None), (None, "Scarlett not connected — plug it in"))

    def test_not_routed_says_so_and_nothing_runs(self):
        self.assertEqual(meter.source({"ctrls": {}, "mic": None}), (None, meter.NOT_ROUTED))

    def test_no_pipewire_source_says_so_and_nothing_runs(self):
        self.assertEqual(meter.source({"ctrls": {}, "mic": dict(XLR, node=None)}), (None, meter.NO_SOURCE))


class Wanted(unittest.TestCase):
    def test_runs_while_the_page_or_the_sidebar_meter_is_on_screen(self):
        self.assertTrue(meter.wanted(True, False, False))
        self.assertTrue(meter.wanted(False, True, False))
        self.assertFalse(meter.wanted(False, False, False))

    def test_never_while_the_window_is_minimised(self):
        self.assertFalse(meter.wanted(True, True, True))


class Pipeline(unittest.TestCase):
    def test_the_scarlett_by_node_name_all_its_channels_unmixed_at_20_per_second(self):
        text = meter.launch(SOURCE, 4)
        self.assertTrue(text.startswith(f'pipewiresrc name=src target-object="{SOURCE}" '), text)
        self.assertIn('stream-properties="props,node.dont-reconnect=(string)true"', text)  # never the default mic
        self.assertIn(" ! audio/x-raw,channels=4,channel-mask=(bitmask)0x0 ! ", text)  # node order, no downmix
        self.assertTrue(text.endswith(meter.TAIL))
        level = Gst.parse_launch(fake(SOURCE, 4)).get_by_name("level")
        self.assertEqual(level.get_property("interval"), 50 * Gst.MSECOND)
        self.assertTrue(level.get_property("post-messages"))

    def test_tests_can_never_build_the_pipewire_source(self):
        self.assertIsNone(Gst.ElementFactory.find("pipewiresrc"))
        failures = []
        m = meter.Meter(print, failures.append)
        m.want(XLR, True)  # the real launch(): no element, so nothing opens
        self.assertEqual((m.pipeline, failures), (None, [meter.UNAVAILABLE]))


class Running(unittest.TestCase):
    def setUp(self):
        self.levels, self.threads, self.failures, self.launched = [], [], [], []
        self.meter = meter.Meter(self.post, self.failures.append,
                                 launch=lambda n, c: self.launched.append((n, c)) or fake(n, c))
        self.addCleanup(drain)
        self.addCleanup(self.meter.stop)

    def post(self, peak, decay):
        self.levels.append((peak, decay))
        self.threads.append(threading.current_thread())

    def test_the_mic_channels_level_reaches_the_ui_on_the_main_loop(self):
        self.meter.want(XLR, True)
        self.assertEqual(self.launched, [(SOURCE, 4)])
        self.assertTrue(until(lambda: len(self.levels) >= 3), "no level reached post()")
        peak, decay = self.levels[-1]
        self.assertAlmostEqual(peak, -12.0, delta=0.5)  # channel 2's sine, not the silent channels
        self.assertGreaterEqual(decay, peak - 0.01)
        self.assertIs(self.threads[0], threading.main_thread())

    def test_another_channel_reads_that_channel(self):
        self.meter.want(dict(XLR, channel=1), True)
        self.assertTrue(until(lambda: self.levels), "no level")
        self.assertLess(self.levels[0][0], meter.FLOOR)

    def test_hidden_or_not_routed_runs_nothing(self):
        self.meter.want(XLR, False)
        self.meter.want(None, True)
        self.assertEqual((self.meter.pipeline, self.launched), (None, []))

    def test_hidden_again_releases_the_scarlett(self):
        self.meter.want(XLR, True)
        p = self.meter.pipeline
        self.assertEqual(state(p), Gst.State.PLAYING)
        self.meter.want(XLR, False)
        self.assertIsNone(self.meter.pipeline)
        self.assertEqual(state(p), Gst.State.NULL)

    def test_unplugged_stops_and_replugged_starts_again(self):
        self.meter.want(XLR, True)
        first = self.meter.pipeline
        self.meter.want(None, True)
        self.assertEqual(state(first), Gst.State.NULL)
        self.meter.want(XLR, True)
        self.assertEqual(state(self.meter.pipeline), Gst.State.PLAYING)

    def test_the_same_mic_keeps_its_pipeline_and_a_new_one_restarts_it(self):
        self.meter.want(XLR, True)
        first = self.meter.pipeline
        self.meter.want(dict(XLR), True)  # the next poll: same node, same channel
        self.assertIs(self.meter.pipeline, first)
        self.meter.want(dict(XLR, channel=1), True)  # the input changed to Instrument
        self.assertIsNot(self.meter.pipeline, first)
        self.assertEqual(state(first), Gst.State.NULL)

    def test_a_level_from_a_stopped_pipeline_is_never_shown(self):
        self.meter.want(XLR, True)
        p = self.meter.pipeline
        self.meter.stop()
        drain()
        loud = Pick.message(None, [-1.0, -1.0], [-1.0, -1.0])
        self.meter.on_message(p.get_bus(), Gst.Message.new_element(p.get_by_name("level"), loud))
        self.assertEqual(self.levels, [])

    def test_a_level_from_the_old_pipeline_after_a_retarget_is_never_shown(self):
        self.meter.want(XLR, True)
        old = self.meter.pipeline
        self.meter.want(dict(XLR, channel=1), True)  # a new pipeline now runs
        self.assertIsNotNone(self.meter.pipeline)
        loud = Pick.message(None, [-1.0, -1.0], [-1.0, -1.0])
        self.meter.on_message(old.get_bus(), Gst.Message.new_element(old.get_by_name("level"), loud))
        self.assertNotIn((-1.0, -1.0), self.levels)

    def test_a_start_that_fails_says_so(self):
        self.meter.on_error = lambda *_: None  # only set_state's FAILURE may report it, not the bus error
        self.meter.launch = lambda n, c: f"filesrc location=/nonexistent/booth-meter ! {meter.TAIL}"
        self.meter.want(XLR, True)
        self.assertTrue(until(lambda: self.failures), "a failed start never reached failed()")
        self.assertEqual(self.failures, [meter.UNAVAILABLE])
        self.assertIsNone(self.meter.pipeline)

    def test_an_error_stops_and_says_so(self):
        self.meter.want(XLR, True)
        p = self.meter.pipeline
        err = GLib.Error.new_literal(Gst.ResourceError.quark(), "x", Gst.ResourceError.READ)
        p.post_message(Gst.Message.new_error(p.get_by_name("level"), err, "debug"))
        self.assertTrue(until(lambda: self.failures), "the error never reached failed()")
        self.assertEqual(self.failures, [meter.UNAVAILABLE])
        self.assertIsNone(self.meter.pipeline)
        self.assertEqual(state(p), Gst.State.NULL)


if __name__ == "__main__":
    unittest.main()
