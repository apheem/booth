"""One mute (#21): Booth Mic's node mute, the one GNOME's mic-mute toggles, read from pw-dump's monitor, put back on a
recreated Booth Mic, and GNOME's mic-mute bound to Ctrl+Alt+M only when it has no binding."""
import json
import subprocess
import unittest

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
        self.assertEqual(seen, [(70, 9001, False), (70, 9001, True)] * 2)
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


if __name__ == "__main__":
    unittest.main()
