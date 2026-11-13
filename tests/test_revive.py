"""The Scarlett's stuck output: heard by one passive listener on the sink's monitor against the device's own Level
Meter, restarted by one pair of silent streams at another rate. Fixtures and fakes only: no pw-record, pw-play or
amixer runs here."""
import array
import os
import unittest
from unittest import mock

from booth import revive
from tests.test_headset import DUMP, PRO_X, SCARLETT, dump, node

SOURCE = "alsa_input.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-surround-40"
# `amixer -c Gen cget iface=PCM,name='Level Meter'` as captured: playing, then stuck (only the mic's points move)
METER = ("numid=46,iface=PCM,name='Level Meter'\n  ; type=INTEGER,access=r--v----,values=12,min=0,max=4095,step=1\n"
         "  : values={}\n")
PLAYING, STUCK = METER.format("25,25,25,25,0,3,0,3,0,3,25,25"), METER.format("0,0,0,0,0,1,0,1,0,1,0,0")
GAIN = 0.438993  # the captured sink's volume


def floats(*values):
    return array.array("f", values).tobytes()


LOUD, QUIET = floats(0.2, -0.4), floats(0.001, 0.0)  # 0.4 x GAIN is well above SENT_MIN; 0.001 never is


class Nodes(unittest.TestCase):
    def test_the_scarletts_sink_and_source_by_name_its_id_and_its_volume(self):
        found = revive.nodes(DUMP)
        self.assertEqual((found["sink"], found["source"], found["id"]), (SCARLETT, SOURCE, 176))
        self.assertAlmostEqual(found["gain"], GAIN)

    def test_a_muted_sink_sends_nothing(self):
        muted = dump(lambda o: node(o, SCARLETT)["info"]["params"]["Props"][0].update(mute=True))
        self.assertEqual(revive.nodes(muted)["gain"], 0.0)

    def test_no_scarlett_sink_or_no_pipewire_is_none_and_a_missing_source_is_said(self):
        self.assertIsNone(revive.nodes(dump(lambda o: o.remove(node(o, SCARLETT)))))
        self.assertIsNone(revive.nodes(""))
        self.assertIsNone(revive.nodes(dump(lambda o: o.remove(node(o, SOURCE))))["source"])

    def test_the_apps_playing_or_recording_now_are_named_for_the_log(self):
        def spotify(objects):
            objects.append({"id": 900, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
                "media.class": "Stream/Output/Audio", "application.name": "Spotify"}}})
            objects.append({"id": 901, "type": "PipeWire:Interface:Node", "info": {"state": "idle", "props": {
                "media.class": "Stream/Output/Audio", "application.name": "Idle"}}})
        self.assertEqual(revive.nodes(dump(spotify))["apps"], ["Spotify"])

    def test_the_pro_x_is_never_the_one_watched(self):
        self.assertNotIn(PRO_X, str(revive.nodes(DUMP)))


class Meter(unittest.TestCase):
    def test_the_level_meters_points(self):
        self.assertEqual(revive.parse_meter(PLAYING), [25, 25, 25, 25, 0, 3, 0, 3, 0, 3, 25, 25])
        self.assertEqual(revive.parse_meter("amixer: Cannot find the given element from control sysdefault:6\n"), [])

    def test_the_peak_of_what_was_heard(self):
        self.assertAlmostEqual(revive.peak(floats(0.0, -0.25, 0.1)), 0.25)
        self.assertEqual(revive.peak(b""), 0.0)
        self.assertAlmostEqual(revive.peak(floats(0.5) + b"\x01"), 0.5)  # a torn last sample is dropped


