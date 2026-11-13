"""One mute (#21): Booth Mic's node mute, the one GNOME's mic-mute toggles, read from pw-dump's monitor, put back on a
recreated Booth Mic, and GNOME's mic-mute bound to Ctrl+Alt+M only when it has no binding."""
import io
import json
import subprocess
import unittest
from unittest import mock

from booth import mute


def booth_mic(i=70, serial=9001, muted=False, props=True):
    params = {"Props": [{"volume": 1.0, "mute": muted, "channelVolumes": [1.0], "softMute": False}]} if props else {}
    return {"id": i, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": "booth_mic", "object.serial": serial, "media.class": "Audio/Source"},
                     "params": params}}


def other(i, name):
    return {"id": i, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name}, "params": {"Props": [{"mute": True}]}}}


class Binding(unittest.TestCase):
    """install.sh sets GNOME's mic-mute to Ctrl+Alt+M only when it has no binding: never the user's own."""

    def test_an_empty_binding_gets_ctrl_alt_m(self):
        for empty in ("['']\n", "@as []\n", "[]", "['', '']"):  # [''] is GNOME 46's default: no shortcut
            self.assertEqual(mute.binding(empty), "['<Control><Alt>m']", empty)

    def test_a_users_binding_is_never_overwritten(self):
        for users in ("['<Super>m']\n", "['XF86AudioMicMute']", "['', '<Shift><Super>space']"):
            self.assertIsNone(mute.binding(users), users)

    def test_already_ours_is_left_alone(self):
        self.assertIsNone(mute.binding("['<Control><Alt>m']\n"))

    def test_an_unreadable_binding_is_never_written(self):  # no GNOME, no such key, or gsettings failed
        for unreadable in (None, "", "No such key “mic-mute”\n", "'<Super>m'", "[[['", "@as"):
            self.assertIsNone(mute.binding(unreadable), unreadable)

    def bind(self, out, rc=0, set_rc=0):
        ran = []

        def run(cmd, **_):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, rc if cmd[1] == "get" else set_rc, out, "")
        return mute.bind(run), ran

    def test_bind_sets_it_with_gsettings_only_when_empty(self):
        said, ran = self.bind("['']\n")
        self.assertEqual(ran, [["gsettings", "get", mute.SCHEMA, "mic-mute"],
                               ["gsettings", "set", mute.SCHEMA, "mic-mute", "['<Control><Alt>m']"]])
        self.assertIsNone(said)
        said, ran = self.bind("['<Super>m']\n")
        self.assertEqual(ran, [["gsettings", "get", mute.SCHEMA, "mic-mute"]])
        self.assertIsNone(said)

    def test_bind_never_writes_when_gsettings_cant_read_it_and_says_how(self):
        said, ran = self.bind("['']\n", rc=1)  # a failed get's output is never trusted
        self.assertEqual(len(ran), 1)
        self.assertIn("Ctrl+Alt+M", said)

    def test_a_failed_set_is_said(self):
        said, _ = self.bind("['']\n", set_rc=1)
        self.assertIn("Ctrl+Alt+M", said)

    def test_the_schema_is_gnome_media_keys(self):
        self.assertEqual(mute.SCHEMA, "org.gnome.settings-daemon.plugins.media-keys")


class Find(unittest.TestCase):
    def test_booth_mics_node_its_serial_and_mute(self):
        objects = [other(5, "alsa_input.usb-Focusrite"), booth_mic(70, 9001, True), other(71, "booth.mic.input")]
        self.assertEqual(mute.find(objects), (70, 9001, True))
        self.assertEqual(mute.find([booth_mic(70, 9001, False)]), (70, 9001, False))

    def test_by_name_only_never_another_muted_node(self):
        self.assertIsNone(mute.find([other(5, "booth.mic.input"), other(6, "booth.test.mic")]))

    def test_no_props_reads_as_unknown_never_unmuted(self):  # a node not yet enumerated, or a removal
        self.assertEqual(mute.find([booth_mic(props=False)]), (70, 9001, None))
        self.assertIsNone(mute.find([{"id": 70, "info": None}]))
        self.assertIsNone(mute.find({"not": "a list"}))


