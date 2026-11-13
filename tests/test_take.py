"""The test recording: Booth Mic by name for SECONDS, played back once on the default output, the file deleted after
every ending. No real pw-record: a fake Popen, the waiter thread inline, the main loop's idles run by hand."""
import os
import unittest
from unittest import mock

from booth import take
from tests.test_app import Inline


class Proc:
    def __init__(self, cmd, **_kw):
        self.cmd, self.returncode, self.terminated = cmd, None, False

    def wait(self):
        pass

    def terminate(self):
        self.terminated = True


class Commands(unittest.TestCase):
    def test_it_records_booth_mic_by_name_mono_and_never_another_mic(self):
        cmd = take.record_cmd("/run/x.wav")
        self.assertEqual(cmd[:3], ["timeout", str(take.SECONDS), "pw-record"])  # it ends itself, with Booth or not
        self.assertEqual(cmd[cmd.index("--target") + 1], "booth_mic")
        self.assertEqual(cmd[cmd.index("--channels") + 1], "1")
        self.assertIn("node.dont-reconnect = true", cmd[cmd.index("-P") + 1])
        self.assertEqual(cmd[-1], "/run/x.wav")

    def test_it_plays_on_the_default_output(self):
        self.assertEqual(take.play_cmd("/run/x.wav"), ["pw-play", "/run/x.wav"])

    def test_the_file_lives_in_the_runtime_folder_not_the_home_folder(self):
        self.assertEqual(os.path.dirname(take.path()), take.GLib.get_user_runtime_dir())


class Flow(unittest.TestCase):
    def setUp(self):
        self.idle, self.timers, self.said, self.procs = [], [], [], []
        for patch in (mock.patch.object(take.threading, "Thread", Inline),
                      mock.patch.object(take.GLib, "idle_add", lambda f, *a: self.idle.append((f, a))),
                      mock.patch.object(take.GLib, "timeout_add_seconds", lambda s, f, *a: self.timers.append((f, a)))):
            patch.start()
            self.addCleanup(patch.stop)
        self.take = take.Take(lambda text, busy: self.said.append((text, busy)), self.popen)
        self.addCleanup(lambda: os.path.exists(take.path()) and os.unlink(take.path()))

    def popen(self, cmd, **kw):
        self.procs.append(Proc(cmd))
        if cmd[0] == "timeout":
            open(take.path(), "w").close()  # as pw-record would
        return self.procs[-1]

    def exits(self, code):
        """The running process ended with `code`: its waiter's idle runs on the main loop."""
        self.procs[-1].returncode = code
        f, a = self.idle.pop(0)
        f(*a)

    def test_record_counts_down_then_plays_back_then_deletes_the_file(self):
        self.take.toggle()
        self.assertEqual(self.procs[0].cmd, take.record_cmd(take.path()))
        self.assertEqual(self.said, [(take.RECORDING.format(take.SECONDS), True)])
        (tick, args), = self.timers
        self.assertTrue(tick(*args))
        self.assertEqual(self.said[-1], (take.RECORDING.format(take.SECONDS - 1), True))
        self.exits(124)  # timeout's own code: the full take
        self.assertEqual(self.procs[1].cmd, take.play_cmd(take.path()))
        self.assertEqual(self.said[-1], (take.PLAYING, True))
        self.assertTrue(os.path.exists(take.path()))  # still there to play
        self.assertFalse(tick(*args))  # the countdown ends with the recording
        self.exits(0)
        self.assertEqual(self.said[-1], (None, False))
        self.assertFalse(os.path.exists(take.path()))

    def test_a_recorder_that_ends_early_is_said_and_nothing_is_played(self):
        self.take.toggle()
        self.exits(1)  # pw-record's own failure, or 127 when it isn't installed
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(self.said[-1], (take.FAILED, False))
        self.assertFalse(os.path.exists(take.path()))

    def test_stop_while_recording_ends_it_plays_nothing_and_deletes_the_file(self):
        self.take.toggle()
        self.take.toggle()  # the button again: Stop
        self.assertTrue(self.procs[0].terminated)
        self.assertEqual(self.said[-1], (None, False))
        self.assertFalse(os.path.exists(take.path()))
        self.exits(124)  # the stopped recorder's exit arrives late
        self.assertEqual(len(self.procs), 1)
        (tick, args), = self.timers
        self.assertFalse(tick(*args))
        self.assertEqual(self.said[-1], (None, False))  # and its countdown says nothing more

    def test_stop_while_playing_ends_the_player(self):
        self.take.toggle()
        self.exits(124)
        self.take.stop()
        self.assertTrue(self.procs[1].terminated)
        self.assertEqual(self.said[-1], (None, False))
        self.assertFalse(os.path.exists(take.path()))

    def test_stop_while_idle_does_nothing(self):
        self.take.stop()
        self.assertEqual(self.said, [])

    def test_a_tool_that_cannot_start_is_said(self):
        self.take.popen = mock.Mock(side_effect=FileNotFoundError)
        self.take.toggle()
        self.assertEqual((self.said, self.timers), ([(take.FAILED, False)], []))
        self.assertIsNone(self.take.proc)  # the next click records again
