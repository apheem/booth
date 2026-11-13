"""Booth Stereo (#19): a stereo source for recording apps, the processed voice on one side and a chosen source on the
other. Its own copy of Booth Mic's chain (same stages, settings and verified plugins), fed from booth.mic.input's
monitor (the selected mic, before processing and before the mute), in its own unit: Booth Mic, its selector and its
mute are never touched, and Booth Mic's mute never silences it."""
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from booth import mute, plugins, scarlett, stereo, store, voice
from tests.fixture import read
from tests.test_headset import DUMP

FOUND = {name: f"/home/u/.local/share/booth/plugins/{name}" for name in plugins.MANIFEST}
CTRLS = scarlett.parse_contents(read("scarlett/amixer-contents.txt"))  # XLR on capture channel 2, Instrument on 1
SCARLETT_IN = "alsa_input.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-surround-40"
SCARLETT_OUT = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.analog-stereo"  # the configured sink
USB_OUT = "alsa_output.usb-Generic_USB_Audio-00.iec958-stereo"
BRIO = "alsa_input.usb-046d_Logitech_BRIO_FAKESER1-03.analog-stereo"
RESTART = ["systemctl", "--user", "restart", "booth-stereo.service"]


def node(i, name, serial, cls="Stream/Input/Audio/Internal", description=None):
    props = {"node.name": name, "object.serial": serial, "media.class": cls}
    if description:
        props["node.description"] = description
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": props}}


MIC_INPUT = node(97, "booth.mic.input", 900, "Audio/Sink/Internal")
BOOTH_MIC = node(106, "booth_mic", 906, "Audio/Source", "Booth Mic")
UP = (node(140, "booth.stereo.input", 940, "Audio/Sink/Internal"),
      node(141, "booth_stereo", 941, "Audio/Source", "Booth Stereo — recording only, not for calls"),
      node(142, "booth.stereo.voice", 942), node(143, "booth.stereo.voice.out", 943, "Stream/Output/Audio/Internal"))
OTHER_UP = (node(144, "booth.stereo.other", 944), node(145, "booth.stereo.other.out", 945, "Stream/Output/Audio/Internal"))


def dump(*extra, props=None, base=DUMP):
    """The captured pw-dump (Scarlett, BRIO, PRO X, a USB card; or `base`) plus `extra` nodes; `props`:
    booth.stereo.voice's Props {node:control: value}, as a filter-chain reports them."""
    objects = json.loads(base) + [json.loads(json.dumps(o)) for o in extra]
    for o in objects:
        if props is not None and ((o.get("info") or {}).get("props") or {}).get("node.name") == stereo.VOICE:
            o["info"]["params"] = {"Props": [{"volume": 1.0}, {"params": [x for p in props.items() for x in p]}]}
    return json.dumps(objects)


def configured(sink):
    """The captured pw-dump with the configured default output `sink` (None: none configured, only the effective one)."""
    objects = json.loads(DUMP)
    for o in objects:
        for m in list(o.get("metadata") or []):
            if m["key"] == "default.configured.audio.sink":
                if sink is None:
                    o["metadata"].remove(m)
                else:
                    m["value"] = {"name": sink}
    return json.dumps(objects)


def settings(side="L", source=None, **v):
    v = dict(v, stereo_side=side)
    if source is not None:
        v["stereo_source"] = source
    return {"microphone": {}, "voice": v}


def modules(text):
    """A config's modules by kind: "source" (Booth Stereo's loopback), "chain" (the voice), "other" (the other side)."""
    found = {}
    for m in json.loads(text)["context.modules"][4:]:
        kind = {"libpipewire-module-filter-chain": "chain"}.get(m["name"]) or \
            {"booth.stereo.input": "source", "booth.stereo.other": "other"}[m["args"]["capture.props"]["node.name"]]
        found[kind] = m["args"]
    return found


def graph(text):
    return next(m["args"] for m in json.loads(text)["context.modules"]
                if m["name"] == "libpipewire-module-filter-chain")["filter.graph"]


SIDES = {"L": ("FL", "FR"), "R": ("FR", "FL")}
OTHERS = {"scarlett": {"node": SCARLETT_IN, "position": "FL"}, "computer": {"node": SCARLETT_OUT, "sink": True},
          "device": {"node": BRIO}, "missing": None}


