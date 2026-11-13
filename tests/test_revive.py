"""The Scarlett's stuck output: found by what PipeWire sends against the device's own Level Meter, restarted by one
pair of silent streams at another rate. Fixtures and fakes only: no pw-record, pw-play or amixer runs here."""
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
PLAYING, STUCK, IDLE = "25,25,25,25,0,3,0,3,0,3,25,25", "0,0,0,0,0,1,0,1,0,1,0,0", "0,0,0,0,0,0,0,0,0,0,0,0"


def running(objects, state="running"):
    node(objects, SCARLETT)["info"]["state"] = state


def floats(*values):
    return array.array("f", values).tobytes()


class Nodes(unittest.TestCase):
    def test_the_scarletts_sink_and_source_by_name_its_state_and_its_volume(self):
        found = revive.nodes(dump(running))
        self.assertEqual((found["sink"], found["source"], found["running"]), (SCARLETT, SOURCE, True))
        self.assertAlmostEqual(found["gain"], 0.438993)

    def test_a_suspended_sink_is_not_running(self):
        self.assertIs(revive.nodes(DUMP)["running"], False)

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


class Meter(unittest.TestCase):
    def test_the_level_meters_points(self):
        self.assertEqual(revive.parse_meter(METER.format(PLAYING)), [25, 25, 25, 25, 0, 3, 0, 3, 0, 3, 25, 25])
        self.assertEqual(revive.parse_meter("amixer: Cannot find the given element from control sysdefault:6\n"), [])

    def test_the_peak_of_what_was_recorded(self):
        self.assertAlmostEqual(revive.peak(floats(0.0, -0.25, 0.1)), 0.25)
        self.assertEqual(revive.peak(b""), 0.0)
        self.assertAlmostEqual(revive.peak(floats(0.5) + b"\x01"), 0.5)  # a torn last sample is dropped

    def test_signal_sent_and_no_playback_point_moving_is_stuck(self):
        self.assertIs(revive.stuck(0.05, revive.parse_meter(METER.format(STUCK))), True)  # the mic's points don't count
        self.assertIs(revive.stuck(0.05, revive.parse_meter(METER.format(PLAYING))), False)
        self.assertIs(revive.stuck(0.05, [0] * 11 + [1]), False)  # one playback point is enough

    def test_quiet_or_unknown_is_never_stuck(self):
        idle = revive.parse_meter(METER.format(IDLE))
        self.assertIs(revive.stuck(revive.SENT_MIN / 2, idle), False)  # too quiet for the meter to show: unknown
        self.assertIs(revive.stuck(revive.SENT_MIN, idle), True)
        self.assertIs(revive.stuck(0.5, []), False)  # the meter couldn't be read
        self.assertIs(revive.stuck(0.5, [0] * 8), False)  # another device's layout


class Look(unittest.TestCase):
    def look(self, raw, meters, card="Gen"):
        asked, texts = [], iter(meters)

        def run(*cmd):
            asked.append(list(cmd))
            return next(texts)
        listen = mock.Mock()
        listen.return_value.communicate.return_value = (raw, None)
        with mock.patch.object(revive.time, "sleep"):
            out = revive.look({"sink": SCARLETT, "gain": 0.5}, run, lambda: card, listen)
        return out, asked, listen

    def test_the_peak_sent_times_the_volume_and_each_points_highest_of_three_reads(self):
        (sent, meters), asked, listen = self.look(floats(0.2, -0.4), [METER.format(IDLE), METER.format(PLAYING),
                                                                      METER.format(STUCK)])
        self.assertAlmostEqual(sent, 0.2)
        self.assertEqual(meters, [25, 25, 25, 25, 0, 3, 0, 3, 0, 3, 25, 25])
        self.assertEqual(asked, [["amixer", "-c", "Gen", "cget", "iface=PCM,name=Level Meter"]] * 3)
        listen.assert_called_once_with(SCARLETT)

    def test_a_read_that_failed_gives_no_meter(self):
        self.assertEqual(self.look(floats(0.4), [METER.format(STUCK), "", METER.format(STUCK)])[0][1], [])

    def test_the_card_gone_listens_to_nothing(self):
        out, asked, listen = self.look(floats(0.4), [], card=None)
        self.assertEqual((out, asked, listen.called), ((0.0, []), [], False))

    def test_it_listens_to_the_sinks_monitor_by_name_briefly_and_never_another_one(self):
        with mock.patch.object(revive.subprocess, "Popen") as popen:
            revive.listen(SCARLETT)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:3], ["timeout", str(revive.LOOK_S), "pw-record"])
        self.assertEqual(cmd[cmd.index("--target") + 1], SCARLETT)
        props = cmd[cmd.index("-P") + 1]
        self.assertIn("stream.capture.sink = true", props)
        self.assertIn("node.dont-reconnect = true", props)
        # mono at 8 kHz: the whole look fits in the pipe, so a recorder is never left blocked on it
        self.assertLess(revive.LOOK_S * 8000 * 4, 65536)
        self.assertEqual((cmd[cmd.index("--channels") + 1], cmd[cmd.index("--rate") + 1]), ("1", "8000"))