class Seen(unittest.TestCase):
    """Booth's record of the mute follows Booth Mic's node; a node Booth hasn't followed yet is never unmuted by it."""

    def test_a_followed_node_is_the_record_whoever_changed_it(self):  # GNOME, another app or Booth
        self.assertEqual(mute.seen((70, 9001, True), 9001, False), (9001, True, None))
        self.assertEqual(mute.seen((70, 9001, False), 9001, True), (9001, False, None))

    def test_a_recreated_booth_mic_gets_the_mute_back_then_is_followed(self):
        self.assertEqual(mute.seen((80, 9050, False), 9001, True), (9001, True, True))  # put back, not followed yet
        self.assertEqual(mute.seen((80, 9050, False), 9001, True), (9001, True, True))  # the write failed: again
        self.assertEqual(mute.seen((80, 9050, True), 9001, True), (9050, True, None))  # it took it

    def test_at_start_a_muted_record_or_node_stays_muted(self):
        self.assertEqual(mute.seen((70, 9001, False), None, True), (None, True, True))
        self.assertEqual(mute.seen((70, 9001, True), None, False), (9001, True, None))  # muted while Booth was closed
        self.assertEqual(mute.seen((70, 9001, False), None, False), (9001, False, None))

    def test_an_unknown_mute_changes_nothing(self):
        self.assertEqual(mute.seen((70, 9001, None), 9001, True), (9001, True, None))
        self.assertEqual(mute.seen((80, 9050, None), 9001, True), (9001, True, None))

    def test_booth_mic_gone_or_a_new_monitor_forgets_the_followed_node(self):  # watch's found(None)
        self.assertEqual(mute.seen(None, 9001, True), (None, True, None))
        self.assertEqual(mute.seen(None, 9001, False), (None, False, None))
        # PipeWire restarted: the recreated booth_mic reuses serial 9001, unmuted; never taken as the user unmuting
        self.assertEqual(mute.seen((70, 9001, False), None, True), (None, True, True))


class Write(unittest.TestCase):
    def test_wpctl_on_booth_mics_node_found_by_name_now(self):
        ran = []
        dump = json.dumps([other(5, "alsa_input.usb-Focusrite"), booth_mic(77)])
        self.assertTrue(mute.write(True, run=lambda cmd: ran.append(cmd) or True, dump=lambda: dump))
        self.assertTrue(mute.write(False, run=lambda cmd: ran.append(cmd) or True, dump=lambda: dump))
        self.assertEqual(ran, [["wpctl", "set-mute", "77", "1"], ["wpctl", "set-mute", "77", "0"]])

    def test_no_booth_mic_writes_nothing(self):  # never another node, never a cached id
        ran = []
        for dump in ("", "[]", json.dumps([other(5, "booth.mic.input")])):
            self.assertFalse(mute.write(True, run=lambda cmd: ran.append(cmd) or True, dump=lambda: dump))
        self.assertEqual(ran, [])


MONITOR = """[
  {
    "id": 70,
    "info": {
      "props": {
        "node.name": "booth_mic",
        "object.serial": 9001
      },
      "params": {
        "Props": [
          {
            "mute": false
          }
        ]
      }
    }
  }
]
[
  {
    "id": 117,
    "info": null
  }
]
[
  {
    "id": 70,
    "info": {
      "props": {
        "node.name": "booth_mic",
        "object.serial": 9001
      },
      "params": {
        "Props": [
          {
            "mute": true
          }
        ]
      }
    }
  }
]
"""


