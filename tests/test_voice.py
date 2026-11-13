"""The voice chain builder and its service side: Booth Mic is a mono source fed only from the mic's channel, through a
separate source selector, with every stage and the master switch a live dry/wet bypass."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from booth import scarlett, voice
from tests.fixture import read
from tests.test_headset import DUMP

CTRLS = scarlett.parse_contents(read("scarlett/amixer-contents.txt"))
MIC = scarlett.mic(CTRLS, DUMP, "XLR")
ON = {"voice": {}}  # the defaults: processing on, rumble filter on
UNITS = ["booth-voice.service", "booth-mic-select.service"]
RESTART = ["systemctl", "--user", "restart"] + UNITS  # a new chain: both, the selector re-links to the new input
START = ["systemctl", "--user", "start"] + UNITS
RESTART_SELECT = ["systemctl", "--user", "restart", "booth-mic-select.service"]


def node(i, name):
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": name}}}


CHAIN_DUMP = json.dumps([node(71, "booth.mic.input")])
SCARLETT_BACK = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"])])  # the selector unloaded itself
SELECTING = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"]), node(80, "booth.mic.select")])


def config(settings=ON):
    """Booth Mic's chain: booth-voice.service's config, whatever the Scarlett does."""
    return json.loads(voice.build(settings))


def selector(mic=MIC):
    """The source selector: booth-mic-select.service's config."""
    return json.loads(voice.build_selector(mic))


def module(cfg, name):
    found = [m["args"] for m in cfg["context.modules"] if m["name"] == name]
    assert len(found) == 1, (name, found)
    return found[0]


def flow(cfg):
    """What reaches Booth Mic, worked out from the graph: {signal path: gain}. The raw input is "in"; each stage wraps
    what it gets, e.g. "rumble(in)". Mixers add their inputs times their gains; unknown labels fail the test."""
    graph = module(cfg, "libpipewire-module-filter-chain")["filter.graph"]
    nodes = {n["name"]: n for n in graph["nodes"]}
    feeds = {}
    for link in graph["links"]:
        assert link["input"] not in feeds, f"two links into {link['input']}"
        feeds[link["input"]] = link["output"]
    (graph_in,), (graph_out,) = graph["inputs"], graph["outputs"]

    def signal(port):  # an input port → what arrives there
        if port == graph_in:
            return {"in": 1.0}
        src = feeds.get(port)
        return output(src.split(":")[0]) if src else {}

    def output(name):
        node, control = nodes[name], nodes[name].get("control", {})
        if node["label"] == "copy":
            return signal(f"{name}:In")
        if node["label"] == "mixer":
            out = {}
            for i in range(1, 9):
                for path, g in signal(f"{name}:In {i}").items():
                    out[path] = out.get(path, 0.0) + g * control.get(f"Gain {i}", 1.0)
            return {p: g for p, g in out.items() if g}
        if node["label"].startswith("bq_"):
            return {f"{name}({p})": g for p, g in signal(f"{name}:In").items()}
        raise AssertionError(f"unknown label {node['label']}")

    return output(graph_out.split(":")[0])