class Build(unittest.TestCase):
    def test_each_side_and_source_combination(self):  # the AC: the builder's output for each one
        for side, (here, there) in SIDES.items():
            for kind, other in OTHERS.items():
                with self.subTest(side=side, source=kind):
                    m = modules(stereo.build(settings(side), FOUND, other))
                    source = m["source"]
                    self.assertEqual((source["audio.channels"], source["audio.position"]), (2, ["FL", "FR"]))
                    self.assertEqual(source["capture.props"]["media.class"], "Audio/Sink/Internal")
                    self.assertIs(source["capture.props"]["node.passive"], True)  # nothing runs until an app records
                    out = source["playback.props"]
                    self.assertEqual((out["node.name"], out["media.class"], out["node.description"]),
                                     ("booth_stereo", "Audio/Source", "Booth Stereo — recording only, not for calls"))
                    chain = m["chain"]
                    cap, play = chain["capture.props"], chain["playback.props"]
                    # the selected mic, before processing and before the mute: booth.mic.input's monitor
                    self.assertEqual((cap["target.object"], cap["stream.capture.sink"], cap["node.dont-reconnect"],
                                      cap["node.passive"], cap["media.class"]),
                                     ("booth.mic.input", True, True, True, "Stream/Input/Audio/Internal"))
                    self.assertEqual((play["target.object"], play["audio.position"], play["stream.dont-remix"],
                                      play["node.dont-reconnect"], play["node.passive"]),
                                     ("booth.stereo.input", [here], True, True, True))
                    if other is None:  # that side silent, never another source
                        self.assertNotIn("other", m)
                        continue
                    cap, play = m["other"]["capture.props"], m["other"]["playback.props"]
                    self.assertEqual((cap["target.object"], cap["node.dont-reconnect"], cap["node.passive"],
                                      cap["media.class"]), (other["node"], True, True, "Stream/Input/Audio/Internal"))
                    self.assertEqual((play["target.object"], play["audio.position"], play["stream.dont-remix"]),
                                     ("booth.stereo.input", [there], True))
                    self.assertEqual(cap.get("stream.capture.sink", False), kind == "computer")  # the sink's monitor
                    if kind == "scarlett":  # its one channel, by position, never a mixdown with the mic
                        self.assertEqual((cap["audio.channels"], cap["audio.position"], cap["stream.dont-remix"]),
                                         (1, ["FL"], True))
                    else:  # the whole source mixed down onto its side
                        self.assertEqual((cap["audio.channels"], cap["audio.position"]), (1, ["MONO"]))
                        self.assertNotIn("stream.dont-remix", cap)

    def test_the_voice_side_is_booth_mics_chain_with_the_same_settings_and_only_verified_plugins(self):
        s = settings(tone=True, tone_air=4.0, leveling_amount=70, limiter_ceiling=-3.0, enabled=False)
        self.assertEqual(graph(stereo.build(s, FOUND, None)), graph(voice.build(s, FOUND)))
        some = {k: v for k, v in FOUND.items() if k != "sc4m_1916.so"}
        self.assertEqual(graph(stereo.build(s, some, None)), graph(voice.build(s, some)))
        self.assertNotIn("sc4m", stereo.build(s, some, None))

    def test_booth_mics_mute_never_silences_booth_stereo(self):  # #21's AC
        for enabled in (True, False):
            master = next(n for n in graph(stereo.build(settings(muted=True, enabled=enabled), FOUND, None))["nodes"]
                          if n["name"] == "master")
            self.assertEqual(master["control"], {"Gain 1": 1.0 if enabled else 0.0, "Gain 2": 0.0 if enabled else 1.0})
        # mute finds and writes Booth Mic's node only, by its exact name: never one of Booth Stereo's
        self.assertIsNone(mute.find(json.loads(dump(*UP, *OTHER_UP))))
        self.assertEqual(mute.find(json.loads(dump(*UP, BOOTH_MIC)))[0], 106)

    def test_its_nodes_are_its_own_and_it_is_never_picked_as_the_default_mic(self):
        text = stereo.build(settings(), FOUND, OTHERS["scarlett"])
        names = [a[k]["node.name"] for a in modules(text).values() for k in ("capture.props", "playback.props")]
        self.assertEqual(sorted(names), ["booth.stereo.input", "booth.stereo.other", "booth.stereo.other.out",
                                         "booth.stereo.voice", "booth.stereo.voice.out", "booth_stereo"])
        self.assertLess(modules(text)["source"]["playback.props"]["priority.session"], 0)  # below Booth Mic's
        self.assertIn("\n  ", text)  # pretty-printed: PipeWire 1.0.5 skips the modules of a one-line conf


