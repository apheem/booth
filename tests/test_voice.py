"""The voice chain builder and its service side: Booth Mic is a mono source fed only from the mic's channel, through a
separate source selector, with every stage and the master switch a live dry/wet bypass."""
import contextlib
import copy
import hashlib
import io
import itertools
import json
import os
import struct
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from booth import plugins, scarlett, voice
from tests.fixture import read
from tests.test_headset import DUMP

PORTS = json.loads(read("ladspa/ports.json"))  # #14's capture of each verified plugin's descriptors: exact port names
FOUND = {name: f"/home/u/.local/share/booth/plugins/{name}" for name in plugins.MANIFEST}  # every plugin verified
STAGE_KEYS = ("noise", "leveling", "limiter")
REAL_VERIFIED = getattr(voice, "verified", None)


REAL_APPLY = voice.apply


def landing(settings, state, run=None, dump=None, *rest, **kw):
    """voice.apply on a fake PipeWire: a set-param its `run` reports done lands on booth.mic.input's Props, which `dump`
    then lists, as pw-dump does (the real chain's shape). Without it nothing a test sends reads back (see Readback)."""
    props, real_run, real_dump = {}, run or REAL_APPLY.__defaults__[0], dump or REAL_APPLY.__defaults__[1]

    def sent(cmd):
        done = real_run(cmd)
        if done and cmd[:2] == ["pw-cli", "set-param"]:
            props.update(itertools.batched(json.loads(cmd[4])["params"], 2))
        return done

    return REAL_APPLY(settings, state, sent, lambda: with_props(real_dump(), props), *rest, **kw)


def with_props(text, props):
    """`pw-dump` JSON `text` with booth.mic.input's Props holding `props` ({node:control: value}), as the filter-chain
    lists them: after the adapter's own Props. Anything that isn't a list of objects comes back as it was."""
    try:
        objects = json.loads(text)
    except (TypeError, ValueError):
        return text
    for o in objects if isinstance(objects, list) else ():
        if isinstance(o, dict) and ((o.get("info") or {}).get("props") or {}).get("node.name") == voice.INPUT:
            o["info"]["params"] = {"Props": [{"volume": 1.0}, {"params": [x for p in props.items() for x in p]}]}
    return json.dumps(objects)


def setUpModule():
    """No plugin verifies unless a test says so: apply() never reads the real plugin folder. apply() runs on landing()."""
    for patch in (mock.patch.object(voice, "verified", lambda where=None: {}, create=True),
                  mock.patch.object(voice, "apply", landing)):
        patch.start()
        unittest.addModuleCleanup(patch.stop)

CTRLS = scarlett.parse_contents(read("scarlett/amixer-contents.txt"))
MIC = scarlett.mic(CTRLS, DUMP, "XLR")
STATE = {"ctrls": CTRLS, "mic": MIC}  # scarlett.read: the Scarlett plugged in, the XLR on capture channel 2
ON = {"voice": {}}  # the defaults: processing on, rumble filter on
UNITS = ["booth-voice.service", "booth-mic-select.service"]
# a new chain: both, the selector re-links to the new input; try-restart: never starts a unit the user stopped
RESTART = ["systemctl", "--user", "try-restart"] + UNITS
ACTIVE = ["systemctl", "--user", "is-active", "booth-voice.service"]
START = ["systemctl", "--user", "start"] + UNITS
RESTART_SELECT = ["systemctl", "--user", "restart", "booth-mic-select.service"]
MOVE = [ACTIVE, RESTART_SELECT]  # a new channel: the selector restarted, only with Booth Mic running
STOP_SELECT = ["systemctl", "--user", "stop", "booth-mic-select.service"]


def rerouted(item):
    """scarlett.read with capture channel 2 (the XLR's) switched to another routing item, e.g. "PCM 1" (the computer's
    playback) or "Mix A" (the loopback mix): the Scarlett is there, the mic reaches no capture channel."""
    ctrls = copy.deepcopy(CTRLS)
    ctrls["PCM 02 Capture Enum"]["values"] = [ctrls["PCM 02 Capture Enum"]["items"].index(item)]
    return {"ctrls": ctrls, "mic": scarlett.mic(ctrls, DUMP, "XLR")}


def xlr_on_channel_1(dump=DUMP):
    """scarlett.read after a reroute that put the XLR on capture channel 1 (FL), the computer's playback on 2."""
    ctrls = copy.deepcopy(CTRLS)
    for pcm, item in (("PCM 01 Capture Enum", "DSP 2"), ("PCM 02 Capture Enum", "PCM 1")):
        ctrls[pcm]["values"] = [ctrls[pcm]["items"].index(item)]
    return {"ctrls": ctrls, "mic": scarlett.mic(ctrls, dump, "XLR")}


def plugged(mic):
    return {"ctrls": CTRLS, "mic": mic}


def node(i, name, serial=None):
    props = {"node.name": name} if serial is None else {"node.name": name, "object.serial": serial}
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def chain_unsaved(cfg, file):
    """store.save with voice.conf unwritable (disk full, read-only): the selector's own config still saves."""
    if Path(file).name == "voice.conf":
        raise OSError(28, "No space left on device")
    real_save(cfg, file)


real_save = voice.store.save


CHAIN_DUMP = json.dumps([node(70, "booth_mic"), node(71, "booth.mic.input")])  # Booth Mic, then its input
SET = ["pw-cli", "set-param", "71"]  # a live change on Booth Mic's input, in one or more calls of voice.CHUNK


def calls(ran):
    """`ran`'s commands, each first 3 words, with a run of set-params (one live change, in chunks) as one SET."""
    return [k for k, _ in itertools.groupby(c[:3] for c in ran)]


def sent(cmds):
    """{node:control: value} every set-param in `cmds` sent, a later call's value winning."""
    return dict(p for c in cmds if c[:2] == ["pw-cli", "set-param"]
                for p in itertools.batched(json.loads(c[4])["params"], 2))


SCARLETT_BACK = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"])])  # the selector unloaded itself
SELECTING = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"]), node(80, "booth.mic.select")])


def config(settings=ON, found=None):
    """Booth Mic's chain: booth-voice.service's config, whatever the Scarlett does, with the plugins in `found` verified
    (none when not given)."""
    return json.loads(voice.build(settings, {} if found is None else found))


def descriptor(plugin_path, label):
    """The fixture's descriptor of `label` in the plugin file at `plugin_path`."""
    found = [d for d in PORTS[os.path.basename(plugin_path)] if d["label"] == label]
    assert len(found) == 1, (plugin_path, label)
    return found[0]


def audio(desc, direction):
    return [p["name"] for p in desc["ports"] if p["type"] == "audio" and p["direction"] == direction]


def ladspa(cfg):
    return [n for n in module(cfg, "libpipewire-module-filter-chain")["filter.graph"]["nodes"] if n["type"] == "ladspa"]


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
        if node["type"] == "ladspa":  # every audio input fed the same signal (a stereo plugin in a mono chain)
            ins = [signal(f"{name}:{p}") for p in audio(descriptor(node["plugin"], node["label"]), "input")]
            assert ins and all(i == ins[0] for i in ins), (name, ins)
            return {f"{name}({p})": g for p, g in ins[0].items()}
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
        for position in (None, ""):  # in PipeWire, but with fewer channels than the mic's: never a guessed one
            self.assertIsNone(voice.build_selector(dict(MIC, position=position)), position)

    def test_the_always_on_selector_capture_is_hidden_from_pulse_source_outputs(self):
        # module-loopback's default Stream/Input/Audio is a pipewire-pulse source-output for as long as it runs:
        # GNOME's mic-in-use indicator would stay on with no app reading Booth Mic
        capture = module(selector(), "libpipewire-module-loopback")["capture.props"]
        self.assertEqual(capture["media.class"], "Stream/Input/Audio/Internal")

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
        self.assertEqual(types, {"builtin"})  # no plugin verified: no LADSPA plugin

    def test_switches_change_only_control_values_so_they_never_need_a_restart(self):
        configs = [config({"voice": {"enabled": e, "rumble": r}}) for e in (True, False) for r in (True, False)]
        self.assertEqual({json.dumps(voice.shape(c), sort_keys=True) for c in configs}.__len__(), 1)
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in configs}), 4)

    def test_the_live_change_sets_every_control_on_the_chain_input_node(self):
        cfg = config({"voice": {"rumble": False}})
        cmds = voice.set_params_cmds(CHAIN_DUMP, cfg)
        self.assertEqual({tuple(c[:4]) for c in cmds}, {("pw-cli", "set-param", "71", "Props")})
        pairs = sent(cmds)
        self.assertEqual((pairs["rumble_mix:Gain 1"], pairs["rumble_mix:Gain 2"]), (0.0, 1.0))
        self.assertEqual((pairs["master:Gain 1"], pairs["master:Gain 2"]), (1.0, 0.0))
        self.assertEqual(len(pairs), sum(len(n.get("control", {})) for n in voice._graph_nodes(cfg)))  # every one
        self.assertTrue(all(isinstance(v, float) for v in pairs.values()))  # pw-cli reads 1 as an Int

    def test_no_chain_running_no_live_change(self):
        self.assertIsNone(voice.set_params_cmds("[]", config()))
        self.assertIsNone(voice.set_params_cmds("not json", config()))


class Stages(unittest.TestCase):
    """Noise suppression, leveling and the limiter: verified plugins only, always in the graph once verified, each with
    its own live dry/wet bypass."""

    def test_every_verified_stage_is_in_the_chain_in_order(self):
        self.assertEqual(flow(config(ON, FOUND)), {"limiter(leveling(noise(rumble(in))))": 1.0})

    def test_every_on_off_combination_bypasses_exactly_the_stages_that_are_off(self):
        keys = ("rumble",) + STAGE_KEYS
        for on in itertools.product((True, False), repeat=len(keys)):
            settings, path = {"voice": dict(zip(keys, on))}, "in"
            for key, active in zip(keys, on):
                path = f"{key}({path})" if active else path
            self.assertEqual(flow(config(settings, FOUND)), {path: 1.0}, settings)

    def test_the_master_switch_off_passes_the_raw_mic_whatever_the_stages(self):
        self.assertEqual(flow(config({"voice": {"enabled": False}}, FOUND)), {"in": 1.0})

    def test_a_plugin_that_didnt_verify_is_left_out_and_bypassed(self):
        for missing in plugins.MANIFEST:
            found = {k: v for k, v in FOUND.items() if k != missing}
            cfg = config(ON, found)
            self.assertNotIn(missing, [os.path.basename(n["plugin"]) for n in ladspa(cfg)])
            gone = voice.missing(found)
            self.assertEqual(len(gone), 1, missing)
            path = "rumble(in)"
            for key in STAGE_KEYS:
                path = path if key in gone else f"{key}({path})"
            self.assertEqual(flow(cfg), {path: 1.0}, missing)
        self.assertEqual((flow(config(ON, {})), ladspa(config(ON, {}))), ({"rumble(in)": 1.0}, []))
        self.assertEqual((voice.missing({}), voice.missing(FOUND)), (list(STAGE_KEYS), []))

    def test_plugins_are_loaded_only_from_the_paths_verification_returned(self):
        self.assertEqual(sorted(n["plugin"] for n in ladspa(config(ON, FOUND))), sorted(FOUND.values()))

    def test_labels_and_control_names_match_the_captured_descriptors_exactly(self):
        nodes = ladspa(config(ON, FOUND))
        self.assertEqual(len(nodes), 3)
        for n in nodes:
            desc = descriptor(n["plugin"], n["label"])  # the label itself must be in the fixture
            ports = {p["name"]: p for p in desc["ports"] if p["type"] == "control" and p["direction"] == "input"}
            self.assertTrue(n["control"], n["name"])
            for name, value in n["control"].items():
                self.assertIn(name, ports, n["name"])
                self.assertIsInstance(value, float, name)  # pw-cli reads 1 as an Int
                self.assertGreaterEqual(value, ports[name].get("lower", value), name)
                self.assertLessEqual(value, ports[name].get("upper", value), name)

    def test_each_stage_output_is_a_real_output_port_of_its_plugin(self):
        cfg = config(ON, FOUND)
        graph = module(cfg, "libpipewire-module-filter-chain")["filter.graph"]
        for n in ladspa(cfg):
            outs = {lk["output"].split(":", 1)[1] for lk in graph["links"] if lk["output"].startswith(n["name"] + ":")}
            self.assertEqual(len(outs), 1, n["name"])
            self.assertIn(outs.pop(), audio(descriptor(n["plugin"], n["label"]), "output"))

    def test_switches_and_knobs_never_change_the_shape_so_booth_mic_is_never_restarted(self):
        configs = [config({"voice": {"noise": a, "leveling": b, "limiter": c, "noise_strength": k,
                                     "leveling_amount": k, "limiter_ceiling": -k / 10}}, FOUND)
                   for a, b, c in itertools.product((True, False), repeat=3) for k in (0, 50, 100)]
        self.assertEqual(len({json.dumps(voice.shape(c), sort_keys=True) for c in configs}), 1)
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in configs}), len(configs))

    def test_a_stage_toggle_is_set_live_on_its_own_mixer(self):
        pairs = sent(voice.set_params_cmds(CHAIN_DUMP, config({"voice": {"leveling": False}}, FOUND)))
        self.assertEqual((pairs["leveling_mix:Gain 1"], pairs["leveling_mix:Gain 2"]), (0.0, 1.0))
        self.assertEqual((pairs["noise_mix:Gain 1"], pairs["noise_mix:Gain 2"]), (1.0, 0.0))
        self.assertEqual(pairs["limiter:Limit (dB)"], -1.0)  # the knobs ride along: live too
        self.assertTrue(all(isinstance(v, float) for v in pairs.values()))

    def test_the_knobs_reach_their_stages(self):
        cfg = config({"voice": {"noise_strength": 100, "leveling_amount": 0, "limiter_ceiling": -3.5}}, FOUND)
        self.assertEqual({n["name"]: n["control"] for n in ladspa(cfg)},
                         {"noise": voice.strength(100), "leveling": voice.amount(0), "limiter": voice.ceiling(-3.5)})

    def test_the_defaults_are_on_at_the_middle_with_a_minus_1_db_ceiling(self):
        self.assertEqual({n["name"]: n["control"] for n in ladspa(config(ON, FOUND))},
                         {"noise": voice.strength(50), "leveling": voice.amount(50), "limiter": voice.ceiling(-1.0)})


