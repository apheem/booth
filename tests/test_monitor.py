"""Processed monitoring: the loopback Booth Mic → the PRO X headset (booth-monitor.service's config), and sync(), which
writes it, starts and stops the unit and brings it back, never touching Booth Mic, its chain or its selector."""
import json
import tempfile
import unittest
from pathlib import Path

from booth import monitor, voice
from tests.test_headset import DUMP, PRO_X, SCARLETT

ON, OFF = {"voice": {"monitor": True}}, {"voice": {}}
RESTART = ["systemctl", "--user", "restart", "booth-monitor.service"]
STOP = ["systemctl", "--user", "stop", "booth-monitor.service"]


def node(i, name, serial):
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": name, "object.serial": serial}}}


def dump(*extra, prox=True):
    """The captured pw-dump (the PRO X, the Scarlett, the BRIO) plus `extra` nodes; prox=False: the dongle unplugged."""
    objects = [o for o in json.loads(DUMP) if prox or o["id"] != 33] + list(extra)
    return json.dumps(objects)


MIC = node(97, "booth_mic", 900)
MONITOR = (node(130, "booth.monitor", 950), node(131, "booth.monitor.out", 951))


def loopback(cfg):
    (args,) = [m["args"] for m in cfg["context.modules"] if m["name"] == "libpipewire-module-loopback"]
    return args


class Build(unittest.TestCase):
    def test_captures_booth_mic_by_name_and_never_falls_back_to_another_mic(self):
        capture = loopback(json.loads(monitor.build(PRO_X)))["capture.props"]
        self.assertEqual(capture["target.object"], "booth_mic")
        self.assertIs(capture["node.dont-reconnect"], True)
        self.assertEqual((capture["audio.channels"], capture["audio.position"]), (1, ["MONO"]))
        self.assertEqual(capture["media.class"], "Stream/Input/Audio")  # an app's stream: never a source a call picks
        self.assertNotIn("node.passive", capture)  # it is what keeps Booth Mic running while on

    def test_plays_only_to_the_pro_x_sink_by_name_never_the_default(self):
        playback = loopback(json.loads(monitor.build(PRO_X)))["playback.props"]
        self.assertEqual(playback["target.object"], PRO_X)
        self.assertIs(playback["node.dont-reconnect"], True)  # the dongle gone: destroyed, never moved to a speaker
        self.assertEqual(playback["media.class"], "Stream/Output/Audio")  # an app's stream: never a sink apps pick
        self.assertEqual((playback["audio.channels"], playback["audio.position"]), (1, ["MONO"]))  # both ears
        self.assertNotIn("stream.dont-remix", playback)  # mono upmixed to both sides by the stream, never left only

    def test_its_nodes_are_its_own_never_booth_mics(self):
        args = loopback(json.loads(monitor.build(PRO_X)))
        names = {args["capture.props"]["node.name"], args["playback.props"]["node.name"]}
        self.assertEqual(names, {"booth.monitor", "booth.monitor.out"})
        self.assertFalse(names & {voice.NODE, voice.INPUT, voice.SELECTOR, f"{voice.SELECTOR}.out"})

    def test_the_config_is_quoted_whatever_the_sink_name(self):
        odd = 'alsa_output.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00."x" }, { y = 1'
        self.assertEqual(loopback(json.loads(monitor.build(odd)))["playback.props"]["target.object"], odd)

    def test_the_label_states_a_measured_delay(self):
        # measured live (#18's PR): ticks through a copy of the chain + this loopback, against the same ticks direct,
        # one GStreamer clock: 40 ms at quantum 1024, 35.5 at 256 (RNNoise's frames; the limiter alone 5 ms)
        self.assertGreaterEqual(monitor.DELAY_MS, 35)
        self.assertIn(f"{monitor.DELAY_MS} ms", monitor.LABEL)
        self.assertIn("PRO X headset", monitor.LABEL)  # where it plays, and that the Scarlett stays in use
        self.assertIn("Scarlett", monitor.LABEL)

    def test_the_label_never_says_only_the_headset_hears_it(self):  # the PRO X sink's monitor carries it to any capture
        self.assertNotIn("only", monitor.LABEL.lower())
        self.assertIn("apps capturing the computer's sound", monitor.LABEL)
        self.assertIn("hear it too", monitor.LABEL)