class Target(unittest.TestCase):
    """The other side's source, resolved by name or position now; missing: silent and said, never another source."""

    def setUp(self):
        self.reads, self.state = [], {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, DUMP, "XLR")}

    def read(self, choice):
        self.reads.append(choice)
        return self.state

    def target(self, s, text=DUMP):
        return stereo.target(s, text, self.read)

    def test_the_sources_are_every_other_audio_source_never_booths_own_nor_the_scarletts(self):
        text = dump(*UP, BOOTH_MIC, node(200, "booth.mic.select", 9, "Audio/Source"),
                    node(201, "booth.test.tone", 9, "Audio/Source"))
        self.assertEqual(stereo.sources(text), [
            ("alsa_input.usb-Generic_USB_Audio-00.iec958-stereo", "USB Audio Digital Stereo (IEC958)"),
            (BRIO, "BRIO Ultra HD Webcam Analog Stereo"),
            ("alsa_input.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.mono-fallback",
             "PRO X Wireless Gaming Headset Mono")])
        self.assertEqual(stereo.sources("not json"), [])

    def test_the_default_is_the_scarletts_input_the_mic_isnt_on_left_for_the_voice(self):
        self.assertEqual(self.target(settings())[0], {"node": SCARLETT_IN, "position": "FL"})  # Instrument, channel 1
        self.assertEqual(self.target({})[1], "Right: the Scarlett's Instrument input, capture channel 1 (FL)")

    def test_the_scarlett_side_takes_the_channel_the_mic_doesnt_use_by_position(self):
        self.state = {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, DUMP, "Instrument")}
        other, said = self.target(dict(settings("R", "scarlett"), microphone={"input": "Instrument"}))
        self.assertEqual(other, {"node": SCARLETT_IN, "position": "FR"})  # the XLR's, channel 2
        self.assertEqual(said, "Left: the Scarlett's XLR input, capture channel 2 (FR)")
        self.assertEqual(self.reads, ["Instrument"])  # the Scarlett read now, for the mic's input

    def test_computer_sound_is_the_configured_default_outputs_monitor_resolved_by_name_each_time(self):
        self.assertEqual(self.target(settings(source="computer")),
                         ({"node": SCARLETT_OUT, "sink": True}, "Right: the computer's sound (Scarlett Solo 4th Gen "
                                                                "Analog Stereo)"))
        self.assertEqual(self.target(settings(source="computer"), configured(USB_OUT))[0],
                         {"node": USB_OUT, "sink": True})
        self.assertEqual(self.reads, [])  # no amixer for it

    def test_a_missing_source_leaves_its_side_silent_and_says_so_never_another_source(self):
        unset = configured(None)  # only the effective default: never taken for the configured one
        gone = DUMP.replace(BRIO, "alsa_input.gone")
        for s, text, said in ((settings(source="computer"), unset, "Right: silent, no default output is set"),
                              (settings("R", BRIO), gone, f"Left: silent, {BRIO} isn't connected"),
                              (settings(source="booth_mic"), dump(BOOTH_MIC), "Right: silent, booth_mic isn't connected"),
                              (settings(source="booth_stereo"), dump(*UP), "Right: silent, booth_stereo isn't connected"),
                              (settings(source="computer"), configured("alsa_output.gone"),
                               "Right: silent, the default output isn't connected")):
            with self.subTest(said=said):
                self.assertEqual(self.target(s, text), (None, said))
        self.state = None  # the Scarlett unplugged
        self.assertEqual(self.target(settings()), (None, "Right: silent, the Scarlett isn't connected"))
        self.state = {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, "", "XLR")}  # its card there, no node in PipeWire
        self.assertEqual(self.target(settings(), "[]"), (None, "Right: silent, the Scarlett isn't connected"))
        ctrls = json.loads(json.dumps(CTRLS))
        ctrls["PCM 01 Capture Enum"]["values"] = [0]  # the instrument jack on no capture channel
        self.state = {"ctrls": ctrls, "mic": scarlett.mic(ctrls, DUMP, "XLR")}
        self.assertEqual(self.target(settings()),
                         (None, "Right: silent, the Scarlett's Instrument input isn't routed to a capture channel"))

    def test_the_scarletts_other_input_on_the_mics_channel_leaves_its_side_silent_never_the_voice_twice(self):  # #4
        self.state = {"ctrls": CTRLS, "mic": dict(self.state["mic"], channel=1)}  # the Instrument's channel
        self.assertEqual(self.target(settings()),
                         (None, "Right: silent, the Scarlett's Instrument input is on the mic's capture channel"))

    def test_a_channel_its_pipewire_source_lacks_leaves_its_side_silent_never_raises(self):  # #5: heal tick, throttle
        ctrls = json.loads(json.dumps(CTRLS))
        ctrls["PCM 01 Capture Enum"]["values"], ctrls["PCM 03 Capture Enum"]["values"] = [0], [1]  # Instrument on 3
        objects = json.loads(DUMP)
        scarlett_in = next(o["info"]["props"] for o in objects
                           if ((o.get("info") or {}).get("props") or {}).get("node.name") == SCARLETT_IN)
        scarlett_in["audio.position"] = "FL,FR"  # a 2-channel profile
        self.state = {"ctrls": ctrls, "mic": scarlett.mic(ctrls, json.dumps(objects), "XLR")}
        self.assertEqual(self.target(settings(), json.dumps(objects)),
                         (None, "Right: silent, the Scarlett's Instrument input is on capture channel 3, which its "
                                "PipeWire source lacks"))
        del scarlett_in["audio.position"]  # no positions at all
        self.state = {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, json.dumps(objects), "XLR")}
        self.assertEqual(self.target(settings(), json.dumps(objects)),
                         (None, "Right: silent, the Scarlett's Instrument input is on capture channel 1, which its "
                                "PipeWire source lacks"))

    def test_a_discovered_device_is_taken_by_name(self):
        self.assertEqual(self.target(settings("R", BRIO)),
                         ({"node": BRIO}, "Left: BRIO Ultra HD Webcam Analog Stereo"))