class Mappings(unittest.TestCase):
    """Each friendly knob → its plugin's raw controls: both ends and the middle."""

    def test_strength_raises_the_voice_gate(self):
        self.assertEqual(voice.strength(0), {"VAD Threshold (%)": 0.0, "VAD Grace Period (ms)": 200.0,
                                             "Retroactive VAD Grace (ms)": 0.0})
        self.assertEqual(voice.strength(50)["VAD Threshold (%)"], 48.0)  # a whole percent: RNNoise reads it as an integer
        self.assertEqual(voice.strength(100)["VAD Threshold (%)"], 95.0)

    def test_amount_lowers_the_threshold_and_raises_the_ratio_with_makeup_gain(self):
        fixed = {"RMS/peak": 0.0, "Attack time (ms)": 10.0, "Release time (ms)": 150.0, "Knee radius (dB)": 5.0}
        self.assertEqual(voice.amount(0), {**fixed, "Threshold level (dB)": 0.0, "Ratio (1:n)": 1.0,
                                           "Makeup gain (dB)": 0.0})
        self.assertEqual(voice.amount(50), {**fixed, "Threshold level (dB)": -15.0, "Ratio (1:n)": 3.5,
                                            "Makeup gain (dB)": 5.36})
        self.assertEqual(voice.amount(100), {**fixed, "Threshold level (dB)": -30.0, "Ratio (1:n)": 6.0,
                                             "Makeup gain (dB)": 12.5})

    def test_ceiling_is_the_limit_in_db(self):
        for db in (0, -1.0, -20.0):
            self.assertEqual(voice.ceiling(db), {"Input gain (dB)": 0.0, "Limit (dB)": float(db),
                                                 "Release time (s)": 0.05})
        self.assertIsInstance(voice.ceiling(0)["Limit (dB)"], float)


