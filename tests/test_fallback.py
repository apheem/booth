"""Mic fallback (#20): with the Scarlett gone, Booth Mic's selector takes the PRO X mic while the headset is on, else the
BRIO mic, each by name and its own discovered position, and goes back to the Scarlett when it returns. Only the selector
ever restarts, so Booth Mic (booth_mic) and its node mute stay; the routing policy says each settled change once."""
import json
import tempfile
import unittest
from pathlib import Path

from booth import routing, voice
from tests.test_headset import DUMP
from tests.test_voice import (ACTIVE, MIC, ON, RESTART_SELECT, STATE, STOP_SELECT, UNITS, module, node, rerouted,
                              selector)

PROX_MIC = "alsa_input.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.mono-fallback"  # the fixture's, MONO
BRIO_MIC = "alsa_input.usb-046d_Logitech_BRIO_FAKESER1-03.analog-stereo"  # the fixture's, FL,FR
SCARLETT_SOURCE = MIC["node"]


def desk(*gone, positions=None):
    """The fixture's pw-dump with Booth Mic's input in it, minus the nodes whose name starts with any of `gone`;
    `positions`: {node name: audio.position} overrides, as a device might report them."""
    objects = [o for o in json.loads(DUMP)
               if not ((o.get("info") or {}).get("props") or {}).get("node.name", "").startswith(gone or "\0")]
    for o in objects:
        props = (o.get("info") or {}).get("props") or {}
        if props.get("node.name") in (positions or {}):
            props["audio.position"] = positions[props["node.name"]]
    return json.dumps(objects + [node(71, voice.INPUT)])


ABSENT = desk("alsa_input.usb-Focusrite", "alsa_output.usb-Focusrite")  # the Scarlett unplugged: gone from PipeWire


def capture(cfg):
    return module(cfg, "libpipewire-module-loopback")["capture.props"]


class Choose(unittest.TestCase):
    """voice.choose: the one source choice, (which mic, what apply() takes)."""

    def test_the_scarlett_present_is_always_the_scarlett_whatever_else_is_there(self):
        self.assertEqual(voice.choose(STATE, True, desk(), None), (voice.SCARLETT, STATE))

    def test_the_scarlett_present_with_the_mic_not_routed_never_falls_back(self):
        state = rerouted("PCM 1")  # privacy: the selector stops, silent, never another mic
        self.assertEqual(voice.choose(state, True, desk(), None), (voice.SCARLETT, state))

    def test_absent_with_the_headset_on_takes_the_prox_mic_mono(self):
        source, state = voice.choose(None, True, ABSENT, SCARLETT_SOURCE)
        self.assertEqual(source, voice.PRO_X_MIC)
        self.assertEqual((state["mic"]["node"], state["mic"]["position"]), (PROX_MIC, "MONO"))

    def test_absent_with_the_headset_off_takes_the_brio_by_its_first_discovered_position(self):
        source, state = voice.choose(None, False, ABSENT, SCARLETT_SOURCE)
        self.assertEqual((source, state["mic"]["node"], state["mic"]["position"]), (voice.BRIO_MIC, BRIO_MIC, "FL"))
        swapped = desk("alsa_input.usb-Focusrite", positions={BRIO_MIC: "FR,FL"})  # never a guessed one
        self.assertEqual(voice.choose(None, False, swapped, None)[1]["mic"]["position"], "FR")

    def test_the_headset_on_with_no_prox_mic_in_pipewire_takes_the_brio(self):
        dump = desk("alsa_input.usb-Focusrite", "alsa_input.usb-Logitech_PRO_X")
        self.assertEqual(voice.choose(None, True, dump, None)[0], voice.BRIO_MIC)

    def test_a_backup_with_no_position_is_skipped_never_silenced_on_a_wrong_one(self):
        dump = desk("alsa_input.usb-Focusrite", positions={BRIO_MIC: ""})
        self.assertEqual(voice.choose(None, False, dump, None), (None, None))

    def test_no_mic_at_all_stops_a_backup_selector_and_keeps_the_scarlett_config(self):
        dump = desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d")
        self.assertEqual(voice.choose(None, False, dump, BRIO_MIC), (None, {"mic": None}))  # never left on a mic
        self.assertEqual(voice.choose(None, False, dump, PROX_MIC), (None, {"mic": None}))  # Booth no longer picks
        self.assertEqual(voice.choose(None, False, dump, SCARLETT_SOURCE), (None, None))  # it unloaded itself

    def test_the_scarlett_still_in_pipewire_is_never_absent(self):  # amixer failed, or the card is going
        self.assertEqual(voice.choose(None, True, desk(), SCARLETT_SOURCE), (voice.SCARLETT, None))

    def test_a_failed_pw_dump_leaves_the_selector_as_it_is(self):
        for dump in ("", "not json"):
            self.assertEqual(voice.choose(None, True, dump, PROX_MIC), (voice.PRO_X_MIC, None), dump)
            self.assertEqual(voice.choose(None, True, dump, None), (None, None), dump)

    def test_a_backup_selector_takes_only_its_mic_never_reconnected_or_heard_from_another(self):
        cfg = json.loads(voice.build_selector(voice.choose(None, True, ABSENT, None)[1]["mic"]))
        props = capture(cfg)
        self.assertEqual((props["target.object"], props["audio.position"], props["node.dont-reconnect"]),
                         (PROX_MIC, ["MONO"], True))
        self.assertIsNone(props[voice.CHANNEL])  # never kept as a Scarlett channel when the Scarlett is back
        self.assertEqual(module(cfg, "libpipewire-module-loopback")["playback.props"]["target.object"], voice.INPUT)