class Watch(unittest.TestCase):
    """pw-dump --monitor prints one JSON array per change, closed by a "]" alone on its line (PipeWire 1.0.5)."""

    def test_batches_split_the_monitors_output(self):
        got = list(mute.batches(MONITOR.splitlines(keepends=True)))
        self.assertEqual([[o["id"] for o in b] for b in got], [[70], [117], [70]])

    def test_a_broken_batch_is_skipped(self):
        got = list(mute.batches(["[\n", "  {oops\n", "]\n", "[\n", "  {}\n", "]\n"]))
        self.assertEqual(got, [[{}]])

    def test_found_on_each_change_of_booth_mic_and_the_monitor_restarted_when_it_ends(self):
        seen, started, slept = [], [], []

        class Popen:
            def __init__(self, cmd, **kw):
                started.append(cmd)
                self.stdout = iter(MONITOR.splitlines(keepends=True))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def sleep(s):
            slept.append(s)
            if len(slept) == 2:
                raise KeyboardInterrupt  # out of the loop
        with self.assertRaises(KeyboardInterrupt):
            mute.watch(seen.append, popen=Popen, sleep=sleep)
        self.assertEqual(seen, [None, (70, 9001, False), (70, 9001, True)] * 2)  # each run forgets the node first
        self.assertEqual(started, [["pw-dump", "--monitor", "--no-colors"]] * 2)
        self.assertEqual(slept, [mute.RETRY_S] * 2)

    def test_no_pw_dump_retries_later_never_in_a_tight_loop(self):
        slept = []

        def popen(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        def sleep(s):
            slept.append(s)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            mute.watch(lambda _: None, popen=popen, sleep=sleep)
        self.assertEqual(slept, [mute.RETRY_S])
        self.assertGreaterEqual(mute.RETRY_S, 2)

    def monitors(self, *outs):
        """subprocess.Popen stand-ins: pw-dump's monitor printing each of `outs` (bytes) in turn, decoded as Popen's own
        keyword arguments say; each records its terminate()s."""
        made = []

        class Popen:
            def __init__(self, cmd, text=False, encoding=None, errors=None, **kw):
                self.stdout = io.TextIOWrapper(io.BytesIO(outs[len(made)]), encoding=encoding, errors=errors)
                self.terminate = mock.Mock()
                made.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Popen, made

    def run_watch(self, popen, found, runs=1):
        slept = []

        def sleep(s):
            slept.append(s)
            if len(slept) == runs:
                raise KeyboardInterrupt  # out of the loop
        with self.assertRaises(KeyboardInterrupt):
            mute.watch(found, popen=popen, sleep=sleep)
        return slept

    def test_a_non_utf8_byte_never_ends_the_watch_and_later_changes_still_apply(self):
        seen = []
        popen, _ = self.monitors(b'[\n"\xff"\n]\n' + MONITOR.encode())
        self.run_watch(popen, seen.append)
        self.assertEqual(seen, [None, (70, 9001, False), (70, 9001, True)])  # the same monitor, read on past the byte

    def test_any_error_restarts_the_monitor_later_and_its_changes_apply(self):
        seen, real, calls = [], mute.find, []

        def find(batch):
            calls.append(batch)
            if len(calls) == 1:
                raise RuntimeError("a parser's own bug")
            return real(batch)
        popen, made = self.monitors(MONITOR.encode(), MONITOR.encode())
        with mock.patch.object(mute, "find", find):
            slept = self.run_watch(popen, seen.append, runs=2)
        self.assertEqual(slept, [mute.RETRY_S] * 2)
        self.assertEqual(seen, [None, None, (70, 9001, False), (70, 9001, True)])  # from the restarted monitor
        made[0].terminate.assert_called_with()  # the broken one never outlives its loop

    def test_a_monitor_left_is_terminated_never_an_orphan(self):  # it would live on until the graph next changes
        popen, made = self.monitors(MONITOR.encode())
        self.run_watch(popen, lambda _: None)
        made[0].terminate.assert_called_with()

    def test_stop_terminates_the_running_monitor(self):  # Booth's quit (app.do_shutdown)
        popen, made, stopped = *self.monitors(MONITOR.encode()), []

        def found(node):
            if node is None:
                return
            mute.stop()
            stopped.append(made[0].terminate.called)
        self.run_watch(popen, found)
        self.assertEqual(stopped[:1], [True])

    def follow(self, *outs):
        """watch() over pw-dump monitors printing each of `outs` (text) in turn, with app.on_mute_seen's bookkeeping
        and a muted record → (record, the node mutes written)."""
        state = {"known": None, "record": True, "writes": []}
        popen, _ = self.monitors(*(o.encode() for o in outs))

        def found(node):  # booth.app.on_mute_seen
            state["known"], state["record"], w = mute.seen(node, state["known"], state["record"])
            if w is not None:
                state["writes"].append(w)
        self.run_watch(popen, found, runs=len(outs))
        return state["record"], state["writes"]

    def test_skeptic_pipewire_restart_reusing_booth_mics_serial_never_unmutes_the_user(self):
        # PipeWire's object.serial restarts with it: the recreated booth_mic may carry the old one, unmuted
        record, writes = self.follow(mon(booth_mic(106, 87, True)), mon(booth_mic(106, 87, False)))
        self.assertEqual((record, writes), (True, [True]))  # its mute put back, never recorded as live

    def test_booth_mics_removal_forgets_it_so_a_node_reusing_its_serial_is_muted(self):
        record, writes = self.follow(mon(booth_mic(106, 87, True)) + mon({"id": 117, "info": None})
                                     + mon({"id": 106, "info": None}) + mon(booth_mic(106, 87, False)))
        self.assertEqual((record, writes), (True, [True]))

    def test_another_nodes_removal_keeps_booth_mic_followed(self):  # the user's own unmute still counts
        record, writes = self.follow(mon(booth_mic(106, 87, True)) + mon({"id": 117, "info": None})
                                     + mon(booth_mic(106, 87, False)))
        self.assertEqual((record, writes), (False, []))


def mon(*objects):
    """One batch as pw-dump --monitor prints it."""
    return json.dumps(list(objects), indent=2) + "\n"


class Enforce(unittest.TestCase):
    """booth-voice.service's ExecStartPost (booth.mute --enforce): a muted record mutes the new booth_mic's node with no
    Booth running, so a kept voice-run.conf with live gains is never heard; it never unmutes."""

    def enforce(self, settings, dumps):
        ran, slept, dumps = [], [], iter(dumps)
        got = mute.enforce(load=lambda: settings, dump=lambda: next(dumps, ""),
                           run=lambda cmd: ran.append(cmd) or True, sleep=slept.append)
        return got, ran, slept

    def test_a_muted_record_mutes_booth_mic_once_it_is_there(self):
        got, ran, slept = self.enforce({"voice": {"muted": True}}, ["", "[]", json.dumps([booth_mic(106)])])
        self.assertTrue(got)
        self.assertEqual(ran, [["wpctl", "set-mute", "106", "1"]])
        self.assertEqual(len(slept), 2)

    def test_an_unmuted_record_never_touches_the_node(self):
        got, ran, _ = self.enforce({"voice": {}}, [json.dumps([booth_mic(106, muted=True)])])
        self.assertTrue(got)
        self.assertEqual(ran, [])

    def test_it_gives_up_within_five_seconds(self):
        got, ran, slept = self.enforce({"voice": {"muted": True}}, [])
        self.assertFalse(got)
        self.assertEqual(ran, [])
        self.assertLessEqual(sum(slept), 5)
        self.assertGreater(len(slept), 1)

    def test_main_enforce(self):
        with mock.patch.object(mute, "enforce", lambda: False), mock.patch.object(mute.sys, "argv", ["x", "--enforce"]),\
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(mute.main(), 1)
        self.assertIn("muted", err.getvalue())
        with mock.patch.object(mute, "enforce", lambda: True), mock.patch.object(mute.sys, "argv", ["x", "--enforce"]):
            self.assertEqual(mute.main(), 0)


if __name__ == "__main__":
    unittest.main()