def plugin_folder(test, good, bad):
    """A plugin folder and a manifest: each of `good` holds its pinned bytes, each of `bad` other bytes."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    where = Path(tmp.name) / "plugins"
    where.mkdir()
    where.chmod(0o755)
    manifest = {name: hashlib.sha256(name.encode()).hexdigest() for name in plugins.MANIFEST}
    for name in good:
        (where / name).write_bytes(name.encode())
    for name in bad:
        (where / name).write_bytes(b"changed after install")
    for f in where.iterdir():
        f.chmod(0o644)
    return where, manifest


class Verification(unittest.TestCase):
    """The trust seam: every chain Booth writes holds only plugins verified while it was built."""

    def found(self, where, manifest):
        return REAL_VERIFIED(where, manifest)

    def test_only_plugins_that_verify_now_are_found(self):
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names[:1], bad=names[1:2])  # the third is missing
        self.assertEqual(self.found(where, manifest), {names[0]: str(where / names[0])})
        where, manifest = plugin_folder(self, good=names, bad=())
        self.assertEqual(self.found(where, manifest), {n: str(where / n) for n in names})

    def test_a_symlinked_plugin_is_never_found(self):
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names, bad=())
        real = where.parent / "elsewhere.so"
        (where / names[0]).rename(real)
        (where / names[0]).symlink_to(real)
        self.assertNotIn(names[0], self.found(where, manifest))

    def hashed(self, call):
        """The plugin files call() hashed (plugins.verify ran on)."""
        names, real = [], plugins.verify
        with mock.patch.object(voice.plugins, "verify", lambda name, *a: names.append(name) or real(name, *a)):
            call()
        return names

    def test_an_unchanged_plugin_is_hashed_once_across_polls_and_a_changed_one_again(self):
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names, bad=())
        every = {n: str(where / n) for n in names}
        self.assertEqual(sorted(self.hashed(lambda: self.assertEqual(self.found(where, manifest), every))), sorted(names))
        self.assertEqual(self.hashed(lambda: self.assertEqual(self.found(where, manifest), every)), [])  # 5.7 MB: once
        (where / names[0]).write_bytes(b"changed")
        (where / names[0]).chmod(0o644)
        (where / names[1]).chmod(0o664)  # group-writable: refused, its bytes unchanged
        self.assertEqual(self.found(where, manifest), {names[2]: every[names[2]]})
        (where / names[1]).chmod(0o644)
        where.chmod(0o775)  # its folder group-writable: everything in it refused
        self.assertEqual(self.found(where, manifest), {})

    def test_a_same_size_rewrite_with_its_mtime_put_back_is_hashed_again(self):  # only its ctime tells
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names, bad=())
        p, every = where / names[0], {n: str(where / n) for n in names}
        self.assertEqual(self.found(where, manifest), every)
        before = os.stat(p)
        for _ in range(100):  # a coarse ctime clock: until the rewrite lands on another tick
            p.write_bytes(b"\0" * before.st_size)
            os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
            if os.stat(p).st_ctime_ns != before.st_ctime_ns:
                break
            time.sleep(0.01)
        self.assertEqual((os.stat(p).st_size, os.stat(p).st_mtime_ns), (before.st_size, before.st_mtime_ns))
        self.assertNotIn(names[0], self.found(where, manifest))

    def test_each_field_of_the_cache_key_alone_gets_the_plugin_hashed_again(self):
        """A swap by rename (inode), a hardlink or chmod (ctime, mode), a chown (uid), the folder replaced or changed:
        each one field that differs from the cached verdict's misses it. A new pin too."""
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names, bad=())
        name, real = names[0], os.stat
        fields = ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid")
        self.found(where, manifest)
        folder_fields = ("st_ino", "st_mode", "st_uid", "st_ctime_ns")  # a new entry in it changes its ctime
        for folder, field in [(False, f) for f in fields] + [(True, f) for f in folder_fields]:
            with self.subTest(folder=folder, field=field):
                def stat(p, *a, **k):
                    s, bump = real(p, *a, **k), Path(p) == (where if folder else where / name)
                    return types.SimpleNamespace(**{f: getattr(s, f) + (f == field and bump) for f in fields})
                with mock.patch.object(voice.os, "stat", stat):
                    self.assertIn(name, self.hashed(lambda: self.found(where, manifest)))
                self.found(where, manifest)  # cached again on the real stat
                self.assertEqual(self.hashed(lambda: self.found(where, manifest)), [])
        self.assertNotIn(name, self.found(where, dict(manifest, **{name: "0" * 64})))  # pinned to other bytes now

    def test_the_pre_start_hashes_every_plugin_every_time_never_a_cached_verdict(self):
        names = list(plugins.MANIFEST)
        where, manifest = plugin_folder(self, good=names, bad=())
        run = self.tmp_file().with_name(voice.RUN_CONF)
        with mock.patch.dict(plugins.MANIFEST, manifest), mock.patch.object(voice, "verified", REAL_VERIFIED), \
                mock.patch.object(voice.store, "load", lambda **_: ON):
            REAL_VERIFIED(where, manifest)  # a poll's: cached
            for _ in range(2):
                self.assertEqual(sorted(self.hashed(lambda: voice.write(run, where))), sorted(names))
            with mock.patch.object(voice.plugins, "folder", lambda: where):
                REAL_VERIFIED()
                self.assertEqual(sorted(self.hashed(lambda: voice.reload(run=lambda cmd: True, file=run))),
                                 sorted(names))

    def test_every_build_verifies_again(self):
        calls = []
        with mock.patch.object(voice, "verified", lambda: calls.append(1) or FOUND):
            voice.build(ON)
            voice.build(ON)
        self.assertEqual(len(calls), 2)

    def tmp_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / "voice.conf"

    def test_a_first_chain_holds_only_what_verified_just_before(self):
        file, ran = self.tmp_file(), []
        changed = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        with mock.patch.object(voice, "verified", lambda: changed):
            voice.apply(ON, STATE, run=lambda cmd: ran.append(cmd) or True, dump=lambda: CHAIN_DUMP, file=file)
        self.assertEqual(sorted(n["plugin"] for n in ladspa(json.loads(file.read_text()))), sorted(changed.values()))
        self.assertEqual(ran, [RESTART])

    def test_a_plugin_that_stops_verifying_is_bypassed_live_and_booth_mic_never_restarts(self):
        """Booth Mic runs the verified code it mapped at its start: a plugin changed, or a read that fails for a moment
        (EIO, EMFILE), must never drop Booth Mic in a call. Its stage goes dry live; the next start leaves it out."""
        file, ran = self.tmp_file(), []

        def apply(settings=ON):
            return voice.apply(settings, STATE, run=lambda cmd: ran.append(cmd) or True, dump=lambda: CHAIN_DUMP,
                               file=file)
        with mock.patch.object(voice, "verified", lambda: FOUND):
            apply()
        for why in (plugins.Refused("sc4m_1916.so: sha256 mismatch"), OSError(5, "Input/output error"),
                    OSError(24, "Too many open files")):
            real_verify = plugins.verify
            verify = lambda name, *a: (_ for _ in ()).throw(why) if name == "sc4m_1916.so" else real_verify(name, *a)
            where, manifest = plugin_folder(self, good=list(plugins.MANIFEST), bad=())
            with mock.patch.object(voice.plugins, "verify", verify):
                self.assertNotIn("sc4m_1916.so", REAL_VERIFIED(where, manifest), why)
        changed = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        ran.clear()
        with mock.patch.object(voice, "verified", lambda: changed):
            self.assertEqual(apply(), "live")
            self.assertEqual(calls(ran), [SET])  # no systemctl: Booth Mic stays up
            cfg = json.loads(file.read_text())
            self.assertEqual(sorted(n["plugin"] for n in ladspa(cfg)), sorted(FOUND.values()))  # the running chain
            self.assertEqual(flow(cfg), {"limiter(noise(rumble(in)))": 1.0})  # leveling dry
            self.assertEqual(voice.loaded(file), changed)  # the page: Leveling missing
            ran.clear()
            self.assertEqual(apply({"voice": {"leveling": True, "noise_strength": 80}}), "live")  # still dry
            self.assertEqual(flow(json.loads(file.read_text())), {"limiter(noise(rumble(in)))": 1.0})
            self.assertTrue(all(c[0] == "pw-cli" for c in ran), ran)
        ran.clear()
        with mock.patch.object(voice, "verified", lambda: FOUND):  # it verifies again: wet, live
            self.assertEqual(apply(), "live")
        self.assertEqual(flow(json.loads(file.read_text())), {"limiter(leveling(noise(rumble(in))))": 1.0})
        self.assertEqual(calls(ran), [SET])

    def test_a_plugin_that_verifies_only_since_booth_mic_started_waits_for_the_next_start(self):
        """Also an old Booth's chain (no stages) after an install that fetched the plugins: no restart under a call."""
        file, ran = self.tmp_file(), []
        for running in ({}, {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}):
            file.write_text(voice.build(ON, running))
            ran.clear()
            with mock.patch.object(voice, "verified", lambda: FOUND):
                self.assertEqual(voice.apply(ON, None, run=lambda cmd: ran.append(cmd) or True,
                                             dump=lambda: CHAIN_DUMP, file=file), "unchanged")
                self.assertEqual(voice.loaded(file), running)
            self.assertEqual((ran, json.loads(file.read_text())), ([], config(ON, running)))

    def test_a_new_shape_from_new_code_still_restarts_with_every_plugin_verified_now(self):
        file, ran = self.tmp_file(), []
        old = config(ON, FOUND)
        module(old, "libpipewire-module-filter-chain")["filter.graph"]["nodes"].pop(1)  # an older chain's shape
        file.write_text(json.dumps(old))
        with mock.patch.object(voice, "verified", lambda: dict(list(FOUND.items())[:2])):
            self.assertEqual(voice.apply(ON, None, run=lambda cmd: ran.append(cmd) or True, dump=lambda: CHAIN_DUMP,
                                         file=file), "restarted")
        self.assertEqual((ran, json.loads(file.read_text())), ([RESTART], config(ON, dict(list(FOUND.items())[:2]))))

    def test_reload_restarts_a_running_booth_mic_only_when_its_plugins_arent_the_verified_ones(self):
        file, ran = self.tmp_file(), []
        changed = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        for running, now, ok, want in ((FOUND, FOUND, True, []), (changed, FOUND, True, [RESTART]),
                                       (FOUND, changed, True, [RESTART]), (changed, FOUND, False, [RESTART])):
            file.write_text(voice.build(ON, running))
            ran.clear()
            with mock.patch.object(voice, "verified", lambda: now):
                self.assertEqual(voice.reload(run=lambda cmd: ran.append(cmd) or ok, file=file), ok)
            self.assertEqual(ran, want)
        file.write_text(voice.build(ON, FOUND))  # what Booth wants; its pre-start left sc4m out of what runs
        file.with_name(voice.RUN_CONF).write_text(voice.build(ON, changed))
        ran.clear()
        with mock.patch.object(voice, "verified", lambda: FOUND):
            self.assertTrue(voice.reload(run=lambda cmd: ran.append(cmd) or True, file=file))
        self.assertEqual(ran, [RESTART])
        file.with_name(voice.RUN_CONF).unlink()
        file.unlink()
        with mock.patch.object(voice, "verified", lambda: {}):
            self.assertTrue(voice.reload(run=lambda cmd: ran.append(cmd) or True, file=file))
        self.assertEqual(ran[-1], RESTART)  # try-restart: none running, none started

    def test_the_pre_start_loads_every_plugin_that_verifies_in_its_folder_and_the_next_poll_leaves_it(self):
        file, ran = self.tmp_file(), []
        where, manifest = plugin_folder(self, good=list(plugins.MANIFEST), bad=())
        elsewhere = file.parent / "not-the-installed-plugins"
        with mock.patch.dict(plugins.MANIFEST, manifest), mock.patch.object(voice, "verified", REAL_VERIFIED), \
                mock.patch.object(voice.store, "load", lambda **_: ON):
            with mock.patch.object(voice.plugins, "folder", lambda: where):
                voice.apply(ON, STATE, run=lambda cmd: True, dump=lambda: CHAIN_DUMP, file=file)
            file.write_text(voice.build(ON, {}))  # the chain an old Booth wrote
            with mock.patch.object(voice.plugins, "folder", lambda: elsewhere), \
                    mock.patch.object(voice.sys, "argv", ["x", "--write", str(file), str(where)]):
                self.assertEqual(voice.main(), 0)
            self.assertEqual(sorted(n["plugin"] for n in ladspa(json.loads(file.read_text()))),
                             sorted(str(where / n) for n in manifest))
            with mock.patch.object(voice.plugins, "folder", lambda: where):
                self.assertEqual(voice.apply(ON, STATE, run=lambda cmd: ran.append(cmd) or True,
                                             dump=lambda: CHAIN_DUMP, file=file), "unchanged")  # no restart loop
        self.assertEqual(ran, [])

    def test_the_services_pre_start_rewrites_the_chain_from_the_settings_and_a_fresh_verification(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "voice.conf"
        file.write_text(voice.build(ON, FOUND))  # written while every plugin verified
        settings = {"voice": {"noise": False}}
        with mock.patch.object(voice.store, "load", lambda **_: settings):
            self.assertIsNone(voice.write(file))  # all three changed since (none verifies now): none is ever loaded
        self.assertEqual(json.loads(file.read_text()), config(settings, {}))
        self.assertEqual(ladspa(json.loads(file.read_text())), [])

    def test_a_pre_start_that_cant_write_fails_so_the_old_chain_never_starts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "voice.conf"
        file.mkdir()  # a folder where the file goes: the write fails
        with mock.patch.object(voice.store, "load", lambda **_: ON):
            self.assertIn("not saved", voice.write(file))
            with mock.patch.object(voice, "path", lambda: file.with_name("voice.conf")), \
                    mock.patch.object(voice, "RUN_CONF", file.name), mock.patch.object(voice.sys, "argv", ["x", "--write"]):
                self.assertEqual(voice.main(), 1)

    def test_the_pre_start_never_runs_systemctl_or_reads_the_scarlett(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "voice.conf"
        with mock.patch.object(voice.store, "load", lambda **_: ON), mock.patch.object(voice, "path", lambda: file), \
                mock.patch.object(voice.sys, "argv", ["x", "--write"]), \
                mock.patch.object(voice.service, "run", side_effect=AssertionError("ran")), \
                mock.patch.object(voice.scarlett, "read", side_effect=AssertionError("read the Scarlett")):
            self.assertEqual(voice.main(), 0)
        self.assertEqual(json.loads(file.with_name("voice-run.conf").read_text()), config())
        self.assertFalse(file.exists())  # Booth's own record: never the pre-start's


    def test_pre_start_refuses_to_start_when_conf_unwritable_even_if_its_plugins_verify(self):
        """A full disk or read-only config folder: the run conf on disk loads only plugins verified now, at the paths
        verified, so Booth Mic starts on it. One naming any other plugin path never starts."""
        run = self.tmp_file().with_name("voice-run.conf")
        where, manifest = plugin_folder(self, good=list(plugins.MANIFEST), bad=())
        good = {n: str(where / n) for n in manifest}
        settings = {"voice": {"noise": False}}
        with mock.patch.dict(plugins.MANIFEST, manifest), mock.patch.object(voice, "verified", REAL_VERIFIED), \
                mock.patch.object(voice.store, "load", lambda **_: settings), \
                mock.patch.object(voice.sys, "argv", ["x", "--write", str(run), str(where)]):
            for running, code in ((good, 0), (dict(list(good.items())[:1]), 0),
                                  (dict(good, **{"sc4m_1916.so": "/elsewhere/sc4m_1916.so"}), 1)):
                run.write_text(voice.build(ON, running))
                with mock.patch.object(voice.store, "save", side_effect=OSError(30, "Read-only file system")):
                    self.assertEqual(voice.main(), code, running)
            run.write_text(voice.build(settings, good))  # what it would write: nothing to write
            with mock.patch.object(voice.store, "save", side_effect=AssertionError("wrote an unchanged config")):
                self.assertEqual(voice.main(), 0)
        self.assertEqual(voice.describe(STATE, selecting=False, running=False), voice.STOPPED)  # not the Scarlett
        self.assertNotIn("Scarlett", voice.STOPPED)

    def test_a_chain_naming_a_plugin_at_another_path_never_counts_as_loaded(self):
        file = self.tmp_file()
        file.write_text(voice.build(ON, dict(FOUND, **{"sc4m_1916.so": "/tmp/elsewhere/sc4m_1916.so"})))
        with mock.patch.object(voice, "verified", lambda: FOUND):
            self.assertEqual(voice.loaded(file), {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"})

    def test_the_running_chain_is_the_pre_starts_config_never_the_one_apply_wrote(self):
        """voice.conf is what Booth wants; voice-run.conf what ExecStartPre wrote and pipewire loaded. A plugin the
        pre-start left out isn't running, whatever voice.conf says: the page shows it missing, its stage dry, no restart."""
        file, ran = self.tmp_file(), []
        run = file.with_name("voice-run.conf")
        changed = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        file.write_text(voice.build(ON, FOUND))
        run.write_text(voice.build(ON, changed))
        os.utime(run, ns=(10**17, 10**17))  # recorded since its start: voice.conf holds what was set live
        with mock.patch.object(voice, "verified", lambda: FOUND):
            self.assertEqual(voice.loaded(file), changed)
            self.assertEqual(voice.apply(ON, None, run=lambda cmd: ran.append(cmd) or True, dump=lambda: CHAIN_DUMP,
                                         file=file), "live")
        self.assertEqual(calls(ran), [SET])
        self.assertEqual(flow(json.loads(file.read_text())), {"limiter(noise(rumble(in)))": 1.0})  # leveling dry
        self.assertEqual(run.read_text(), voice.build(ON, changed))  # never written by apply

    def test_skeptic_a_poll_racing_the_pre_start_leaves_an_unverified_plugin_in_the_conf_pipewire_loads(self):
        """A poll's apply reads the configs, then waits on pw-dump before it writes. booth-voice.service starting in that
        gap runs --write: apply's write lands after it, before ExecStart's pipewire reads its config."""
        file = self.tmp_file()
        run = file.with_name("voice-run.conf")
        where, manifest = plugin_folder(self, good=list(plugins.MANIFEST), bad=())
        with mock.patch.dict(plugins.MANIFEST, manifest), mock.patch.object(voice, "verified", REAL_VERIFIED), \
                mock.patch.object(voice.plugins, "folder", lambda: where), \
                mock.patch.object(voice.store, "load", lambda **_: ON):
            voice.apply(ON, None, run=lambda c: True, dump=lambda: CHAIN_DUMP, file=file)  # Booth Mic, all verified
            (where / "sc4m_1916.so").write_bytes(b"changed after the chain started")  # stays 0644, user-owned
            pre_start = []

            def dump():  # the pre-start runs while this poll waits on its first pw-dump
                if not pre_start:
                    pre_start.append(voice.write(run, where))
                return CHAIN_DUMP
            voice.apply(ON, None, run=lambda c: True, dump=dump, file=file)
            self.assertEqual(pre_start, [None])
            with self.assertRaises(plugins.Refused):
                plugins.verify("sc4m_1916.so", where, manifest)
            self.assertNotIn(str(where / "sc4m_1916.so"), [n["plugin"] for n in ladspa(json.loads(run.read_text()))],
                             "the file ExecStart's pipewire reads names a plugin that fails verify()")


class KeptRunConf(unittest.TestCase):
    """booth-voice's pre-start can't replace voice-run.conf (disk full) and Booth Mic starts on the one there."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.run_conf = self.file.with_name(voice.RUN_CONF)

    def full_disk(self, settings):
        return mock.patch.multiple(voice.store, load=lambda **_: settings,
                                   save=mock.Mock(side_effect=OSError(28, "No space left on device")))

    def test_a_kept_run_conf_with_older_values_gets_the_saved_ones_live_once(self):
        """Booth set processing off live and recorded it; Booth Mic restarts on a full disk on its older run conf
        (processing on): the next apply sends off live, then leaves it."""
        off = {"voice": {"enabled": False}}
        self.file.write_text(voice.build(off, FOUND))
        os.utime(self.file, ns=(10**18, 10**18))  # recorded before Booth Mic's start
        self.run_conf.write_text(voice.build(ON, FOUND))
        os.utime(self.run_conf, ns=(10**17, 10**17))  # written at an earlier start
        ran = []
        with mock.patch.object(voice, "verified", lambda where=None: FOUND):
            with self.full_disk(off):
                self.assertTrue(voice.write(self.run_conf).startswith("kept"))  # exit 0: starts on processing ON
            said = voice.apply(off, None, run=lambda c: ran.append(c) or True, dump=lambda: CHAIN_DUMP, file=self.file)
            self.assertEqual((calls(ran), said), ([SET], "live"))
            self.assertEqual(sent(ran)["master:Gain 1"], 0.0)
            ran.clear()
            self.assertEqual(voice.apply(off, None, run=lambda c: ran.append(c) or True, dump=lambda: CHAIN_DUMP,
                                         file=self.file), "unchanged")
        self.assertEqual(ran, [])
        self.assertEqual(self.run_conf.read_text(), voice.build(ON, FOUND))  # never written by apply

    def test_a_run_conf_no_newer_than_voice_conf_counts_as_voice_conf(self):  # an equal mtime: voice.conf's values
        off = {"voice": {"enabled": False}}
        self.file.write_text(voice.build(off, FOUND))
        self.run_conf.write_text(voice.build(ON, FOUND))
        for f in (self.file, self.run_conf):
            os.utime(f, ns=(10**18, 10**18))
        ran = []
        with mock.patch.object(voice, "verified", lambda where=None: FOUND):
            said = voice.apply(off, None, run=lambda c: ran.append(c) or True, dump=lambda: CHAIN_DUMP, file=self.file)
        self.assertEqual((said, ran), ("unchanged", []))

    def test_a_run_conf_of_another_shape_is_never_kept_so_booth_mic_is_restarted_once(self):
        """New code (a new shape), voice.conf saved, the run conf can't be replaced: kept, Booth Mic would run the old
        shape and every poll's apply try-restart it again."""
        self.file.write_text(voice.build(ON, FOUND))
        old = json.loads(voice.build(ON, FOUND))
        module(old, "libpipewire-module-filter-chain")["filter.graph"]["nodes"].append(
            {"type": "builtin", "name": "oldnode", "label": "copy"})
        self.run_conf.write_text(json.dumps(old))
        running, restarts = [True], []

        def run(cmd):  # systemd: a failed pre-start leaves the unit stopped; try-restart restarts only a running one
            if cmd == RESTART and running[0]:
                restarts.append(voice.write(self.run_conf))
                running[0] = not restarts[-1].startswith("not ")
                return running[0]
            return True
        with mock.patch.object(voice, "verified", lambda where=None: FOUND), self.full_disk(ON):
            for _ in range(3):
                voice.apply(ON, None, run=run, dump=lambda: CHAIN_DUMP, file=self.file)
        self.assertEqual(restarts, ["not saved: No space left on device"])
        self.assertEqual(json.loads(self.run_conf.read_text()), old)


class Apply(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "booth" / "voice.conf"
        self.select = self.file.with_name("voice-select.conf")
        self.ran, self.fails = [], ()

    def apply(self, settings=ON, state=STATE):
        return voice.apply(settings, state, run=lambda cmd: self.ran.append(cmd) or not set(cmd[:3]) & set(self.fails),
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
        self.assertEqual(calls(self.ran), [SET])
        self.assertEqual(json.loads(self.file.read_text()), config({"voice": {"rumble": False}}))

    def test_another_input_restarts_only_the_selector_never_booth_mic(self):
        self.apply()
        chain, self.ran = self.file.read_text(), []
        instrument = scarlett.mic(CTRLS, DUMP, "Instrument")
        self.apply(state=plugged(instrument))
        self.assertEqual(self.ran, MOVE)
        self.assertEqual(self.file.read_text(), chain)  # booth-voice.service's config: untouched
        self.assertEqual(json.loads(self.select.read_text()), selector(instrument))

    def test_unplugged_keeps_the_last_selector_and_runs_nothing(self):
        self.apply()
        before, self.ran = self.select.read_text(), []
        self.assertEqual(self.apply(state=None), "unchanged")  # it unloaded itself; heal brings it back
        self.assertEqual((self.select.read_text(), self.ran), (before, []))

    def test_not_routed_stops_the_selector_whose_channel_now_carries_computer_audio(self):
        for item in ("PCM 1", "Mix A"):  # capture 2 rerouted: the computer's playback, the loopback mix
            self.apply()
            self.ran.clear()
            self.assertIsNone(rerouted(item)["mic"])
            self.apply(state=rerouted(item))
            self.assertEqual(self.ran, [STOP_SELECT], item)
            self.assertFalse(self.select.exists())  # nothing to restart it with: not at login, not by heal
            self.ran.clear()
            self.assertEqual(self.apply(state=rerouted(item)), "unchanged")  # stopped once
            self.assertEqual(self.ran, [])

    def test_not_routed_stops_a_selector_whose_old_config_has_no_channel_recorded(self):
        self.apply()
        cfg = json.loads(self.select.read_text())  # written before booth.mic.channel existed
        del module(cfg, "libpipewire-module-loopback")["capture.props"][voice.CHANNEL]
        self.select.write_text(json.dumps(cfg))
        self.ran.clear()
        self.apply(state=rerouted("PCM 1"))  # no mic at all: nothing to keep it for
        self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False))

    def test_a_mic_with_no_channel_to_take_stops_the_selector(self):
        self.apply()
        self.ran.clear()
        self.apply(state=plugged(dict(MIC, position=None)))  # the Scarlett's node has fewer channels
        self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False))

    def test_a_pw_dump_that_fails_leaves_a_correctly_routed_selector_alone(self):
        self.apply()
        before, self.ran = self.select.read_text(), []
        blip = plugged(scarlett.mic(CTRLS, "", "XLR"))  # pw-dump timed out: no node, still routed to capture 2
        self.assertEqual(self.apply(state=blip), "unchanged")
        self.assertEqual((self.ran, self.select.read_text()), ([], before))

    def test_an_unsaved_chain_change_never_holds_back_a_new_channel(self):
        self.apply()
        instrument = scarlett.mic(CTRLS, DUMP, "Instrument")
        self.ran.clear()
        with mock.patch.object(voice.store, "save", chain_unsaved):
            said = self.apply({"voice": {"rumble": False}}, state=plugged(instrument))
        self.assertIn("not saved", said)
        self.assertEqual(json.loads(self.select.read_text()), selector(instrument))
        self.assertEqual(calls(self.ran), [SET, ACTIVE[:3], RESTART_SELECT[:3]])
        self.assertEqual(self.ran[-1], RESTART_SELECT)

    def test_a_live_change_booth_mic_didnt_take_never_holds_back_a_new_channel(self):
        self.apply()
        instrument = scarlett.mic(CTRLS, DUMP, "Instrument")
        self.ran, self.fails = [], ("pw-cli",)
        self.assertIn("not applied", self.apply({"voice": {"rumble": False}}, state=plugged(instrument)))
        self.assertEqual(json.loads(self.select.read_text()), selector(instrument))
        self.assertEqual(self.ran[-1], RESTART_SELECT)
        self.assertEqual(json.loads(self.file.read_text()), config())  # not recorded: the next apply retries it

    def test_a_new_channel_restarts_the_selector_whatever_pw_dump_says(self):
        instrument = scarlett.mic(CTRLS, DUMP, "Instrument")
        for blip in ("", json.dumps([node(40, MIC["node"])])):  # pw-dump timed out; Booth Mic's input not listed
            self.apply()
            self.ran.clear()
            said = voice.apply(ON, plugged(instrument), run=lambda cmd: self.ran.append(cmd) or True,
                               dump=lambda: blip, file=self.file)
            self.assertEqual((said, self.ran), ("selector restarted", MOVE), blip)
            self.assertEqual(json.loads(self.select.read_text()), selector(instrument))
            self.apply()  # back to the XLR for the next blip

    def test_a_pw_dump_that_fails_stops_a_selector_whose_channel_moved(self):
        self.apply()
        self.ran.clear()
        moved = xlr_on_channel_1(dump="")  # no node: only the ALSA routing says the mic left capture 2
        self.assertEqual((moved["mic"]["channel"], moved["mic"]["node"]), (1, None))
        self.apply(state=moved)
        self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False))

    def test_a_selector_that_wont_stop_keeps_its_config_and_is_stopped_next_time(self):
        self.apply()
        self.ran, self.fails = [], ("systemctl",)
        self.assertTrue(self.apply(state=rerouted("PCM 1")).startswith("not stopped"))
        self.assertTrue(self.select.exists())
        self.ran, self.fails = [], ()
        self.apply(state=rerouted("PCM 1"))
        self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False))

    def test_a_failed_selector_restart_is_not_recorded_and_is_retried(self):
        self.apply()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        self.ran, self.fails = [], ("restart",)
        self.assertTrue(self.apply(state=instrument).startswith("not restarted"))
        self.assertFalse(self.select.exists())  # never the old channel back: Restart=, a later start would load it
        self.ran, self.fails = [], ()
        self.assertEqual(self.apply(state=instrument), "selector restarted")
        self.assertEqual(self.ran, MOVE)

    def test_a_new_channel_never_starts_a_booth_mic_the_user_stopped(self):
        self.apply()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        self.ran = []
        said = voice.apply(ON, instrument, run=lambda cmd: self.ran.append(cmd) or cmd != ACTIVE,
                           dump=lambda: CHAIN_DUMP, file=self.file)
        # restart booth-mic-select would start Booth Mic (BindsTo): stopped, so nothing starts on the old channel
        self.assertEqual((said, self.ran), ("selector stopped: Booth Mic is stopped", [ACTIVE, STOP_SELECT]))
        self.assertEqual(json.loads(self.select.read_text()), selector(instrument["mic"]))  # what its next start loads
        self.ran = []
        self.assertEqual(voice.apply(ON, instrument, run=lambda cmd: self.ran.append(cmd) or cmd != ACTIVE,
                                     dump=lambda: CHAIN_DUMP, file=self.file), "unchanged")
        self.assertEqual(self.ran, [])

    def test_a_selector_that_wont_stop_while_booth_mic_is_stopped_says_it_may_take_the_old_channel(self):
        self.apply()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        said = voice.apply(ON, instrument, run=lambda cmd: cmd not in (ACTIVE, STOP_SELECT), dump=lambda: CHAIN_DUMP,
                           file=self.file)
        self.assertTrue(said.startswith("not stopped"), said)
        self.assertEqual(voice.describe(instrument, True, said, running=False), voice.STALE)

    def test_plugins_are_verified_and_the_chain_written_while_holding_the_lock(self):
        """The last apply wins: the plugins verified just before its write, never another thread's in between."""
        held = []

        def other_thread_blocked():
            out = []

            def probe():
                got = voice._LOCK.acquire(blocking=False)
                if got:
                    voice._LOCK.release()
                out.append(not got)
            t = threading.Thread(target=probe)
            t.start()
            t.join()
            return out[0]

        def save(cfg, file):
            held.append(("saved", Path(file).name, other_thread_blocked()))
            real_save(cfg, file)
        with mock.patch.object(voice, "verified", lambda: held.append(("verified", other_thread_blocked())) or FOUND), \
                mock.patch.object(voice.store, "save", save):
            self.apply()
        self.assertEqual(held, [("verified", True), ("saved", "voice-select.conf", True), ("saved", "voice.conf", True)])

    def test_a_failed_chain_restart_is_not_recorded_and_is_retried(self):
        self.fails = ("systemctl",)
        self.assertTrue(self.apply().startswith("not restarted"))
        self.assertEqual((self.file.exists(), self.select.exists()), (False, False))
        self.file.write_text("{ half")
        self.assertTrue(self.apply().startswith("not restarted"))
        self.assertEqual(self.file.read_text(), "{ half")  # what it held before
        self.ran, self.fails = [], ()
        self.assertEqual(self.apply(), "restarted")
        self.assertEqual(self.ran, [RESTART])

    def test_a_switch_is_applied_with_the_scarlett_unplugged(self):
        self.apply()
        self.ran.clear()
        self.apply({"voice": {"enabled": False}}, state=None)
        self.assertEqual(calls(self.ran), [SET])

    def test_a_live_change_that_fails_is_not_recorded_and_the_next_apply_retries(self):
        self.apply()
        before, self.ran, self.fails = self.file.read_text(), [], ("pw-cli",)
        self.assertTrue(self.apply({"voice": {"rumble": False}}).startswith("not applied"))
        self.assertEqual(self.file.read_text(), before)  # still the old state: the next apply sees the change
        self.ran, self.fails = [], ()
        self.assertEqual(self.apply({"voice": {"rumble": False}}), "live")
        self.assertEqual(calls(self.ran), [SET])
        self.assertEqual(json.loads(self.file.read_text()), config({"voice": {"rumble": False}}))

    def test_stage_switched_off_during_a_pw_dump_blip_is_recorded_but_never_applied(self):
        """pw-dump failed, or listed no Booth Mic input: no set-param ran, so nothing is recorded; the next apply sends
        it. The same for a stage forced dry because its plugin stopped verifying."""
        off = {"voice": {"noise": False}}
        with mock.patch.object(voice, "verified", lambda: FOUND):
            self.apply()
            for blip in ("", json.dumps([node(70, "booth_mic")])):
                self.ran = []
                said = voice.apply(off, None, run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: blip,
                                   file=self.file)
                self.assertTrue(said.startswith("not applied"), said)
                self.assertEqual((self.ran, json.loads(self.file.read_text())), ([], config(ON, FOUND)), blip)
            self.ran = []
            self.assertEqual(self.apply(off, None), "live")
            self.assertEqual(calls(self.ran), [SET])
        with mock.patch.object(voice, "verified", lambda: {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}):
            self.assertTrue(voice.apply(off, None, run=lambda cmd: True, dump=lambda: "",
                                        file=self.file).startswith("not applied"))  # leveling forced dry: not sent
            self.assertEqual(json.loads(self.file.read_text()), config(off, FOUND))

    def test_a_live_change_that_cant_be_saved_is_retried(self):
        self.apply()
        self.ran.clear()
        with mock.patch.object(voice.store, "save", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.apply({"voice": {"rumble": False}}), "not saved: No space left on device")
        self.ran.clear()
        self.apply({"voice": {"rumble": False}})
        self.assertEqual(calls(self.ran), [SET])

    def test_a_corrupt_config_is_replaced_and_restarted(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{ half")
        self.apply()
        self.assertEqual(self.ran, [RESTART])

    def test_a_config_of_the_wrong_shape_is_replaced_and_restarted(self):
        self.file.parent.mkdir(parents=True)
        for text in ("null", "[1]", '{"context.modules": 3}', '{"context.modules": ["x"]}',
                     '{"context.modules": [{"args": {"filter.graph": {"nodes": ["x"]}}}]}'):
            self.file.write_text(text)
            self.ran.clear()
            self.assertEqual(self.apply(), "restarted", text)
            self.assertEqual((self.ran, json.loads(self.file.read_text())), ([RESTART], config()), text)

    def test_a_config_that_cant_be_written_never_restarts_the_chain(self):
        self.file.parent.mkdir(parents=True)
        self.file.mkdir()  # a folder where the file goes: the write fails
        self.assertIn("not saved", self.apply())
        self.assertNotIn(RESTART, self.ran)

    def test_it_only_ever_runs_systemctl_on_booth_mics_two_units(self):
        self.apply()
        self.apply({"voice": {"enabled": False}})
        self.apply(state=plugged(scarlett.mic(CTRLS, DUMP, "Instrument")))
        self.apply(state=rerouted("PCM 1"))
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
        self.assertEqual(json.loads(self.file.with_name("voice-select.conf").read_text()), selector())

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
        self.assertEqual(voice.describe(None, source=voice.BRIO_MIC),
                         "Booth Mic runs from the BRIO mic: the Scarlett isn't connected")
        self.assertEqual(voice.describe(None, running=False, source=voice.BRIO_MIC), voice.STOPPED)
        self.assertEqual(voice.describe(None, source=voice.SCARLETT), voice.WAITING)
        # its backup selector isn't up (heal's backoff, a failed start): Booth Mic is silent, never "runs from"
        self.assertEqual(voice.describe(None, selecting=False, source=voice.BRIO_MIC), voice.WAITING)

    def test_a_selector_that_wont_stop_is_never_shown_as_no_input(self):
        said = "not stopped: Booth Mic's source still takes the mic's old channel"
        self.assertEqual(voice.describe(rerouted("PCM 1"), True, said), voice.STALE)
        self.assertIn("old channel", voice.STALE)
        self.assertEqual(voice.describe(rerouted("PCM 1"), True, "unchanged"), voice.NOT_ROUTED)

    def test_skeptic_a_channel_moved_while_booth_mic_is_stopped_starts_on_the_old_channel(self):
        """XLR moved to capture 1, the computer's playback now on capture 2, while booth-voice was stopped. sync()
        (Booth's start) applies, then starts both units: the selector must start on capture 1."""
        voice.sync(ON, read=lambda c: {"ctrls": CTRLS, "mic": MIC}, run=lambda c: True, dump=lambda: CHAIN_DUMP,
                   file=self.file)
        moved, at_start = xlr_on_channel_1(), []

        def run(cmd):
            if cmd == START:
                at_start.append(json.loads(self.file.with_name("voice-select.conf").read_text()))
            return cmd != ACTIVE  # booth-voice stopped
        voice.sync(ON, read=lambda c: moved, run=run, dump=lambda: CHAIN_DUMP, file=self.file)
        self.assertEqual(at_start, [selector(moved["mic"])],
                         "the selector was started on capture 2, which now carries the computer's playback")

    def test_the_config_lives_beside_the_settings(self):
        from booth import store
        self.assertEqual(voice.path(), store.path().parent / "voice.conf")


class Heal(unittest.TestCase):
    """Every 10 s, with or without a window: the Scarlett read now, Booth Mic's configs brought up to date, and the
    selector restarted when it unloaded itself (its Scarlett went, or wasn't there yet at login) and the Scarlett and
    Booth Mic's input are back."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.select = self.file.with_name("voice-select.conf")
        self.ran = []
        voice._HEALED.clear()
        self.addCleanup(voice._HEALED.clear)

    def heal(self, dump, state=STATE, t=1000.0, settings=ON, fails=()):
        return voice.heal(settings, read=lambda choice: state,
                          run=lambda cmd: self.ran.append(cmd) or not set(cmd[:3]) & set(fails),
                          dump=lambda: dump, file=self.file, clock=lambda: t)

    def saved(self):
        voice.apply(ON, STATE, run=lambda cmd: True, dump=lambda: "[]", file=self.file)

    def test_the_scarlett_back_and_no_selector_restarts_the_selector(self):
        self.saved()
        self.assertTrue(self.heal(SCARLETT_BACK))
        self.assertEqual(self.ran, [RESTART_SELECT])

    def test_a_running_selector_or_no_scarlett_restarts_nothing(self):
        self.saved()
        for dump in (SELECTING, CHAIN_DUMP, "[]", "not json"):
            self.assertFalse(self.heal(dump), dump)
        self.assertEqual(self.ran, [])

    def test_unplugged_restarts_nothing_and_keeps_the_selector_config(self):
        self.saved()
        before = self.select.read_text()
        self.assertFalse(self.heal(SCARLETT_BACK, state=None))
        self.assertEqual((self.ran, self.select.read_text()), ([], before))

    def test_heal_follows_a_reroute_made_with_only_the_background_booth_running(self):
        self.saved()
        for tick in range(3):  # capture 2 now carries the computer's playback: stopped, never revived
            self.assertFalse(self.heal(SCARLETT_BACK, state=rerouted("PCM 1")))
        self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False))

    def test_heal_finds_the_scarlett_under_a_deduplicated_or_new_profile_name(self):
        self.saved()
        for name, position in ((MIC["node"] + ".2", "FR"),  # a fast replug: WirePlumber 0.4 dedups the name
                               (MIC["node"].replace("analog-surround-40", "pro-input-0"), "AUX1")):  # a profile switch
            back = json.dumps([node(71, "booth.mic.input"), node(41, name)])
            self.assertTrue(self.heal(back, state=plugged(dict(MIC, node=name, position=position))), name)
            sel = module(json.loads(self.select.read_text()), "libpipewire-module-loopback")["capture.props"]
            self.assertEqual((sel["target.object"], sel["audio.position"]), (name, [position]))
        self.assertEqual(self.ran, MOVE * 2)

    def test_heal_follows_a_profile_switch_once_even_with_the_old_selector_still_up(self):
        self.saved()
        name = MIC["node"].replace("analog-surround-40", "pro-input-0")
        up = json.dumps([node(71, "booth.mic.input"), node(41, name), node(80, "booth.mic.select")])
        self.assertTrue(self.heal(up, state=plugged(dict(MIC, node=name, position="AUX1"))))
        self.assertEqual(self.ran, MOVE)
        self.assertFalse(self.heal(json.dumps([node(71, "booth.mic.input"), node(41, name)]),
                                   state=plugged(dict(MIC, node=name, position="AUX1")), t=1001.0))
        self.assertEqual(self.ran, MOVE)  # just restarted: not again while it comes up

    def test_heal_never_restarts_the_selector_without_booth_mics_input(self):
        self.saved()  # booth-voice.service down: restarting the selector would pull it back up (BindsTo)
        for tick in range(6):
            self.assertFalse(self.heal(json.dumps([node(40, MIC["node"])]), t=1000.0 + 10 * tick))
        self.assertEqual(self.ran, [])

    def test_a_selector_that_wont_stay_up_is_restarted_at_most_once_a_minute(self):
        self.saved()
        ticks = [self.heal(SCARLETT_BACK, t=1000.0 + 10 * tick) for tick in range(7)]  # it never appears
        self.assertEqual(ticks, [True, False, False, False, False, False, True])
        replugged = json.dumps([node(71, "booth.mic.input"), node(41, MIC["node"])])  # a new node id: a replug
        self.assertTrue(self.heal(replugged, t=1061.0))
        self.assertEqual(self.ran, [RESTART_SELECT] * 3)

    def test_an_unsaved_chain_change_never_holds_back_reviving_the_selector(self):
        self.saved()
        with mock.patch.object(voice.store, "save", chain_unsaved):
            self.assertTrue(self.heal(SCARLETT_BACK, settings={"voice": {"rumble": False}}))
        self.assertEqual(self.ran[-1], RESTART_SELECT)

    def test_a_new_channel_with_an_unsaved_chain_change_restarts_the_selector_once(self):
        self.saved()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        with mock.patch.object(voice.store, "save", chain_unsaved):
            self.assertTrue(self.heal(SCARLETT_BACK, state=instrument, settings={"voice": {"rumble": False}}))
        self.assertEqual([c for c in self.ran if c[0] == "systemctl"], MOVE)  # apply's: coming up

    def test_a_failed_selector_move_is_never_revived_on_the_old_channel(self):
        self.saved()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        self.assertFalse(self.heal(SCARLETT_BACK, state=instrument, fails=("restart",)))
        self.assertEqual(self.ran, MOVE)  # apply's try, its config removed: the next tick tries the move again
        self.assertFalse(self.select.exists())  # never the old channel back

    def test_a_replug_that_reuses_the_node_id_restarts_the_selector_at_once(self):
        self.saved()
        before = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"], serial=500)])
        after = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"], serial=530)])  # same id, a new node
        self.assertTrue(self.heal(before, t=1000.0))
        self.assertFalse(self.heal(before, t=1010.0))  # the same node failing again: waits out the minute
        self.assertTrue(self.heal(after, t=1020.0))
        self.assertEqual(self.ran, [RESTART_SELECT] * 2)

    def test_heal_moves_the_selector_and_restarts_it_at_once(self):
        self.saved()
        name = MIC["node"].replace("analog-surround-40", "pro-input-0")
        moved = plugged(dict(MIC, node=name, position="AUX1"))
        self.assertTrue(self.heal(json.dumps([node(41, name)]), state=moved))  # apply's restart: never left saved only
        self.assertEqual(self.ran, MOVE)
        self.assertEqual(module(json.loads(self.select.read_text()), "libpipewire-module-loopback")
                         ["capture.props"]["target.object"], name)

    def test_a_pw_dump_blip_never_leaves_the_running_selector_on_the_old_channel(self):
        """Skeptic repro: the selector runs on FR; the XLR is rerouted to FL (FR now carries the computer's playback);
        apply's pw-dump comes back empty. The new channel is never recorded while the running selector stays on FR."""
        self.saved()

        def position():
            return module(json.loads(self.select.read_text()), "libpipewire-module-loopback")["capture.props"][
                "audio.position"] if self.select.exists() else None

        running, calls = [position()], []
        up = json.dumps([node(71, "booth.mic.input"), node(40, MIC["node"]), node(80, "booth.mic.select")])

        def dump():
            calls.append(1)
            return "" if len(calls) % 3 == 1 else up  # the first one apply asks for, then every third, blips

        def run(cmd):
            self.ran.append(cmd)
            if cmd == RESTART_SELECT:
                running[0] = position()
            return True

        self.assertEqual(running, [["FR"]])
        for tick in range(20):
            voice.heal(ON, read=lambda choice: xlr_on_channel_1(), run=run, dump=dump, file=self.file,
                       clock=lambda: 1000.0 + 10 * tick)
            self.assertFalse(position() == ["FL"] and running[0] == ["FR"], f"tick {tick}: saved FL, running FR")
        self.assertEqual((position(), running[0]), (["FL"], ["FL"]))

    def test_a_selector_the_page_just_restarted_is_left_to_come_up(self):
        self.saved()
        instrument = plugged(scarlett.mic(CTRLS, DUMP, "Instrument"))
        with mock.patch.object(voice.time, "monotonic", return_value=1000.0):  # the Microphone page's poll moves the mic
            said = voice.apply(ON, instrument, run=lambda cmd: self.ran.append(cmd) or True,
                               dump=lambda: SCARLETT_BACK, file=self.file)
        self.assertEqual(said, "selector restarted")
        self.assertFalse(self.heal(SCARLETT_BACK, state=instrument, t=1010.0))  # its node isn't there yet
        self.assertEqual(self.ran, MOVE)  # one dropout per mic move, never two

    def test_reroute_seen_while_booth_mic_is_down_starts_it_on_the_computers_playback(self):
        """booth-voice stopped, activating or auto-restarting when heal sees the XLR moved to capture 1, the computer's
        playback now on capture 2: whatever starts it next (a reboot, WantedBy, the user) must load capture 1."""
        self.heal(SELECTING)
        moved, down = xlr_on_channel_1(), json.dumps([node(40, MIC["node"])])  # no Booth Mic input
        self.ran.clear()
        self.assertFalse(self.heal(down, state=moved, fails=("is-active",)))
        self.assertEqual(json.loads(self.select.read_text()), selector(moved["mic"]))
        self.assertEqual(self.ran, [ACTIVE, STOP_SELECT])  # nothing started: BindsTo would start booth-voice

    def test_heal_takes_the_chosen_input(self):
        self.saved()
        settings = {"voice": {}, "microphone": {"input": "Instrument"}}
        voice.heal(settings, read=lambda choice: plugged(scarlett.mic(CTRLS, DUMP, choice)),
                   run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: SCARLETT_BACK, file=self.file,
                   clock=lambda: 1000.0)
        sel = module(json.loads(self.select.read_text()), "libpipewire-module-loopback")["capture.props"]
        self.assertEqual(sel["audio.position"], ["FL"])  # the Instrument's channel 1, never the XLR's FR

    def test_running_reads_booth_mics_input_and_a_failed_pw_dump_never_says_stopped(self):
        self.assertTrue(voice.running(CHAIN_DUMP))
        self.assertFalse(voice.running(json.dumps([node(70, "booth_mic"), node(40, MIC["node"])])))  # no input
        self.assertTrue(voice.running(""))  # pw-dump failed: unknown

    def test_selecting_reads_the_selector_node_by_name(self):
        self.assertEqual((voice.selecting(SELECTING), voice.selecting(SCARLETT_BACK), voice.selecting("")),
                         (True, False, False))


MUTED = {"voice": {"muted": True}}
# booth_mic muted live (GNOME's Ctrl+Alt+M, Booth closed: nothing recorded it), and its input
LIVE_MUTED = json.dumps([{"id": 70, "type": "PipeWire:Interface:Node",
                          "info": {"props": {"node.name": "booth_mic"}, "params": {"Props": [{"mute": True}]}}},
                         node(71, "booth.mic.input")])


class Muted(unittest.TestCase):
    """#21: muted, Booth Mic's chain carries silence (its master mixer, both gains 0) from its first sample, so a Booth
    Mic recreated by a booth-voice restart is never heard before Booth puts its node mute back; and Booth never restarts
    a running Booth Mic while muted, since a call falls back to the raw mic during the gap."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.ran = []

    def apply(self, settings, dump=CHAIN_DUMP, state=None):
        return voice.apply(settings, state, run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: dump,
                           file=self.file)

    def test_muted_booth_mic_carries_silence_whatever_the_switches(self):
        for v in ({}, {"enabled": False}, {"rumble": False}, {"enabled": False, "rumble": False}):
            self.assertEqual(flow(config({"voice": dict(v, muted=True)})), {}, v)
            self.assertNotEqual(flow(config({"voice": dict(v, muted=False)})), {}, v)

    def test_mute_changes_only_control_values_so_it_is_live_never_a_restart(self):
        self.assertEqual(voice.shape(config(MUTED)), voice.shape(config()))
        self.apply(ON)
        self.ran.clear()
        self.apply(MUTED)
        self.assertEqual(calls(self.ran), [SET])
        pairs = sent(self.ran)
        self.assertEqual((pairs["master:Gain 1"], pairs["master:Gain 2"]), (0.0, 0.0))

    def test_the_pre_start_writes_a_silent_chain_while_muted(self):
        with mock.patch.object(voice.store, "load", lambda **_: MUTED):
            self.assertIsNone(voice.write(self.file))
        self.assertEqual(flow(json.loads(self.file.read_text())), {})

    def old_shape(self):
        old = config(MUTED)
        module(old, "libpipewire-module-filter-chain")["filter.graph"]["nodes"].pop(1)  # an older chain's shape
        self.file.write_text(json.dumps(old))
        return self.file.read_text()

    def test_a_new_shape_never_restarts_a_running_booth_mic_while_muted(self):
        before = self.old_shape()
        said = self.apply(MUTED)
        self.assertEqual(said, voice.HELD)  # not recorded: the first apply after unmuting restarts it
        self.assertNotIn(RESTART, self.ran)
        self.assertEqual(self.file.read_text(), before)
        self.ran.clear()
        self.assertEqual(self.apply(ON), "restarted")
        self.assertEqual(self.ran, [RESTART])

    def test_a_booth_mic_that_isnt_running_is_still_started_while_muted(self):  # no gap to fall back in
        self.old_shape()
        self.assertEqual(self.apply(MUTED, dump=json.dumps([node(71, voice.INPUT)])), "restarted")
        self.assertEqual(self.ran, [RESTART])

    def test_a_held_restart_still_moves_the_selector(self):  # only the selector restarts: Booth Mic and its mute stay
        self.old_shape()
        self.assertIn("not restarted", self.apply(MUTED, state=STATE))
        self.assertEqual(calls(self.ran), [SET, ACTIVE[:3], RESTART_SELECT[:3]])
        self.assertEqual(json.loads(self.file.with_name(voice.SELECT_CONF).read_text()), selector())

    def test_reload_never_restarts_a_running_booth_mic_while_muted(self):
        changed = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        self.file.write_text(voice.build(MUTED, changed))
        for settings, dump, want in ((MUTED, CHAIN_DUMP, []), (MUTED, "[]", [RESTART]), (ON, CHAIN_DUMP, [RESTART])):
            self.ran.clear()
            with mock.patch.object(voice, "verified", lambda: FOUND), \
                    mock.patch.object(voice.store, "load", lambda **_: settings), mock.patch.object(voice, "_dump", lambda: dump):
                got = voice.reload(run=lambda cmd: self.ran.append(cmd) or True, file=self.file)
            self.assertEqual(got, True if want else voice.HELD_RELOAD, (settings, dump))
            self.assertEqual(self.ran, want, (settings, dump))

    def test_skeptic_a_failed_pw_dump_never_restarts_a_running_booth_mic_while_muted(self):  # fail closed
        self.old_shape()
        for blip in ("", "not json", "{}"):  # devices._run's "" on a timeout or OSError; anything but a list
            self.ran.clear()
            calls = iter([blip] + [CHAIN_DUMP] * 20)  # _held's read fails; Booth Mic IS running
            said = voice.apply(MUTED, None, run=lambda c: self.ran.append(c) or True, dump=lambda: next(calls),
                               file=self.file)
            self.assertNotIn(RESTART, self.ran, f"{blip!r}: {said}")

    def test_skeptic_reload_never_restarts_while_muted_on_a_pw_dump_blip(self):
        self.file.write_text(voice.build(MUTED, {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}))
        with mock.patch.object(voice, "verified", lambda: FOUND), \
                mock.patch.object(voice.store, "load", lambda **_: MUTED), mock.patch.object(voice, "_dump", lambda: ""):
            self.assertEqual(voice.reload(run=lambda c: self.ran.append(c) or True, file=self.file), voice.HELD_RELOAD)
        self.assertEqual(self.ran, [])

    def test_skeptic_a_held_restart_still_silences_the_running_chain_live(self):
        old = config(ON)  # running unmuted, a shape change pending (an earlier restart failed)
        module(old, "libpipewire-module-filter-chain")["filter.graph"]["nodes"].pop(1)
        self.file.write_text(json.dumps(old))
        self.assertEqual(self.apply(MUTED), voice.HELD)  # only the restart waits: not recorded, so it's retried
        self.assertEqual(calls(self.ran), [SET])
        pairs = sent(self.ran)
        self.assertEqual((pairs["master:Gain 1"], pairs["master:Gain 2"]), (0.0, 0.0))
        self.ran.clear()
        self.assertTrue(voice.apply(MUTED, None, run=lambda c: self.ran.append(c) or c[0] != "pw-cli",
                                    dump=lambda: CHAIN_DUMP, file=self.file).startswith("not applied"))  # said
        self.assertNotIn(RESTART, self.ran)

    def test_skeptic_a_kept_live_run_conf_while_muted_is_said_as_a_mismatch(self):  # enforce mutes its node
        run = self.file.with_name(voice.RUN_CONF)
        run.write_text(voice.build(ON, {}))  # last written while unmuted
        with mock.patch.object(voice.store, "load", lambda **_: MUTED), \
                mock.patch.object(voice, "verified", lambda w=None: {}), \
                mock.patch.object(voice, "_save", lambda cfg, f: "not saved: No space left on device"):
            said = voice.write(run)
        self.assertEqual(said, "kept the last config, not saved: No space left on device; it isn't silent though "
                               "Booth Mic is muted: booth.mute --enforce mutes its node")
        run.write_text(voice.build({"voice": {"enabled": False}}, {}))  # older values, unmuted record: no mismatch
        with mock.patch.object(voice.store, "load", lambda **_: ON), mock.patch.object(voice, "verified", lambda w=None: {}), \
                mock.patch.object(voice, "_save", lambda cfg, f: "not saved: No space left on device"):
            self.assertEqual(voice.write(run), "kept the last config, not saved: No space left on device")
        run.write_text(voice.build({"voice": {"muted": True, "rumble": False}}, {}))  # older values, silent: no mismatch
        with mock.patch.object(voice.store, "load", lambda **_: MUTED), \
                mock.patch.object(voice, "verified", lambda w=None: {}), \
                mock.patch.object(voice, "_save", lambda cfg, f: "not saved: No space left on device"):
            self.assertEqual(voice.write(run), "kept the last config, not saved: No space left on device")

    def test_w5_a_live_node_mute_with_no_record_still_holds_a_restart(self):  # #21 W5 final
        self.old_shape()
        self.assertEqual(self.apply(ON, dump=LIVE_MUTED), voice.HELD)
        self.assertNotIn(RESTART, self.ran)

    def test_w5_reload_holds_on_a_live_node_mute_and_records_it(self):
        self.file.write_text(voice.build(ON, {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}))
        saved = []
        with mock.patch.object(voice, "verified", lambda: FOUND), mock.patch.object(voice.store, "save", saved.append), \
                mock.patch.object(voice.store, "load", lambda **_: {"voice": {}}), \
                mock.patch.object(voice, "_dump", lambda: LIVE_MUTED):
            self.assertEqual(voice.reload(run=lambda c: self.ran.append(c) or True, file=self.file), voice.HELD_RELOAD)
        self.assertEqual(self.ran, [])
        self.assertEqual(saved, [{"voice": {"muted": True}}])  # a later PipeWire restart's enforce re-mutes it

    def test_w5_the_pre_start_records_a_live_node_mute_and_writes_a_silent_chain(self):
        saved = []
        with mock.patch.object(voice.store, "load", lambda **_: {"voice": {}}), \
                mock.patch.object(voice.store, "save", lambda s, f=None: saved.append(s) if f is None else real_save(s, f)), \
                mock.patch.object(voice, "_dump", lambda: LIVE_MUTED):
            self.assertIsNone(voice.write(self.file))
        self.assertEqual(flow(json.loads(self.file.read_text())), {})
        self.assertEqual(saved, [{"voice": {"muted": True}}])

    def test_w5_install_sync_records_a_live_node_mute(self):
        saved = []
        with mock.patch.object(voice.store, "load", lambda **_: {"voice": {}}), \
                mock.patch.object(voice.store, "save", saved.append), mock.patch.object(voice, "_dump", lambda: LIVE_MUTED), \
                mock.patch.object(voice, "sync", lambda settings: "synced"), mock.patch.object(voice.sys, "argv", ["x"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(voice.main(), 0)
        self.assertEqual(saved, [{"voice": {"muted": True}}])

    def test_w5_a_held_shape_change_sends_only_what_the_running_chain_has_once(self):  # #21 W5 final item 3
        before = self.old_shape()  # rumble's filter isn't in the running chain: its controls can never land
        has = {f"{n['name']}:{k}" for n in voice._graph_nodes(json.loads(before)) for k in n.get("control", {})}
        props = {}

        def run(cmd):
            self.ran.append(cmd)
            props.update((k, v) for k, v in itertools.batched(json.loads(cmd[4])["params"], 2) if k in has)
            return True
        said = REAL_APPLY(MUTED, None, run=run, dump=lambda: with_props(CHAIN_DUMP, props), file=self.file)
        self.assertEqual(said, voice.HELD)  # never "didn't take rumble:Freq": the mute landed
        self.assertEqual((props["master:Gain 1"], props["master:Gain 2"]), (0.0, 0.0))
        self.assertEqual(set(sent(self.ran)), has)  # only what it has, each call once
        self.assertEqual(len(self.ran), -(-len(has) // voice.CHUNK) + 1)

    def test_w5_a_held_chain_whose_run_conf_cant_be_read_still_gets_the_mute(self):
        self.old_shape()
        self.file.with_name(voice.RUN_CONF).write_text("not json")
        self.assertEqual(self.apply(MUTED), voice.HELD)
        self.assertEqual(sent(self.ran), {"master:Gain 1": 0.0, "master:Gain 2": 0.0})

    def test_w5_an_unreadable_settings_file_keeps_the_last_run_conf_never_defaults(self):  # #17 W5 final
        run = self.file.with_name(voice.RUN_CONF)
        last = voice.build(MUTED, {})
        run.write_text(last)
        eio = mock.Mock(side_effect=OSError(5, "Input/output error"))
        with mock.patch.object(voice.store, "load", eio), mock.patch.object(voice, "verified", lambda w=None: {}):
            self.assertEqual(voice.write(run), "kept the last config, not read: the settings (Input/output error)")
            self.assertEqual(run.read_text(), last)
            run.unlink()
            self.assertEqual(voice.write(run), "not read: the settings (Input/output error)")  # the unit doesn't start
        self.assertFalse(run.exists())

    def test_install_says_a_muted_booth_mic_loads_its_new_plugins_at_its_next_start(self):  # never "couldn't restart"
        err = io.StringIO()
        with mock.patch.object(voice, "reload", lambda: voice.HELD_RELOAD), \
                mock.patch.object(voice.sys, "argv", ["x", "--reload"]), contextlib.redirect_stderr(err):
            self.assertEqual(voice.main(), 0)  # install.sh's "couldn't restart Booth Mic" is for a failure only
        self.assertEqual(err.getvalue(),
                         "install.sh: Booth Mic is muted: its new plugins load the next time Booth Mic starts\n")


TONE_PATH = "tone_air(tone_presence(tone_warmth({})))"


class Tone(unittest.TestCase):
    """#17's Tone stage: Warmth, Presence and Air as builtin biquads (no plugin) between noise and leveling, with its
    own live dry/wet bypass; every knob a control value, so never a restart."""

    def tone_nodes(self, cfg):
        nodes = module(cfg, "libpipewire-module-filter-chain")["filter.graph"]["nodes"]
        return {n["name"]: n for n in nodes if n["name"].startswith("tone_") and n["name"] != "tone_mix"}

    def test_tone_sits_between_noise_and_leveling(self):
        self.assertEqual(flow(config({"voice": {"tone": True}}, FOUND)),
                         {f"limiter(leveling({TONE_PATH.format('noise(rumble(in))')}))": 1.0})

    def test_tone_needs_no_plugin_so_it_is_always_in_the_graph(self):
        self.assertEqual(flow(config({"voice": {"tone": True}}, {})), {TONE_PATH.format("rumble(in)"): 1.0})

    def test_tone_off_or_absent_passes_dry_so_an_upgrade_never_changes_the_sound(self):
        for v in ({}, {"tone": False}, {"tone": False, "tone_warmth": 6.0}):
            self.assertEqual(flow(config({"voice": v}, FOUND)), {"limiter(leveling(noise(rumble(in))))": 1.0}, v)

    def test_the_bands_are_builtin_shelves_and_a_peak_with_the_exact_control_names(self):
        nodes = self.tone_nodes(config({"voice": {"tone": True}}))
        self.assertEqual({k: (n["type"], n["label"]) for k, n in nodes.items()},
                         {"tone_warmth": ("builtin", "bq_lowshelf"), "tone_presence": ("builtin", "bq_peaking"),
                          "tone_air": ("builtin", "bq_highshelf")})
        for n in nodes.values():  # PipeWire 1.0.5's builtin biquad controls, checked live on a test pipewire
            self.assertEqual(list(n["control"]), ["Freq", "Q", "Gain"])
            self.assertTrue(all(isinstance(v, float) for v in n["control"].values()))  # pw-cli reads 1 as an Int

    def test_band_maps_a_knob_in_db_to_its_biquad(self):
        self.assertEqual(voice.band("tone_warmth", 2), {"Freq": 150.0, "Q": 0.707, "Gain": 2.0})
        self.assertEqual(voice.band("tone_presence", -3.5), {"Freq": 4000.0, "Q": 1.0, "Gain": -3.5})
        self.assertEqual(voice.band("tone_air", 0), {"Freq": 10000.0, "Q": 0.707, "Gain": 0.0})
        self.assertIsInstance(voice.band("tone_air", 0)["Gain"], float)

    def test_the_knobs_reach_their_bands_and_absent_is_flat(self):
        nodes = self.tone_nodes(config({"voice": {"tone_warmth": 3, "tone_presence": -2.5}}))
        self.assertEqual({k: n["control"] for k, n in nodes.items()},
                         {"tone_warmth": voice.band("tone_warmth", 3), "tone_presence": voice.band("tone_presence", -2.5),
                          "tone_air": voice.band("tone_air", 0)})

    def test_tone_switch_and_knobs_never_change_the_shape(self):
        configs = [config({"voice": {"tone": on, "tone_warmth": g, "tone_presence": -g, "tone_air": g / 2}}, FOUND)
                   for on in (True, False) for g in (-12.0, 0.0, 12.0)]
        self.assertEqual(len({json.dumps(voice.shape(c), sort_keys=True) for c in configs}), 1)
        self.assertEqual(len({json.dumps(c, sort_keys=True) for c in configs}), len(configs))

    def test_a_tone_knob_goes_live_as_node_colon_control_on_the_input_node(self):
        cmds = voice.set_params_cmds(CHAIN_DUMP, config({"voice": {"tone": True, "tone_presence": 4.5}}, FOUND))
        self.assertEqual({tuple(c[:4]) for c in cmds}, {("pw-cli", "set-param", "71", "Props")})
        pairs = sent(cmds)
        self.assertEqual((pairs["tone_presence:Gain"], pairs["tone_presence:Freq"], pairs["tone_presence:Q"]),
                         (4.5, 4000.0, 1.0))
        self.assertEqual((pairs["tone_warmth:Gain"], pairs["tone_air:Gain"]), (0.0, 0.0))
        self.assertEqual((pairs["tone_mix:Gain 1"], pairs["tone_mix:Gain 2"]), (1.0, 0.0))

    def test_a_tone_knob_on_a_running_chain_is_live_never_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file, ran = Path(tmp.name) / "voice.conf", []
        voice.apply({"voice": {"tone": True}}, None, run=lambda c: ran.append(c) or True, dump=lambda: CHAIN_DUMP,
                    file=file)
        ran.clear()
        said = voice.apply({"voice": {"tone": True, "tone_air": 3.0}}, None, run=lambda c: ran.append(c) or True,
                           dump=lambda: CHAIN_DUMP, file=file)
        self.assertEqual((said, calls(ran)), ("live", [SET]))
        self.assertEqual(sent(ran)["tone_air:Gain"], 3.0)

    def test_the_services_pre_start_writes_the_saved_tone_values(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "voice-run.conf"
        settings = {"voice": {"tone": True, "tone_warmth": -4.0, "tone_air": 2.5}}
        with mock.patch.object(voice.store, "load", lambda **_: settings):
            self.assertIsNone(voice.write(file))
        nodes = self.tone_nodes(json.loads(file.read_text()))
        self.assertEqual((nodes["tone_warmth"]["control"]["Gain"], nodes["tone_air"]["control"]["Gain"]), (-4.0, 2.5))


# Every stage's switch and knob: what a preset must set, all of it
EVERY = {"rumble", "noise", "noise_strength", "tone", "tone_warmth", "tone_presence", "tone_air", "leveling",
         "leveling_amount", "limiter", "limiter_ceiling"}


class Presets(unittest.TestCase):
    """Natural, Broadcast and Noisy room: each sets every stage's switch and knob, in one live apply."""

    def test_the_preset_values(self):
        on = {"rumble": True, "noise": True, "leveling": True, "limiter": True, "limiter_ceiling": -1.0}
        self.assertEqual(voice.PRESETS, {
            "Natural": {**on, "noise_strength": 25, "tone": False, "tone_warmth": 0.0, "tone_presence": 0.0,
                        "tone_air": 0.0, "leveling_amount": 30},
            "Broadcast": {**on, "noise_strength": 50, "tone": True, "tone_warmth": 2.0, "tone_presence": 3.0,
                          "tone_air": 4.0, "leveling_amount": 70},
            "Noisy room": {**on, "noise_strength": 85, "tone": True, "tone_warmth": -3.0, "tone_presence": 3.0,
                           "tone_air": 0.0, "leveling_amount": 40}})

    def test_every_preset_sets_every_stage_and_never_the_master_or_monitoring(self):
        for name, p in voice.PRESETS.items():
            self.assertEqual(set(p), EVERY, name)

    def test_every_preset_is_a_valid_saved_setting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "settings.json"
        for name, p in voice.PRESETS.items():
            voice.store.save({"voice": p}, file)
            self.assertEqual(voice.store.load(file)["voice"], p, name)

    def test_the_default_is_broadcast_for_the_users_dynamic_mic(self):  # #2: a Shure Beta 57A/58A
        self.assertEqual(voice.DEFAULT_PRESET, "Broadcast")
        self.assertEqual(voice.store.defaults()["voice"], voice.PRESETS["Broadcast"])
        self.assertIsNot(voice.store.defaults()["voice"], voice.PRESETS["Broadcast"])  # a fresh copy

    def test_an_existing_users_saved_values_are_never_replaced_by_the_default(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "settings.json"
        file.write_text('{"version": 1, "voice": {"noise_strength": 70}}')
        self.assertEqual(voice.store.load(file)["voice"], {"noise_strength": 70})

    def test_the_page_names_the_preset_the_values_match_and_none_after_a_knob_moved(self):
        for name, p in voice.PRESETS.items():
            self.assertEqual(voice.preset_of(dict(p)), name)
            self.assertEqual(voice.preset_of(dict(p, monitor=True, enabled=False)), name)  # not a stage
            for k in p:
                moved = dict(p, **{k: (not p[k]) if isinstance(p[k], bool) else p[k] + 1})
                self.assertIsNone(voice.preset_of(moved), (name, k))
        self.assertIsNone(voice.preset_of({}))  # an upgrade's absent values: the old defaults, no preset
        self.assertEqual(voice.preset_of({k: v for k, v in voice.PRESETS["Broadcast"].items() if k != "rumble"}),
                         "Broadcast")  # absent: its default, on

    def test_a_preset_is_one_live_change_with_every_stage_never_a_restart(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file, ran = Path(tmp.name) / "voice.conf", []
        run = lambda c: ran.append(c) or True  # noqa: E731
        with mock.patch.object(voice, "verified", lambda where=None: FOUND):
            voice.apply({"voice": dict(voice.PRESETS["Natural"])}, None, run=run, dump=lambda: CHAIN_DUMP, file=file)
            ran.clear()
            said = voice.apply({"voice": dict(voice.PRESETS["Noisy room"])}, None, run=run, dump=lambda: CHAIN_DUMP,
                               file=file)
        self.assertEqual((said, calls(ran)), ("live", [SET]))
        pairs = sent(ran)
        self.assertEqual((pairs["noise:VAD Threshold (%)"], pairs["tone_mix:Gain 1"], pairs["tone_warmth:Gain"],
                          pairs["leveling:Threshold level (dB)"]), (81.0, 1.0, -3.0, -12.0))


class Readback(unittest.TestCase):
    """PipeWire 1.0.5's pw-cli sends an empty Props above ~25 controls (prints `Object: size 8`) and still exits 0, so
    nothing lands: every live change goes in calls of at most 12 controls, master's gains alone and first when they
    silence the chain and last when they don't, and counts only once pw-dump reads every control back."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        landing(ON, None, run=lambda c: True, dump=lambda: CHAIN_DUMP, file=self.file)  # Booth Mic runs, recorded
        self.ran, self.props = [], {}

    def chain(self, drop=lambda cmd, n: False, store=lambda v: v):
        """A fake chain whose pw-cli always exits 0: a call lands unless `drop(cmd, how many times it was sent)`."""
        def run(cmd):
            self.ran.append(cmd)
            if not drop(cmd, self.ran.count(cmd)):
                self.props.update((k, store(v)) for k, v in itertools.batched(json.loads(cmd[4])["params"], 2))
            return True
        return dict(run=run, dump=lambda: with_props(CHAIN_DUMP, self.props), file=self.file)

    def test_no_call_carries_more_than_twelve_controls_even_for_the_largest_chain(self):
        for muted in (False, True):
            cfg = config({"voice": {"tone": True, "muted": muted}}, FOUND)
            total = sum(len(n.get("control", {})) for n in voice._graph_nodes(cfg))
            cmds = voice.set_params_cmds(CHAIN_DUMP, cfg)
            self.assertGreater(total, 25)  # one call of these sent nothing, live
            self.assertLessEqual(max(len(json.loads(c[4])["params"]) // 2 for c in cmds), 12, muted)
            self.assertEqual(len(sent(cmds)), total)

    def test_a_mute_sends_master_alone_and_first_anything_else_sends_it_last(self):
        muting = voice.set_params_cmds(CHAIN_DUMP, config({"voice": {"tone": True, "muted": True}}, FOUND))
        self.assertEqual(json.loads(muting[0][4])["params"], ["master:Gain 1", 0.0, "master:Gain 2", 0.0])
        self.assertNotIn("master:", "".join(c[4] for c in muting[1:]))
        live = voice.set_params_cmds(CHAIN_DUMP, config({"voice": {"tone": True}}, FOUND))
        self.assertEqual(json.loads(live[-1][4])["params"][-4:], ["master:Gain 1", 1.0, "master:Gain 2", 0.0])
        self.assertNotIn("master:", "".join(c[4] for c in live[:-1]))

    def test_a_set_param_that_exits_0_but_never_lands_is_not_applied_and_not_recorded(self):  # fix-skeptic, #59
        before = self.file.read_text()
        said = REAL_APPLY(MUTED, None, run=lambda c: self.ran.append(c) or True, dump=lambda: CHAIN_DUMP, file=self.file)
        self.assertTrue(said.startswith("not applied: Booth Mic didn't take master:Gain 1, master:Gain 2, "), said)
        self.assertEqual(self.file.read_text(), before)  # the next apply sends it all again
        cmds = voice.set_params_cmds(CHAIN_DUMP, config(MUTED))
        self.assertEqual(self.ran, cmds + cmds)  # every call, then once more

    def test_a_call_that_never_lands_is_named_and_the_mute_before_it_still_landed(self):
        self.props.update(sent(voice.set_params_cmds(CHAIN_DUMP, config(ON))))  # the chain runs ON's values
        knob = {"voice": {"muted": True, "tone_air": 3.0}}
        said = REAL_APPLY(knob, None, **self.chain(drop=lambda cmd, n: "tone_air:Gain" in cmd[4]))
        dropped = next(c for c in voice.set_params_cmds(CHAIN_DUMP, config(knob)) if "tone_air:Gain" in c[4])
        self.assertGreater(len(sent([dropped])), 1)  # the rest of its call already held its values: never named
        self.assertEqual(said, "not applied: Booth Mic didn't take tone_air:Gain")
        self.assertEqual((self.props["master:Gain 1"], self.props["master:Gain 2"]), (0.0, 0.0))  # silent anyway
        self.assertEqual(self.ran.count(dropped), 2)
        self.assertEqual(json.loads(self.file.read_text()), config(ON))  # not recorded

    def test_a_call_that_lands_only_when_sent_again_is_live(self):
        said = REAL_APPLY(MUTED, None, **self.chain(drop=lambda cmd, n: "tone_air:Gain" in cmd[4] and n == 1))
        self.assertEqual(said, "live")
        self.assertEqual([c for c in self.ran if "tone_air:Gain" in c[4]].__len__(), 2)
        # only the missed call goes again: every other one, read back as landed, once
        self.assertEqual(len(self.ran), len(voice.set_params_cmds(CHAIN_DUMP, config(MUTED))) + 1)
        self.assertEqual(json.loads(self.file.read_text()), config(MUTED))

    def test_a_mute_whose_master_never_lands_is_not_applied_though_the_old_gains_are_close(self):  # #21 cnf59
        self.props.update(sent(voice.set_params_cmds(CHAIN_DUMP, config(ON))))  # running ON: master Gain 1 is 1.0
        said = REAL_APPLY(MUTED, None, **self.chain(drop=lambda cmd, n: "master:" in cmd[4]))
        self.assertEqual(said, "not applied: Booth Mic didn't take master:Gain 1")  # 1.0 stale, never 0.0's
        self.assertEqual(json.loads(self.file.read_text()), config(ON))  # not recorded

    def test_a_value_the_chain_stores_as_a_float32_has_landed(self):  # 0.707 reads back 0.7070000171661377
        f32 = lambda v: struct.unpack("f", struct.pack("f", v))[0]  # noqa: E731
        self.assertNotEqual(f32(0.707), 0.707)
        self.assertEqual(REAL_APPLY(MUTED, None, **self.chain(store=f32)), "live")
        self.assertEqual(len(self.ran), len(voice.set_params_cmds(CHAIN_DUMP, config(MUTED))))  # none sent again


class Throttle(unittest.TestCase):
    """Knob changes coalesce: at most one send per gap (about 20 a second), off the caller's thread, the latest value
    wins and the last change always lands."""

    def setUp(self):
        self.now, self.sent, self.workers, self.value = 100.0, [], [], 0
        self.t = voice.Throttle(self.send, self.workers.append, gap=0.05, clock=lambda: self.now, sleep=self.sleep)

    def sleep(self, s):
        self.assertGreaterEqual(s, 0)
        self.now += s
        self.sleeps = getattr(self, "sleeps", 0) + 1  # a worker that never clears `dirty` fails here, never hangs
        self.assertLess(self.sleeps, 1000, "the worker never stopped")

    def send(self):
        self.sent.append((round(self.now, 3), self.value))  # apply() reads the settings when it runs: the latest

    def change(self, value):
        self.value = value
        self.t.kick()

    def run_worker(self):
        self.workers.pop(0)()

    def test_changes_before_the_worker_runs_coalesce_into_one_send_of_the_latest(self):
        for v in (1, 2, 3):
            self.change(v)
        self.assertEqual((len(self.workers), self.sent), (1, []))  # nothing sent on the caller's thread
        self.run_worker()
        self.assertEqual(self.sent, [(100.0, 3)])

    def test_changes_while_sending_land_once_a_gap_later(self):
        def send():
            self.sent.append((round(self.now, 3), self.value))
            if len(self.sent) == 1:  # dragged on while pw-cli ran
                for v in (5, 6, 7):
                    self.change(v)
        self.t.send = send
        self.change(4)
        self.run_worker()
        self.assertEqual(self.sent, [(100.0, 4), (100.05, 7)])
        self.assertEqual(self.workers, [])  # one worker did it all

    def test_never_more_than_one_send_per_gap_and_the_last_value_always_lands(self):
        def send():
            self.sent.append((round(self.now, 3), self.value))
            self.now += 0.01  # pw-cli's own time
            if self.value < 40:
                self.change(self.value + 1)
        self.t.send = send
        self.change(1)
        self.run_worker()
        times = [t for t, _ in self.sent]
        self.assertTrue(all(b - a >= 0.05 - 1e-9 for a, b in zip(times, times[1:])), times)
        self.assertEqual(self.sent[-1][1], 40)

    def test_a_change_after_the_worker_finished_starts_one_that_still_waits_the_gap(self):
        self.change(1)
        self.run_worker()
        self.change(2)
        self.assertEqual(len(self.workers), 1)
        self.run_worker()
        self.assertEqual(self.sent, [(100.0, 1), (100.05, 2)])

    def test_a_send_that_raises_never_stops_later_changes(self):
        def send():
            raise OSError("pw-cli gone")
        self.t.send = send
        self.change(1)
        with self.assertRaises(OSError):
            self.run_worker()
        self.t.send = self.send
        self.change(2)
        self.run_worker()
        self.assertEqual(self.sent[-1][1], 2)

    def test_a_change_made_while_a_send_raised_still_lands_without_another_change(self):  # #17 aud58
        def send():
            self.change(2)  # the knob moved while pw-cli ran, then the send failed
            raise OSError("pw-cli gone")
        self.t.send = send
        self.change(1)
        with self.assertRaises(OSError):
            self.run_worker()
        self.t.send = self.send
        self.assertEqual(len(self.workers), 1)  # a new worker for it, with no next kick
        self.run_worker()
        self.assertEqual(self.sent, [(100.05, 2)])

    def test_the_default_gap_is_about_20_a_second(self):
        self.assertEqual(voice.Throttle(self.send, self.workers.append).gap, 0.05)


if __name__ == "__main__":
    unittest.main()