class Listener(unittest.TestCase):
    def test_one_passive_internal_stream_on_the_sinks_monitor_by_name_and_never_another_one(self):
        with mock.patch.object(revive.subprocess, "Popen") as popen:
            revive.listen(SCARLETT)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:3], ["pw-record", "--target", SCARLETT])
        props = cmd[cmd.index("-P") + 1]
        for wanted in ("stream.capture.sink = true",  # the output's monitor, not a microphone
                       "node.dont-reconnect = true",  # never moved to another node, a mic least of all
                       "node.passive = true",  # the Scarlett still sleeps when nothing plays
                       "media.class = Stream/Input/Audio/Internal"):  # no recording indicator, in no app's list
            self.assertIn(wanted, props)
        self.assertEqual((cmd[cmd.index("--channels") + 1], cmd[cmd.index("--rate") + 1], cmd[cmd.index("--format") + 1]),
                         ("1", "8000", "f32"))
        self.assertEqual(revive.CHUNK, int(8000 * revive.CHUNK_S) * 4)  # hear()'s chunk is CHUNK_S of that


class Hearing(unittest.TestCase):
    """Watch.hear, a chunk at a time: the meter is whatever self.meter says, the volume whatever self.dump gives."""

    def setUp(self):
        self.meter, self.dump, self.now, self.card = PLAYING, DUMP, 100.0, "Gen"
        self.asked, self.restarts, self.logged = [], [], []
        self.watch = revive.Watch(None, self.tool, lambda: self.card, self.restart, None, lambda: self.now,
                                  self.logged.append)
        self.watch.found, self.watch.dumped = revive.nodes(DUMP), self.now

    def tool(self, *cmd):
        self.asked.append(cmd[0])
        return self.dump if cmd[0] == "pw-dump" else self.meter

    def restart(self, found):
        self.restarts.append(found["sink"])
        self.now += 0.56  # as measured

    def hear(self, *chunks):
        return [self.watch.hear(chunk) for chunk in chunks]

    def test_sound_heard_inside_the_device_does_nothing_and_the_meter_is_read_sparingly(self):
        self.assertEqual(set(self.hear(*[LOUD] * 13)), {None})
        self.assertEqual(self.asked, ["amixer"] * 3)  # at SUSTAIN chunks, then every EVERY: 3, 8, 13
        self.assertEqual(self.restarts, [])

    def test_silence_and_quiet_sound_are_unknown_and_the_meter_is_not_even_read(self):
        self.meter = STUCK
        self.hear(*[QUIET] * 20)
        self.assertEqual((self.asked, self.restarts), ([], []))

    def test_unbroken_sound_and_a_dead_meter_twice_is_restarted_within_half_a_second_of_sound(self):
        self.meter = STUCK
        self.assertEqual(set(self.hear(*[LOUD] * 4)), {None})  # 3 chunks to judge, the 4th confirms: 0.4 s of sound
        self.assertEqual(self.restarts, [SCARLETT])
        self.assertIn("stuck", self.logged[0])
        self.assertEqual(self.asked, ["amixer", "pw-dump", "amixer"])  # the volume read again before believing it

    def test_a_pause_between_the_two_reads_starts_over(self):  # speech: the meter may have been read in the gap
        self.meter = STUCK
        self.hear(LOUD, LOUD, LOUD, QUIET, LOUD, LOUD, LOUD, QUIET)
        self.assertEqual(self.restarts, [])

    def test_sound_too_short_to_judge_is_never_judged(self):  # the device's own buffer is behind the monitor
        self.meter = STUCK
        self.hear(*[LOUD, LOUD, QUIET] * 5)
        self.assertEqual((self.asked, self.restarts), ([], []))

    def test_a_volume_turned_down_since_it_was_read_is_not_a_dead_device(self):
        self.meter = STUCK
        self.dump = dump(lambda o: node(o, SCARLETT)["info"]["params"]["Props"][0].update(channelVolumes=[0.001] * 2))
        self.hear(*[LOUD] * 10)
        self.assertEqual(self.restarts, [])

    def test_a_volume_turned_up_since_it_was_read_is_followed(self):
        self.watch.found["gain"], self.meter = 0.0001, STUCK
        self.hear(*[LOUD] * 4)
        self.assertEqual(self.restarts, [])  # still the old volume: too quiet to judge
        self.now += revive.GAIN_S + 1
        self.hear(*[LOUD] * 4)
        self.assertEqual(self.restarts, [SCARLETT])

    def test_a_muted_sink_is_never_judged(self):
        self.watch.found["gain"], self.meter = 0.0, STUCK
        self.hear(*[LOUD] * 10)
        self.assertEqual(self.restarts, [])

    def test_a_meter_that_cannot_be_read_or_has_another_layout_is_never_judged(self):
        for self.meter in ("", METER.format("0,0,0,0,0,0,0,0")):
            self.hear(*[LOUD] * 10)
        self.card, self.meter = None, STUCK
        self.hear(*[LOUD] * 10)
        self.assertEqual(self.restarts, [])

    def test_the_mics_points_moving_do_not_count_as_playback(self):
        self.meter = METER.format("0,0,0,0,900,900,900,900,900,900,0,0")
        self.hear(*[LOUD] * 4)
        self.assertEqual(self.restarts, [SCARLETT])

    def stuck_then(self, meter):
        """A restart, then the chunks that piled up while it ran, then sound with `meter`."""
        self.meter = STUCK
        self.hear(*[LOUD] * 4)
        self.meter = meter
        return self.hear(*[LOUD] * 14)

    def test_a_restart_is_said_once_the_device_is_heard_again(self):
        said = self.stuck_then(PLAYING)
        self.assertEqual([s for s in said if s], [revive.REVIVED])
        self.assertEqual(said[:7], [None] * 7)  # 0.56 s of backlog is skipped: it is older than the meter

    def test_a_second_stop_in_the_same_call_is_restarted_again_at_once(self):
        self.stuck_then(PLAYING)
        self.meter = STUCK
        self.hear(*[LOUD] * 8)
        self.assertEqual(self.restarts, [SCARLETT] * 2)

    def test_a_restart_that_did_not_help_says_to_replug_once_and_is_not_repeated_until_the_device_is_heard(self):
        said = self.stuck_then(STUCK) + self.hear(*[LOUD] * 40)
        self.assertEqual(([s for s in said if s], self.restarts), ([revive.GAVE_UP], [SCARLETT]))
        # the volume read once per verdict (the stop, the restart's check), never over and over once given up
        self.assertEqual(self.asked.count("pw-dump"), 2)
        self.meter = PLAYING  # replugged
        self.hear(*[LOUD] * 8)
        self.meter = STUCK
        self.hear(*[LOUD] * 8)
        self.assertEqual(self.restarts, [SCARLETT] * 2)  # a new stop gets its own restart