class Fallback(unittest.TestCase):
    """voice.heal on the 10 s tick (or a debounced poke) is the one writer of the selector, Scarlett or backup."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.select = self.file.with_name(voice.SELECT_CONF)
        self.ran = []
        voice._HEALED.clear()
        self.addCleanup(voice._HEALED.clear)
        voice.apply(ON, STATE, run=lambda cmd: True, dump=lambda: "[]", file=self.file)  # on the Scarlett

    def heal(self, state, dump, on=True):
        voice.heal(ON, read=lambda choice: state, run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: dump,
                   file=self.file, clock=lambda: 1000.0, on=lambda: on)
        return voice.CHOSEN, json.loads(self.select.read_text()) if self.select.exists() else None

    def test_scarlett_out_then_headset_off_then_scarlett_back(self):
        chosen, cfg = self.heal(None, ABSENT)
        self.assertEqual((chosen, capture(cfg)["target.object"], capture(cfg)["audio.position"]),
                         (voice.PRO_X_MIC, PROX_MIC, ["MONO"]))
        chosen, cfg = self.heal(None, ABSENT, on=False)
        self.assertEqual((chosen, capture(cfg)["target.object"], capture(cfg)["audio.position"]),
                         (voice.BRIO_MIC, BRIO_MIC, ["FL"]))
        chosen, cfg = self.heal(STATE, desk())
        self.assertEqual((chosen, cfg), (voice.SCARLETT, selector()))
        self.assertEqual(self.ran, [ACTIVE, RESTART_SELECT] * 3)  # each switch: the selector restarted at once

    def test_no_fallback_path_ever_restarts_booth_mic_so_its_mute_survives(self):
        for state, dump, on in ((None, ABSENT, True), (None, ABSENT, False), (STATE, desk(), True),
                                (None, desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d"), False),
                                (None, ABSENT, True), (STATE, desk(), False)):
            self.heal(state, dump, on)
        touched = {u for cmd in self.ran for u in cmd if u.endswith(".service")}
        self.assertTrue(self.ran)
        self.assertEqual({tuple(c[:3]) for c in self.ran if UNITS[0] in c}, {tuple(ACTIVE[:3])})  # only asked
        self.assertLessEqual(touched, set(UNITS))

    def test_no_mic_left_stops_a_backup_selector_and_the_scarlett_brings_it_back(self):
        self.heal(None, ABSENT, on=False)  # on the BRIO
        self.ran.clear()
        chosen, cfg = self.heal(None, desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d"), on=False)
        self.assertEqual((chosen, cfg, self.ran), (None, None, [STOP_SELECT]))  # silent, never a mic Booth no longer picks
        chosen, cfg = self.heal(STATE, desk())
        self.assertEqual((chosen, cfg), (voice.SCARLETT, selector()))

    def test_the_scarlett_unplugged_with_no_backup_keeps_its_config_as_before(self):
        chosen, cfg = self.heal(None, desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d"), on=False)
        self.assertEqual((chosen, cfg, self.ran), (None, selector(), []))

    def test_the_scarlett_there_with_the_mic_not_routed_stops_never_falls_back(self):
        self.heal(None, ABSENT)  # on the PRO X
        self.ran.clear()
        chosen, cfg = self.heal(rerouted("PCM 1"), desk())
        self.assertEqual((chosen, cfg, self.ran), (voice.SCARLETT, None, [STOP_SELECT]))


class Sequences(unittest.TestCase):
    """The desk through heal and the routing policy's "mic" rule, as the app runs them: a change is said once it holds
    on two heals in a row (the second one a debounced poke, ~2 s later)."""

    def setUp(self):
        Fallback.setUp(self)
        self.state, self.notes, self.labels = {}, [], []

    heal = Fallback.heal

    def tick(self, state, dump, on=True):
        self.heal(state, dump, on)
        self.state, actions = routing.next(self.state, {"kind": "mic", "source": voice.CHOSEN})
        for kind, value in actions:
            (self.notes if kind == "notify" else self.labels).append(value)

    def test_scarlett_out_is_said_once(self):
        for _ in range(4):
            self.tick(None, ABSENT)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.PRO_X_MIC]], [voice.PRO_X_MIC]))

    def test_scarlett_out_with_the_headset_off_is_said_once(self):
        for _ in range(3):
            self.tick(None, ABSENT, on=False)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.BRIO_MIC]], [voice.BRIO_MIC]))

    def test_scarlett_back_is_said_too(self):
        for state, dump in ((None, ABSENT), (None, ABSENT), (STATE, desk()), (STATE, desk())):
            self.tick(state, dump)
        self.assertEqual(self.labels, [voice.PRO_X_MIC, voice.SCARLETT])
        self.assertEqual(self.notes[-1], routing.MIC_SAID[voice.SCARLETT])

    def test_flapping_says_nothing_until_it_settles_then_once(self):
        for _ in range(10):  # unplugged and replugged between every heal
            self.tick(None, ABSENT)
            self.tick(STATE, desk())
        self.assertEqual(self.notes, [])
        for _ in range(3):
            self.tick(None, ABSENT)
        self.assertEqual(self.labels, [voice.PRO_X_MIC])

    def test_mute_is_preserved_booth_mic_is_never_restarted_and_nothing_touches_its_mute(self):
        for state, dump, on in ((None, ABSENT, True),) * 2 + ((None, ABSENT, False),) * 2 + ((STATE, desk(), True),) * 2:
            self.tick(state, dump, on)
        self.assertEqual(self.labels, [voice.PRO_X_MIC, voice.BRIO_MIC, voice.SCARLETT])
        self.assertFalse([c for c in self.ran if UNITS[0] in c and c != ACTIVE])
        self.assertFalse([c for c in self.ran if "wpctl" in c or "set-mute" in c])


if __name__ == "__main__":
    unittest.main()