class Choices(unittest.TestCase):
    def test_the_scarlett_computer_sound_then_each_source_and_a_saved_one_thats_gone(self):
        found = [(BRIO, "BRIO")]
        self.assertEqual(stereo.choices(settings(), found),
                         [("scarlett", "Scarlett Instrument input"), ("computer", "Computer sound"), (BRIO, "BRIO")])
        # the input the mic isn't on, as the Scarlett side takes it: the XLR while the mic is on the Instrument
        mic_on_instrument = dict(settings(), microphone={"input": "Instrument"})
        self.assertEqual(stereo.choices(mic_on_instrument, found)[0], ("scarlett", "Scarlett XLR input"))
        self.assertEqual(stereo.choices(settings(source="alsa_input.gone"), found)[-1],
                         ("alsa_input.gone", "alsa_input.gone (not connected)"))


class Sync(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "stereo.conf"
        self.ran, self.ok, self.t, self.props, self.other = [], True, 0.0, None, ()
        self.gone, self.disk, self.notes, self.base = (), None, [], DUMP  # nodes gone; the saved settings; notices
        self.mic = MIC_INPUT
        self.state = {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, DUMP, "XLR")}
        self.found = dict(FOUND)
        stereo._STARTED.clear()
        patch = mock.patch.object(voice, "verified", lambda where=None: self.found)
        patch.start()
        self.addCleanup(patch.stop)

    def run_(self, cmd):
        self.ran.append(cmd)
        if self.ok and cmd[:2] == ["pw-cli", "set-param"] and cmd[2] == "142":  # lands on booth.stereo.voice
            self.props.update(itertools.batched(json.loads(cmd[4])["params"], 2))
        return self.ok

    def dump_(self):
        if self.mic is None:
            return dump(base=self.base)
        if self.props is None:
            return dump(self.mic, base=self.base)
        up = [n for n in UP if n["info"]["props"]["node.name"] not in self.gone]
        return dump(self.mic, *up, *self.other, props=self.props, base=self.base)

    def sync(self, s=None):
        s = s or settings()
        return stereo.sync(s, lambda _c: self.state, self.run_, self.dump_, self.file, lambda: self.t,
                           saved=lambda: self.disk or s, notify=self.notes.append)

    def up(self, s=None):
        """Booth Stereo started as its pre-start writes it: its nodes up, its chain's Props what it loaded."""
        s = s or settings()
        other = stereo.target(s, self.base, lambda _c: self.state)[0]
        self.file.write_text(stereo.build(s, self.found, other))
        self.other = OTHER_UP if other else ()
        self.props = {f"{n['name']}:{k}": v for n in graph(self.file.read_text())["nodes"]
                      for k, v in n.get("control", {}).items()}

    def test_the_first_sync_starts_only_its_own_unit_and_leaves_its_config_to_its_pre_start(self):
        said = self.sync()
        self.assertEqual(said, "Right: the Scarlett's Instrument input, capture channel 1 (FL)")
        self.assertEqual(self.ran, [RESTART])
        self.assertFalse(self.file.exists())  # written only by booth-stereo.service's pre-start
        self.assertEqual((stereo.LAST, stereo.SOURCES[0][0]), (said, "alsa_input.usb-Generic_USB_Audio-00.iec958-stereo"))

    def test_running_and_unchanged_touches_nothing(self):
        self.up()
        self.assertEqual(self.sync(), "Right: the Scarlett's Instrument input, capture channel 1 (FL)")
        self.assertEqual(self.ran, [])

    def test_changing_side_or_source_restarts_only_booth_stereo_at_once_never_booth_mic(self):
        self.up()
        for s in (settings("R"), settings("R", "computer"), settings("R", BRIO)):
            self.t += 1  # inside the backoff: a change restarts at once
            self.sync(s)
            self.assertEqual(self.ran[-1], RESTART)
            self.up(s)
        self.assertEqual(self.ran, [RESTART] * 3)
        self.assertEqual(self.notes, [])  # the user's own change: no notice

    def test_left_right_left_within_the_backoff_restarts_at_each_change(self):  # #2
        self.sync()
        self.up()
        for s in (settings("R"), settings("L")):
            self.t += 1
            self.sync(s)
            self.up(s)
        self.assertEqual(self.ran, [RESTART] * 3)

    def test_booth_stereo_up_without_its_voice_is_restarted_never_left_one_sided(self):  # #1
        self.up()
        self.gone = (stereo.VOICE,)  # booth-voice restarted: booth.stereo.voice went with the old booth.mic.input
        self.assertEqual(self.sync(), "Right: the Scarlett's Instrument input, capture channel 1 (FL)")
        self.assertEqual(self.ran, [RESTART])

    def test_its_voice_gone_between_the_dump_and_the_live_set_says_not_applied(self):  # #3
        self.up()
        real = self.dump_

        def dump_():
            text, self.gone = real(), (stereo.VOICE,)
            return text
        self.dump_ = dump_
        self.assertEqual(self.sync(settings(tone_air=-6.0)), "not applied: Booth Stereo didn't take the change")
        self.assertEqual(self.ran, [])

    def test_a_side_or_source_that_wasnt_saved_never_restarts_it_the_next_save_does(self):  # #7
        self.up()
        self.disk = settings()  # the save of R failed: its pre-start would still build L, so a restart is a loop
        self.assertEqual(self.sync(settings("R")), "Right: the Scarlett's Instrument input, capture channel 1 (FL)")
        self.t += stereo.BACKOFF_S
        self.sync(settings("R"))
        self.assertEqual(self.ran, [])  # the running config is kept
        self.disk = None  # saved now
        self.sync(settings("R"))
        self.assertEqual(self.ran, [RESTART])

    def test_computer_sound_on_a_new_default_output_restarts_it_and_says_so_once(self):  # #8
        s = settings(source="computer")
        self.up(s)
        self.base = configured(USB_OUT)  # #12 or the user moved the default output
        self.ok = False
        self.sync(s)
        self.assertEqual(self.notes, [])  # not restarted: nothing to say
        self.ok, self.t = True, stereo.BACKOFF_S
        self.sync(s)
        self.assertEqual(self.notes, ["Booth Stereo switched computer sound to USB Audio Digital Stereo (IEC958) — a "
                                      "recording on it restarted"])
        self.up(s)
        self.sync(s)
        self.assertEqual((self.ran, len(self.notes)), ([RESTART] * 2, 1))  # once
        self.props, self.base, self.t = None, DUMP, self.t + stereo.BACKOFF_S  # not running: no recording on it
        self.sync(s)
        self.assertEqual((self.ran[-1], len(self.notes)), (RESTART, 1))

    def test_w5_a_mic_input_that_wasnt_saved_never_restarts_it(self):  # #19 W5 final: its pre-start reads the saved
        self.up()
        self.disk = settings()  # the save of the Instrument failed: its pre-start still builds the XLR's other side
        s = dict(settings(), microphone={"input": "Instrument"})
        self.sync(s)
        self.t += stereo.BACKOFF_S
        self.sync(s)
        self.assertEqual(self.ran, [])  # never a restart a minute

    def test_w5_a_restart_on_the_same_computer_output_says_nothing(self):  # p02: only a new default output is said
        s = settings(source="computer")
        self.up(s)
        self.sync(settings("R", "computer"))  # the user's side change, same output
        self.assertEqual((self.ran, self.notes), ([RESTART], []))

    def test_w5_a_knob_tick_while_muted_never_silences_booth_stereo(self):  # p06: live() builds it unmuted too
        self.up()
        with mock.patch.object(stereo, "LAST", "Right: x"):
            stereo.live(settings(muted=True, tone_air=-6.0), self.run_, self.dump_)
        self.assertEqual(self.props["tone_air:Gain"], -6.0)
        self.assertEqual((self.props["master:Gain 1"], self.props["master:Gain 2"]), (1.0, 0.0))

    def test_a_knob_tick_only_sets_its_chains_values_live_never_a_shape_or_restart_decision(self):  # #10
        self.up()
        s = settings("R", tone_air=-6.0)  # a side change waiting too: sync's, never the throttle's
        with mock.patch.object(stereo, "LAST", "Right: x"):
            self.assertEqual(stereo.live(s, self.run_, self.dump_), "Right: x")
            self.assertEqual({tuple(c[:3]) for c in self.ran}, {("pw-cli", "set-param", "142")})
            self.assertEqual(self.props["tone_air:Gain"], -6.0)
            self.ok = False
            self.assertEqual(stereo.live(settings(tone_air=3.0), self.run_, self.dump_),
                             "not applied: Booth Stereo didn't take tone_air:Gain")
            self.assertEqual(stereo.LAST, "Right: x")
        self.assertNotIn(RESTART, self.ran)

    def test_a_preset_or_knob_change_goes_live_on_booth_stereos_own_chain_readback_verified(self):
        self.up()
        s = settings(**voice.PRESETS["Noisy room"])
        self.assertEqual(self.sync(s), "Right: the Scarlett's Instrument input, capture channel 1 (FL)")
        sets = [c for c in self.ran if c[:2] == ["pw-cli", "set-param"]]
        self.assertEqual({c[2] for c in sets}, {"142"})  # booth.stereo.voice, never booth.mic.input (97)
        self.assertNotIn(RESTART, self.ran)  # live: no restart
        self.assertLessEqual(max(len(json.loads(c[4])["params"]) // 2 for c in sets), voice.CHUNK)
        self.assertEqual(self.props["noise:VAD Threshold (%)"], 81.0)
        self.ran.clear()
        self.sync(s)  # now in place: nothing sent again
        self.assertEqual(self.ran, [])

    def test_a_live_change_that_doesnt_read_back_says_so(self):
        self.up()
        self.ok = False
        self.assertEqual(self.sync(settings(tone_air=-6.0)), "not applied: Booth Stereo didn't take tone_air:Gain")

    def test_booth_mics_mute_never_reaches_booth_stereo(self):
        self.up()
        self.sync(settings(muted=True))
        self.assertEqual(self.ran, [])
        self.assertEqual((self.props["master:Gain 1"], self.props["master:Gain 2"]), (1.0, 0.0))

    def test_a_plugin_that_stops_verifying_never_restarts_a_running_booth_stereo_its_stage_goes_dry(self):
        self.up()
        del self.found["sc4m_1916.so"]
        self.sync()
        self.assertNotIn(RESTART, self.ran)
        self.assertEqual((self.props["leveling_mix:Gain 1"], self.props["leveling_mix:Gain 2"]), (0.0, 1.0))

    def test_its_other_side_gone_needs_no_restart_its_return_does(self):
        s = settings("R", BRIO)
        self.up(s)
        self.other = ()  # the BRIO unplugged: WirePlumber destroyed that loopback, the recording goes on
        with mock.patch.object(stereo, "target", lambda *_a: (None, f"Left: silent, {BRIO} isn't connected")):
            self.assertEqual(self.sync(s), f"Left: silent, {BRIO} isn't connected")
        self.assertEqual(self.ran, [])
        self.sync(s)  # back: restarted to take it again
        self.assertEqual(self.ran, [RESTART])

    def test_waits_for_booth_mic_before_starting(self):
        self.mic = None  # no booth.mic.input: nothing to take the voice from
        self.assertEqual(self.sync(), stereo.NO_MIC)
        self.assertEqual(self.ran, [])

    def test_pw_dump_failing_runs_nothing(self):
        self.dump_ = lambda: ""
        self.assertEqual(self.sync(), stereo.NO_MIC)
        self.assertEqual(self.ran, [])

    def test_a_unit_that_wont_stay_up_is_restarted_at_most_once_a_minute(self):
        self.sync()
        self.t = stereo.BACKOFF_S - 1
        self.assertEqual(self.sync(), stereo.NOT_RUNNING)
        self.t = stereo.BACKOFF_S
        self.sync()
        self.assertEqual(self.ran, [RESTART, RESTART])

    def test_a_new_booth_mic_input_restarts_it_at_once(self):  # its capture went with the old booth.mic.input
        self.sync()
        self.mic = node(98, "booth.mic.input", 901, "Audio/Sink/Internal")
        self.sync()
        self.assertEqual(self.ran, [RESTART, RESTART])

    def test_a_failed_start_says_so(self):
        self.ok = False
        self.assertEqual(self.sync(), "not started: see systemctl --user status booth-stereo.service")


class Write(unittest.TestCase):
    """booth-stereo.service's ExecStartPre: the config its pipewire loads, from the saved settings, the source found
    now and the plugins verified right then."""

    def test_the_pre_start_writes_the_config_from_fresh_plugins_and_the_source_found_now(self):
        state = {"ctrls": CTRLS, "mic": scarlett.mic(CTRLS, DUMP, "XLR")}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "stereo.conf"
            saved = settings("R", BRIO, muted=True)
            verified = mock.Mock(return_value={})
            with mock.patch.object(voice, "verified", verified), mock.patch.object(store, "load", lambda: saved), \
                    mock.patch.object(voice, "_dump", lambda: DUMP), mock.patch.object(scarlett, "read", lambda c: state):
                self.assertIsNone(stereo.write(run, Path("/plugins")))
            verified.assert_called_once_with(Path("/plugins"))
            self.assertEqual(run.read_text(), stereo.build(saved, {}, {"node": BRIO}))
            self.assertNotIn("ladspa", run.read_text())  # a plugin that doesn't verify now is never loaded
            run.unlink()
            run.mkdir()  # can't be written: the unit doesn't start
            with mock.patch.object(voice, "verified", verified), mock.patch.object(store, "load", lambda: saved), \
                    mock.patch.object(voice, "_dump", lambda: DUMP), mock.patch.object(scarlett, "read", lambda c: state):
                self.assertTrue(stereo.write(run, Path("/plugins")).startswith("not saved"))


class Note(unittest.TestCase):
    def test_the_note_says_it_carries_the_voice_while_muted_and_is_for_recording_not_calls(self):
        self.assertIn("even while Booth Mic is muted", stereo.NOTE)
        self.assertIn("computer's sound", stereo.NOTE)
        self.assertIn("recording apps, not calls", stereo.NOTE)
        self.assertIn("a recording on it restarts", stereo.NOTE)  # #8: computer sound following a new output


if __name__ == "__main__":
    unittest.main()