class Following(unittest.TestCase):
    def watch(self, text=DUMP):
        self.said, self.text, self.procs = [], text, []
        return revive.Watch(self.said.append, lambda *cmd: self.text, lambda: "Gen", None, self.listen,
                            lambda: 100.0, lambda text: None)

    def listen(self, sink):
        proc = mock.Mock(sink=sink)
        proc.stdout.read.side_effect = [LOUD.ljust(revive.CHUNK, b"\0"), b"x"]  # one chunk, then the stream ends
        self.procs.append(proc)
        return proc

    def test_the_listener_starts_on_the_scarletts_sink_hears_every_chunk_and_ends_its_stream(self):
        watch = self.watch()
        with mock.patch.object(watch, "hear", return_value=revive.REVIVED) as hear:
            watch.ensure()
            watch.thread.join(2)
        self.assertEqual([p.sink for p in self.procs], [SCARLETT])
        hear.assert_called_once_with(LOUD.ljust(revive.CHUNK, b"\0"))
        self.assertEqual(self.said, [revive.REVIVED])
        self.procs[0].kill.assert_called_once_with()  # the stream never outlives its thread
        self.procs[0].stdout.read.assert_called_with(revive.CHUNK)

    def test_no_scarlett_or_an_unreadable_dump_starts_nothing(self):
        for text in (dump(lambda o: o.remove(node(o, SCARLETT))), ""):
            watch = self.watch(text)
            watch.ensure()
            self.assertEqual((self.procs, watch.thread), ([], None))

    def test_a_listener_that_runs_is_left_alone_and_one_on_a_replaced_sink_is_ended(self):
        watch = self.watch()
        watch.found, watch.proc, watch.thread = revive.nodes(DUMP), mock.Mock(), mock.Mock()
        watch.ensure()
        self.text = ""  # pw-dump unreadable this once
        watch.ensure()
        self.assertEqual((self.procs, watch.proc.kill.called), ([], False))
        self.text = dump(lambda o: node(o, SCARLETT).update(id=301))  # replugged: the same name, a new node
        watch.ensure()
        watch.proc.kill.assert_called_once_with()
        self.assertEqual(self.procs, [])  # the next tick starts the new one, once that thread has ended

    def test_an_ended_listener_is_started_again(self):
        watch = self.watch()
        watch.thread = mock.Mock(is_alive=lambda: False)
        with mock.patch.object(watch, "hear", return_value=None):
            watch.ensure()
            watch.thread.join(2)
        self.assertEqual(len(self.procs), 1)

    def test_stop_ends_the_stream_and_is_fine_before_any(self):
        watch = self.watch()
        watch.stop()
        watch.proc = mock.Mock()
        watch.stop()
        watch.proc.kill.assert_called_once_with()