class Sync(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "monitor.conf"
        self.ran, self.ok, self.now, self.t = [], True, dump(MIC), 0.0
        monitor._STARTED.clear()

    def run_(self, cmd):
        self.ran.append(cmd)
        return self.ok

    def sync(self, settings=ON):
        return monitor.sync(settings, self.run_, lambda: self.now, self.file, lambda: self.t)

    def test_on_writes_the_loopback_to_the_pro_x_and_starts_its_unit(self):
        self.assertEqual(self.sync(), "on")
        self.assertEqual(json.loads(self.file.read_text()), json.loads(monitor.build(PRO_X)))
        self.assertEqual(self.ran, [RESTART])

    def test_running_and_unchanged_touches_nothing(self):
        self.sync()
        self.now = dump(MIC, *MONITOR)
        self.assertEqual(self.sync(), "on")
        self.assertEqual(self.ran, [RESTART])

    def test_never_runs_anything_on_booth_mics_units(self):
        self.sync()
        self.now = dump(MIC)
        self.t = 1000.0
        self.sync()
        self.sync(OFF)
        self.assertTrue(self.ran)
        self.assertEqual([c for c in self.ran if any("booth-voice" in w or "booth-mic-select" in w for w in c)], [])
        self.assertEqual([c for c in self.ran if c[0] != "systemctl"], [])  # never pw-cli, wpctl or pw-metadata

    def test_never_plays_to_the_scarlett_or_the_default_when_the_pro_x_is_gone(self):
        self.now = dump(MIC, prox=False)  # output moved to the Scarlett, the dongle unplugged
        self.assertEqual(self.sync(), monitor.NO_PROX)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.ran, [])
        self.assertNotIn(SCARLETT, monitor.build(PRO_X))

    def test_pw_dump_failing_runs_nothing(self):
        self.now = ""
        self.assertEqual(self.sync(), monitor.NO_PROX)
        self.assertEqual(self.ran, [])

    def test_waits_for_booth_mic_before_starting(self):
        self.now = dump()  # Booth Mic not in PipeWire yet (login, or its chain restarting)
        self.assertEqual(self.sync(), monitor.NO_MIC)
        self.assertTrue(self.file.exists())  # ready for when it is
        self.assertEqual(self.ran, [])
        self.now = dump(MIC)
        self.assertEqual(self.sync(), "on")
        self.assertEqual(self.ran, [RESTART])

    def test_a_loopback_gone_with_booth_mic_restarted_is_started_again_at_once(self):
        self.sync()
        self.now = dump(node(98, "booth_mic", 901))  # a new Booth Mic: the old capture was destroyed with it
        self.assertEqual(self.sync(), "on")
        self.assertEqual(self.ran, [RESTART, RESTART])

    def test_a_replugged_dongle_same_sink_name_new_serial_is_started_again_at_once(self):
        self.sync()
        objects = json.loads(dump(MIC))
        for o in objects:
            if o["id"] == 33:  # the PRO X sink, back under its old name: the loopback went with the old one
                o["info"]["props"]["object.serial"] = 7333
        self.now, self.t = json.dumps(objects), 1.0
        self.assertEqual(self.sync(), "on")
        self.assertEqual(self.ran, [RESTART, RESTART])

    def test_half_a_loopback_is_not_up_and_is_restarted(self):
        self.sync()
        for i, half in enumerate(MONITOR):
            self.now, self.t = dump(MIC, half), (i + 1) * monitor.BACKOFF_S
            self.assertEqual(self.sync(), "on")
            self.assertEqual(self.ran, [RESTART] * (i + 2))

    def test_each_result_is_kept_for_the_pages_poll(self):
        self.now = dump()
        self.assertEqual((self.sync(), monitor.LAST), (monitor.NO_MIC, monitor.NO_MIC))
        self.assertEqual((self.sync(OFF), monitor.LAST), ("off", "off"))

    def test_a_loopback_that_wont_stay_up_is_restarted_at_most_once_a_minute(self):
        self.sync()
        self.t = monitor.BACKOFF_S - 1
        self.assertEqual(self.sync(), monitor.NOT_RUNNING)
        self.assertEqual(self.ran, [RESTART])
        self.t = monitor.BACKOFF_S
        self.sync()
        self.assertEqual(self.ran, [RESTART, RESTART])

    def test_a_new_pro_x_sink_name_restarts_it_on_the_new_sink(self):
        self.sync()
        other = PRO_X.replace("iec958-stereo", "analog-stereo")  # a profile switch
        self.now = dump(MIC, *MONITOR).replace(PRO_X, other)
        self.assertEqual(self.sync(), "on")
        self.assertEqual(self.ran, [RESTART, RESTART])
        self.assertEqual(loopback(json.loads(self.file.read_text()))["playback.props"]["target.object"], other)

    def test_a_failed_start_says_so(self):
        self.ok = False
        self.assertTrue(self.sync().startswith("not "))

    def test_a_config_that_cant_be_written_starts_nothing(self):
        self.file.mkdir()  # a folder where the file goes
        self.assertTrue(self.sync().startswith("not saved"))
        self.assertEqual(self.ran, [])

    def test_off_stops_the_unit_and_removes_its_config(self):
        self.sync()
        self.assertEqual(self.sync(OFF), "off")
        self.assertEqual(self.ran, [RESTART, STOP])
        self.assertFalse(self.file.exists())  # ExecCondition: nothing else can start it now

    def test_off_with_nothing_running_runs_nothing(self):
        self.assertEqual(self.sync(OFF), "off")
        self.assertEqual(self.ran, [])

    def test_a_stop_that_fails_keeps_the_config_so_the_next_tick_stops_it_again(self):
        self.sync()
        self.ok = False
        said = self.sync(OFF)
        self.assertTrue(said.startswith("not stopped"))
        self.assertEqual(monitor.problem(said), f"Set, but {said}")  # on the switch's row
        self.assertTrue(self.file.exists())
        self.ok = True
        self.assertEqual(self.sync(OFF), "off")
        self.assertEqual(self.ran, [RESTART, STOP, STOP])

    def sync_on(self, on):
        return monitor.sync(ON, self.run_, lambda: self.now, self.file, lambda: self.t, on=on)

    def test_the_pro_x_off_pauses_monitoring_with_the_switch_on_and_on_resumes_it(self):
        self.assertEqual(self.sync(), "on")
        self.now = dump(MIC, *MONITOR)  # its dongle's sink stays: the loopback would play into a dead headset
        self.assertEqual(self.sync_on(False), monitor.PAUSED)
        self.assertEqual((self.ran, self.file.exists()), ([RESTART, STOP], False))  # Booth Mic and its mic let go
        self.assertEqual(monitor.problem(monitor.PAUSED), "Paused: the PRO X is off")
        self.now = dump(MIC)
        self.assertEqual(self.sync_on(False), monitor.PAUSED)  # the next tick: nothing to stop
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(self.sync_on(True), "on")  # on again: played again
        self.now = dump(MIC, *MONITOR)
        self.assertEqual(self.sync_on(None), "on")  # not answered yet (Booth just started): never paused
        self.assertEqual(self.ran, [RESTART, STOP, RESTART])
        self.assertEqual(ON, {"voice": {"monitor": True}})  # the switch stays on

    def test_a_pause_whose_stop_fails_says_so_and_stops_it_next_time(self):
        self.sync()
        self.ok = False
        said = self.sync_on(False)
        self.assertTrue(said.startswith("not stopped"), said)
        self.assertTrue(self.file.exists())
        self.ok = True
        self.assertEqual(self.sync_on(False), monitor.PAUSED)
        self.assertFalse(self.file.exists())

    def test_it_is_off_unless_switched_on(self):
        for settings in ({}, {"voice": {}}, {"voice": {"monitor": False}}):
            self.assertEqual(self.sync(settings), "off")
        self.assertEqual(self.ran, [])