class BoothMic(unittest.TestCase):
    def test_booth_mic_is_a_mono_audio_source(self):
        chain = module(config(), "libpipewire-module-filter-chain")
        mic = chain["playback.props"]
        self.assertEqual((mic["node.name"], mic["node.description"], mic["media.class"]),
                         ("booth_mic", "Booth Mic", "Audio/Source"))
        self.assertEqual((chain["audio.channels"], chain["audio.position"]), (1, ["MONO"]))

    def test_capture_takes_only_the_mic_channel_by_position_never_a_mixdown(self):
        self.assertEqual(MIC["position"], "FR")  # XLR = capture channel 2 on the fixture
        capture = module(selector(), "libpipewire-module-loopback")["capture.props"]
        self.assertEqual(capture["target.object"], MIC["node"])
        self.assertEqual((capture["audio.channels"], capture["audio.position"]), (1, ["FR"]))
        self.assertIs(capture["stream.dont-remix"], True)  # else PipeWire mixes all four channels into one

    def test_the_instrument_choice_captures_its_own_channel(self):
        mic = scarlett.mic(CTRLS, DUMP, "Instrument")
        capture = module(selector(mic), "libpipewire-module-loopback")["capture.props"]
        self.assertEqual(capture["audio.position"], [mic["position"]])
        self.assertNotEqual(mic["position"], MIC["position"])

    def test_capture_is_passive(self):
        self.assertIs(module(selector(), "libpipewire-module-loopback")["capture.props"]["node.passive"], True)
        self.assertIs(module(selector(), "libpipewire-module-loopback")["playback.props"]["node.passive"], True)
        self.assertIs(module(config(), "libpipewire-module-filter-chain")["capture.props"]["node.passive"], True)

    def test_a_separate_selector_feeds_the_chains_hidden_input(self):
        sel, chain = module(selector(), "libpipewire-module-loopback"), module(config(), "libpipewire-module-filter-chain")
        inlet = chain["capture.props"]
        self.assertEqual(sel["playback.props"]["target.object"], inlet["node.name"])
        self.assertEqual(sel["capture.props"]["node.name"], voice.SELECTOR)
        # internal classes: no "Booth Mic input" in the sound settings' outputs, no stream in the apps list
        self.assertEqual(inlet["media.class"], "Audio/Sink/Internal")
        self.assertEqual(sel["playback.props"]["media.class"], "Stream/Output/Audio/Internal")
        self.assertEqual(inlet["node.name"], voice.INPUT)

    def test_the_chain_and_the_selector_run_in_separate_processes(self):
        # a mic change restarts only the selector's process: Booth Mic, and a call's link to it, never go away
        self.assertEqual([m["name"] for m in config()["context.modules"]][-1:], ["libpipewire-module-filter-chain"])
        self.assertNotIn("libpipewire-module-loopback", [m["name"] for m in config()["context.modules"]])
        self.assertNotIn("libpipewire-module-filter-chain", [m["name"] for m in selector()["context.modules"]])

    def test_the_selector_is_pinned_to_the_scarlett_never_moved_to_another_mic(self):
        # WirePlumber 0.4 would re-link it to the best other source (the BRIO's mic) when the Scarlett goes: with
        # dont-reconnect it destroys the selector instead, and Booth Mic carries silence
        sel = module(selector(), "libpipewire-module-loopback")
        self.assertEqual(sel["capture.props"]["target.object"], MIC["node"])
        self.assertIs(sel["capture.props"]["node.dont-reconnect"], True)
        # and its output never falls to a speaker when Booth Mic's input isn't there (a chain restart, login)
        self.assertIs(sel["playback.props"]["node.dont-reconnect"], True)

    def test_no_mic_channel_no_selector(self):
        self.assertIsNone(voice.build_selector(None))  # not routed: never a guessed channel
        self.assertIsNone(voice.build_selector(dict(MIC, node=None, position=None, channels=None)))  # not in PipeWire

    def test_the_config_is_quoted_whatever_the_node_name(self):
        mic = dict(MIC, node='odd "name" } ] = x')
        self.assertEqual(module(selector(mic), "libpipewire-module-loopback")["capture.props"]["target.object"],
                         mic["node"])


class Bypass(unittest.TestCase):
    def test_on_booth_mic_gets_the_filtered_mic_only(self):
        self.assertEqual(flow(config()), {"rumble(in)": 1.0})

    def test_the_rumble_filter_off_passes_the_mic_dry(self):
        self.assertEqual(flow(config({"voice": {"rumble": False}})), {"in": 1.0})

    def test_the_master_switch_off_passes_the_raw_mic_whatever_the_stages(self):
        self.assertEqual(flow(config({"voice": {"enabled": False}})), {"in": 1.0})
        self.assertEqual(flow(config({"voice": {"enabled": False, "rumble": False}})), {"in": 1.0})

    def test_the_rumble_filter_is_a_builtin_high_pass_near_80_hz(self):
        nodes = module(config(), "libpipewire-module-filter-chain")["filter.graph"]["nodes"]
        rumble = next(n for n in nodes if n["name"] == "rumble")
        self.assertEqual((rumble["type"], rumble["label"]), ("builtin", "bq_highpass"))
        self.assertEqual(rumble["control"]["Freq"], 80.0)
        types = {n["type"] for n in nodes}
        self.assertEqual(types, {"builtin"})  # no LADSPA plugin in this slice

    def test_switches_change_only_control_values_so_they_never_need_a_restart(self):
        configs = [config({"voice": {"enabled": e, "rumble": r}}) for e in (True, False) for r in (True, False)]
        self.assertEqual({json.dumps(voice.shape(c), sort_keys=True) for c in configs}.__len__(), 1)
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in configs}), 4)

    def test_the_live_change_sets_every_control_on_the_chain_input_node(self):
        cfg = config({"voice": {"rumble": False}})
        cmd = voice.set_params_cmd(CHAIN_DUMP, cfg)
        self.assertEqual(cmd[:4], ["pw-cli", "set-param", "71", "Props"])
        params = json.loads(cmd[4])["params"]
        pairs = dict(zip(params[::2], params[1::2]))
        self.assertEqual((pairs["rumble_mix:Gain 1"], pairs["rumble_mix:Gain 2"]), (0.0, 1.0))
        self.assertEqual((pairs["master:Gain 1"], pairs["master:Gain 2"]), (1.0, 0.0))
        self.assertTrue(all(isinstance(v, float) for v in params[1::2]))  # pw-cli reads 1 as an Int

    def test_no_chain_running_no_live_change(self):
        self.assertIsNone(voice.set_params_cmd("[]", config()))
        self.assertIsNone(voice.set_params_cmd("not json", config()))