class Restart(unittest.TestCase):
    def restart(self, found, hw="rate: 48000 (48000/1)\n", fail=False):
        ran, self.started = [], []

        def run(*cmd):
            ran.append(list(cmd))
            self.existed, self.path = os.path.exists(cmd[-1]), cmd[-1]
            if fail:
                raise RuntimeError
            return ""

        def popen(cmd, **kw):
            self.started.append(mock.Mock(cmd=cmd))
            return self.started[-1]
        revive.restart(found, run, popen, lambda: hw)
        return ran

    def test_one_pair_of_silent_streams_in_one_group_at_another_rate(self):
        ran = self.restart({"sink": SCARLETT, "source": SOURCE})
        (play,), (cap,) = ran, self.started
        props = "{ node.dont-reconnect = true node.force-rate = 44100 node.group = booth.revive }"
        self.assertEqual(play[:5], ["pw-play", "--target", SCARLETT, "-P", props])
        self.assertEqual(cap.cmd[:3], ["pw-record", "--target", SOURCE])
        self.assertEqual(cap.cmd[cap.cmd.index("-P") + 1], props)  # both directions: they share the device's clock
        self.assertTrue(self.existed)  # the silence was there to play
        self.assertFalse(os.path.exists(play[-1]))  # and is gone
        cap.kill.assert_called_once_with()
        cap.wait.assert_called_once_with()

    def test_a_second_restart_goes_on_to_88200_never_back_to_the_graphs_own_48000(self):
        ran = self.restart({"sink": SCARLETT, "source": SOURCE}, "rate: 44100 (44100/1)\n")
        self.assertIn("node.force-rate = 88200", ran[0][4])  # 48000 is ignored while streams run; 44100 changes nothing
        self.assertEqual([revive.other_rate(f"rate: {r} ({r}/1)\n") for r in (48000, 88200, 96000)], [44100] * 3)
        self.assertEqual(revive.other_rate("closed\n"), 44100)

    def test_without_a_source_only_the_playback_stream(self):
        ran = self.restart({"sink": SCARLETT, "source": None})
        self.assertEqual((len(ran), self.started), (1, []))

    def test_the_recorder_is_ended_and_the_file_removed_even_when_playing_fails(self):
        with self.assertRaises(RuntimeError):
            self.restart({"sink": SCARLETT, "source": SOURCE}, fail=True)
        self.started[0].kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.path))