class Watching(unittest.TestCase):
    ALIVE, DEAD, QUIET = (0.05, [25] * 12), (0.05, [0] * 12), (0.0, [0] * 12)

    def watch(self, looks, text=None):
        self.looks, self.restarts, self.now, self.said = iter(looks), [], 1000.0, []
        return revive.Watch(run=lambda *cmd: dump(running) if text is None else text,
                            look=lambda found: next(self.looks), restart=self.restarts.append,
                            clock=lambda: self.now, sleep=lambda s: None, log=self.said.append)

    def test_a_sink_that_is_not_running_is_never_looked_at(self):
        self.assertIsNone(self.watch([], DUMP).check())  # suspended: an exhausted `looks` would raise
        self.assertIsNone(self.watch([], "").check())

    def test_playing_and_heard_inside_the_device_does_nothing(self):
        watch = self.watch([self.ALIVE])
        self.assertEqual((watch.check(), self.restarts), (None, []))

    def test_silence_being_played_is_unknown_and_does_nothing(self):
        watch = self.watch([self.QUIET])
        self.assertEqual((watch.check(), self.restarts), (None, []))

    def test_stuck_in_two_looks_is_restarted_and_said_once_it_is_heard_again(self):
        watch = self.watch([self.DEAD, self.DEAD, self.ALIVE])
        self.assertEqual(watch.check(), revive.REVIVED)
        self.assertEqual([found["sink"] for found in self.restarts], [SCARLETT])
        self.assertIn("stuck", self.said[0])

    def test_stuck_in_one_look_only_is_not_restarted(self):  # the music started as the look ended
        watch = self.watch([self.DEAD, self.ALIVE])
        self.assertEqual((watch.check(), self.restarts), (None, []))

    def test_a_restart_that_did_not_help_says_to_replug_and_is_not_tried_again_until_it_is_heard(self):
        watch = self.watch([self.DEAD, self.DEAD, self.DEAD, self.DEAD, self.DEAD, self.ALIVE, self.DEAD, self.DEAD,
                            self.ALIVE])
        self.assertEqual(watch.check(), revive.GAVE_UP)
        self.now += 10 * revive.BACKOFF_S
        self.assertEqual((watch.check(), watch.check(), len(self.restarts)), (None, None, 1))  # still stuck: quiet
        self.assertIsNone(watch.check())  # heard again (a replug)
        self.assertEqual((watch.check(), len(self.restarts)), (revive.REVIVED, 2))  # a new stop gets its own restart

    def test_stuck_again_right_after_a_restart_waits(self):
        watch = self.watch([self.DEAD, self.DEAD, self.ALIVE, self.DEAD, self.DEAD, self.DEAD, self.ALIVE])
        self.assertEqual(watch.check(), revive.REVIVED)
        self.now += revive.BACKOFF_S - 1
        self.assertEqual((watch.check(), len(self.restarts)), (None, 1))
        self.now += 2
        self.assertEqual((watch.check(), len(self.restarts)), (revive.REVIVED, 2))


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

    def test_already_at_44100_it_goes_to_48000(self):  # the same rate again changes nothing in the device
        ran = self.restart({"sink": SCARLETT, "source": SOURCE}, "rate: 44100 (44100/1)\n")
        self.assertIn("node.force-rate = 48000", ran[0][4])
        self.assertEqual(revive.other_rate("closed\n"), 44100)

    def test_without_a_source_only_the_playback_stream(self):
        ran = self.restart({"sink": SCARLETT, "source": None})
        self.assertEqual((len(ran), self.started), (1, []))

    def test_the_recorder_is_ended_and_the_file_removed_even_when_playing_fails(self):
        with self.assertRaises(RuntimeError):
            self.restart({"sink": SCARLETT, "source": SOURCE}, fail=True)
        self.started[0].kill.assert_called_once_with()
        self.assertFalse(os.path.exists(self.path))

    def test_the_pro_x_is_never_restarted(self):
        self.assertNotIn(PRO_X, str(revive.nodes(dump(running))))