class Apply(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "booth" / "voice.conf"
        self.select = self.file.with_name("voice-select.conf")
        self.ran, self.fails = [], ()

    def apply(self, settings=ON, mic=MIC):
        return voice.apply(settings, mic, run=lambda cmd: self.ran.append(cmd) or cmd[0] not in self.fails,
                           dump=lambda: CHAIN_DUMP, file=self.file)

    def test_the_first_config_is_written_and_the_service_restarted(self):
        self.apply()
        self.assertEqual(json.loads(self.file.read_text()), config())
        self.assertEqual(json.loads(self.select.read_text()), selector())
        self.assertEqual(self.ran, [RESTART])

    def test_the_same_config_again_touches_nothing(self):
        self.apply()
        self.ran.clear()
        self.apply()
        self.assertEqual(self.ran, [])

    def test_a_switch_is_changed_live_and_saved_for_the_next_start_never_restarted(self):
        self.apply()
        self.ran.clear()
        self.apply({"voice": {"rumble": False}})
        self.assertEqual([c[:3] for c in self.ran], [["pw-cli", "set-param", "71"]])
        self.assertEqual(json.loads(self.file.read_text()), config({"voice": {"rumble": False}}))

    def test_another_input_restarts_only_the_selector_never_booth_mic(self):
        self.apply()
        chain, self.ran = self.file.read_text(), []
        instrument = scarlett.mic(CTRLS, DUMP, "Instrument")
        self.apply(mic=instrument)
        self.assertEqual(self.ran, [RESTART_SELECT])
        self.assertEqual(self.file.read_text(), chain)  # booth-voice.service's config: untouched
        self.assertEqual(json.loads(self.select.read_text()), selector(instrument))

    def test_no_mic_keeps_the_last_selector_and_runs_nothing(self):
        self.apply()
        before, self.ran = self.select.read_text(), []
        self.apply(mic=None)
        self.apply(mic=dict(MIC, node=None, position=None))
        self.assertEqual((self.select.read_text(), self.ran), (before, []))

    def test_a_switch_is_applied_with_the_scarlett_unplugged(self):
        self.apply()
        self.ran.clear()
        self.apply({"voice": {"enabled": False}}, mic=None)
        self.assertEqual([c[:2] for c in self.ran], [["pw-cli", "set-param"]])

    def test_a_live_change_that_fails_is_not_recorded_and_the_next_apply_retries(self):
        self.apply()
        before, self.ran, self.fails = self.file.read_text(), [], ("pw-cli",)
        self.assertTrue(self.apply({"voice": {"rumble": False}}).startswith("not applied"))
        self.assertEqual(self.file.read_text(), before)  # still the old state: the next apply sees the change
        self.ran, self.fails = [], ()
        self.assertEqual(self.apply({"voice": {"rumble": False}}), "live")
        self.assertEqual([c[:2] for c in self.ran], [["pw-cli", "set-param"]])
        self.assertEqual(json.loads(self.file.read_text()), config({"voice": {"rumble": False}}))

    def test_a_live_change_that_cant_be_saved_is_retried(self):
        self.apply()
        self.ran.clear()
        with mock.patch.object(voice.store, "save", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.apply({"voice": {"rumble": False}}), "not saved: No space left on device")
        self.ran.clear()
        self.apply({"voice": {"rumble": False}})
        self.assertEqual([c[:2] for c in self.ran], [["pw-cli", "set-param"]])

    def test_a_corrupt_config_is_replaced_and_restarted(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{ half")
        self.apply()
        self.assertEqual(self.ran, [RESTART])

    def test_a_config_that_cant_be_written_restarts_nothing(self):
        self.file.parent.mkdir(parents=True)
        self.file.mkdir()  # a folder where the file goes: the write fails
        self.assertTrue(self.apply().startswith("not saved"))
        self.assertEqual(self.ran, [])

    def test_it_only_ever_runs_systemctl_on_booth_mics_two_units(self):
        self.apply()
        self.apply({"voice": {"enabled": False}})
        self.apply(mic=scarlett.mic(CTRLS, DUMP, "Instrument"))
        for cmd in self.ran:
            if cmd[0] == "systemctl":
                self.assertTrue(set(cmd[3:]) <= set(UNITS), cmd)
            else:
                self.assertEqual(cmd[0], "pw-cli")


class Sync(unittest.TestCase):
    """What Booth (every primary instance) and install.sh run: the Scarlett now → config → the service started."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.ran = []

    def sync(self, state):
        return voice.sync(ON, read=lambda choice: state, run=lambda cmd: self.ran.append(cmd) or True,
                          dump=lambda: CHAIN_DUMP, file=self.file)

    def test_writes_then_starts(self):
        said = self.sync({"ctrls": CTRLS, "mic": MIC})
        self.assertEqual(self.ran, [RESTART, START])
        self.assertEqual(said, voice.describe({"ctrls": CTRLS, "mic": MIC}))

    def test_unplugged_with_an_old_config_still_starts_it(self):
        self.sync({"ctrls": CTRLS, "mic": MIC})
        self.ran.clear()
        self.sync(None)
        self.assertEqual(self.ran, [START])

    def test_unplugged_the_first_time_booth_mic_appears_waiting_for_the_scarlett(self):
        self.assertEqual(self.sync(None), voice.WAITING)
        self.assertEqual(self.ran, [RESTART, START])
        self.assertEqual(json.loads(self.file.read_text()), config())
        self.assertFalse(self.file.with_name("voice-select.conf").exists())  # never a guessed channel

    def test_the_page_says_why_there_is_no_capture(self):
        self.assertEqual(voice.describe(None), voice.WAITING)
        self.assertEqual(voice.describe({"ctrls": CTRLS, "mic": None}), voice.NOT_ROUTED)
        self.assertEqual(voice.describe({"ctrls": CTRLS, "mic": dict(MIC, node=None, position=None)}), voice.WAITING)
        self.assertIn("capture channel 2 (FR)", voice.describe({"ctrls": CTRLS, "mic": MIC}))
        self.assertEqual(voice.describe({"ctrls": CTRLS, "mic": MIC}, selecting=False), voice.WAITING)

    def test_the_config_lives_beside_the_settings(self):
        from booth import store
        self.assertEqual(voice.path(), store.path().parent / "voice.conf")


class Heal(unittest.TestCase):
    """The selector unloads itself when its Scarlett goes (or isn't there yet at login): Booth restarts it once the
    Scarlett is back in PipeWire."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.ran = []

    def heal(self, dump):
        return voice.heal(run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: dump, file=self.file)

    def saved(self):
        voice.apply(ON, MIC, run=lambda cmd: True, dump=lambda: "[]", file=self.file)

    def test_the_scarlett_back_and_no_selector_restarts_the_selector(self):
        self.saved()
        self.assertTrue(self.heal(SCARLETT_BACK))
        self.assertEqual(self.ran, [RESTART_SELECT])

    def test_a_running_selector_or_no_scarlett_restarts_nothing(self):
        self.saved()
        for dump in (SELECTING, CHAIN_DUMP, "[]", "not json"):
            self.assertFalse(self.heal(dump), dump)
        self.assertEqual(self.ran, [])

    def test_no_selector_config_restarts_nothing(self):
        self.assertFalse(self.heal(SCARLETT_BACK))
        self.assertEqual(self.ran, [])

    def test_selecting_reads_the_selector_node_by_name(self):
        self.assertEqual((voice.selecting(SELECTING), voice.selecting(SCARLETT_BACK), voice.selecting("")),
                         (True, False, False))


if __name__ == "__main__":
    unittest.main()
