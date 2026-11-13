"""Mic fallback (#20): with the Scarlett gone, Booth Mic's selector takes the PRO X mic while the headset is on, else the
BRIO mic, each by name and its own discovered position, and goes back to the Scarlett when it returns. Only the selector
ever restarts, so Booth Mic (booth_mic) and its node mute stay; the routing policy says each settled change once."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from booth import routing, voice
from tests.test_headset import DUMP
from tests.test_voice import (ACTIVE, MIC, ON, RESTART_SELECT, STATE, STOP_SELECT, UNITS, module, node, rerouted,
                              selector)

PROX_MIC = "alsa_input.usb-Logitech_PRO_X_Wireless_Gaming_Headset-00.mono-fallback"  # the fixture's, MONO
BRIO_MIC = "alsa_input.usb-046d_Logitech_BRIO_FAKESER1-03.analog-stereo"  # the fixture's, FL,FR
SCARLETT_SOURCE = MIC["node"]
C920 = "alsa_input.usb-046d_HD_Pro_Webcam_C920_FAKESER1-02.analog-stereo"  # another Logitech webcam's mic
NO_NODE = {"ctrls": STATE["ctrls"], "mic": dict(MIC, node=None, position=None, channels=None)}  # card there, no source
RealThread = threading.Thread  # the page tests swap threading.Thread for an inline one


def locked():
    """True when another thread holds voice._LOCK now (tried from a fresh thread, so an RLock's owner can't pass)."""
    out = []

    def probe():
        got = voice._LOCK.acquire(blocking=False)
        out.append(not got)
        if got:
            voice._LOCK.release()
    t = RealThread(target=probe)
    t.start()
    t.join()
    return out[0]


def with_node(dump, name, media_class):
    """`dump` plus a node named `name` of `media_class`, at FL,FR."""
    return json.dumps(json.loads(dump) + [{"id": 99, "type": "PipeWire:Interface:Node", "info": {"props": {
        "node.name": name, "media.class": media_class, "audio.position": "FL,FR"}}}])


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
SCARLETT_BACK_DUMP = json.dumps([node(71, voice.INPUT), node(40, MIC["node"])])  # the selector unloaded itself


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

    def test_the_scarlett_in_pipewire_but_not_alsa_names_the_backup_that_still_feeds(self):  # kept: what runs
        self.assertEqual(voice.choose(None, False, desk(), BRIO_MIC), (voice.BRIO_MIC, None))
        self.assertEqual(voice.choose(None, True, desk(), PROX_MIC), (voice.PRO_X_MIC, None))

    def test_another_logitech_capture_device_is_never_taken_as_the_brio(self):
        dump = with_node(desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d"), C920, "Audio/Source")
        self.assertEqual(voice.choose(None, False, dump, None), (None, None))

    def test_a_stream_named_like_a_backup_is_never_taken_as_its_mic(self):
        dump = with_node(desk("alsa_input.usb-Focusrite", "alsa_input.usb-046d"), BRIO_MIC, "Stream/Input/Audio")
        self.assertEqual(voice.choose(None, False, dump, None), (None, None))

    def test_the_scarlett_card_there_with_no_source_in_pipewire_is_no_input_never_a_backup(self):
        self.assertEqual(voice.choose(NO_NODE, True, ABSENT, SCARLETT_SOURCE), (voice.NO_INPUT, NO_NODE))
        self.assertEqual(voice.choose(NO_NODE, True, "", SCARLETT_SOURCE)[0], voice.SCARLETT)  # pw-dump failed
        self.assertEqual(voice.choose(NO_NODE, True, desk(), SCARLETT_SOURCE)[0], voice.SCARLETT)  # back since
        self.assertEqual(voice.choose(rerouted("PCM 1"), True, ABSENT, None)[0], voice.SCARLETT)  # not routed

    def test_a_failed_pw_dump_leaves_the_selector_as_it_is(self):
        for dump in ("", "not json"):
            self.assertEqual(voice.choose(None, True, dump, PROX_MIC), (voice.PRO_X_MIC, None), dump)
            self.assertEqual(voice.choose(None, True, dump, SCARLETT_SOURCE), (voice.SCARLETT, None), dump)
            self.assertEqual(voice.choose(None, True, dump, None), (None, None), dump)

    def test_the_headset_not_answered_yet_keeps_the_prox_and_a_fresh_choice_takes_the_brio(self):
        self.assertEqual(voice.choose(None, None, ABSENT, PROX_MIC)[0], voice.PRO_X_MIC)  # a restart on the PRO X
        self.assertEqual(voice.choose(None, None, ABSENT, BRIO_MIC)[0], voice.BRIO_MIC)
        # fresh: the dongle stays in PipeWire with the headset off, so the PRO X could be silence said as a mic
        self.assertEqual(voice.choose(None, None, ABSENT, SCARLETT_SOURCE)[0], voice.BRIO_MIC)
        self.assertEqual(voice.choose(None, None, ABSENT, None)[0], voice.BRIO_MIC)

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

    def heal(self, state, dump, on=True, fail=None):
        voice.heal(ON, read=lambda choice: state, run=lambda cmd: self.ran.append(cmd) or cmd != fail,
                   dump=lambda: dump, file=self.file, clock=lambda: 1000.0, on=lambda: on)
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

    def test_booth_restarted_on_the_prox_keeps_it_until_the_headset_answers(self):
        self.heal(None, ABSENT)  # on the PRO X
        self.ran.clear()
        chosen, cfg = self.heal(None, ABSENT, on=None)
        self.assertEqual((chosen, capture(cfg)["target.object"], self.ran), (voice.PRO_X_MIC, PROX_MIC, []))

    def test_scarlett_in_pipewire_not_alsa_while_on_a_backup_names_the_brio_that_feeds(self):
        self.heal(None, ABSENT, on=False)  # on the BRIO
        chosen, cfg = self.heal(None, desk(), on=False)  # amixer empty or timed out, the Scarlett's node still there
        self.assertEqual((chosen, capture(cfg)["target.object"]), (voice.BRIO_MIC, BRIO_MIC))

    def test_a_failed_stop_on_return_unrouted_names_the_brio_that_still_feeds(self):
        self.heal(None, ABSENT, on=False)  # on the BRIO
        chosen, cfg = self.heal(rerouted("PCM 1"), desk(), fail=STOP_SELECT)
        self.assertEqual((chosen, capture(cfg)["target.object"]), (voice.BRIO_MIC, BRIO_MIC))
        chosen, cfg = self.heal(rerouted("PCM 1"), desk())  # stopped this time
        self.assertEqual((chosen, cfg), (voice.SCARLETT, None))

    def test_a_failed_restart_onto_a_backup_names_no_mic(self):
        chosen, cfg = self.heal(None, ABSENT, on=False, fail=RESTART_SELECT)
        self.assertEqual((chosen, cfg), (None, None))  # its config removed for the retry: nothing feeds Booth Mic

    def test_the_scarlett_card_there_with_no_source_keeps_its_selector_and_names_no_input(self):
        chosen, cfg = self.heal(NO_NODE, ABSENT)
        self.assertEqual((chosen, cfg, self.ran), (voice.NO_INPUT, selector(), []))  # never a backup: it is there

    def test_heal_reads_and_applies_under_the_lock_so_a_page_poll_never_lands_in_between(self):
        held = []
        voice.heal(ON, read=lambda choice: held.append(locked()) or None, run=lambda cmd: True, dump=lambda: ABSENT,
                   file=self.file, clock=lambda: 1000.0, on=lambda: True)
        self.assertEqual(held, [True])

    def test_heal_keeps_the_pw_dump_it_read_for_running_and_none_when_it_read_none(self):  # one pw-dump less a tick
        self.heal(STATE, SCARLETT_BACK_DUMP)
        self.assertEqual(voice.SEEN, SCARLETT_BACK_DUMP)
        self.heal(None, "[]")  # unplugged, nothing to choose: no revive check
        self.assertIsNone(voice.SEEN)

    def test_a_scarlett_read_with_its_node_is_never_judged_by_a_second_pw_dump(self):  # read just now: no blip
        dumps = []
        chosen, cfg = self.heal(STATE, ABSENT)  # a later dump without the Scarlett: never NO_INPUT
        self.assertEqual((chosen, cfg), (voice.SCARLETT, selector()))
        voice.heal(ON, read=lambda choice: STATE, run=lambda cmd: True, dump=lambda: dumps.append(1) or "[]",
                   file=self.file, clock=lambda: 1000.0, on=lambda: True)
        self.assertEqual(len(dumps), 1)  # only the selector's revive check

    def test_the_scarlett_there_with_the_mic_not_routed_stops_never_falls_back(self):
        self.heal(None, ABSENT)  # on the PRO X
        self.ran.clear()
        chosen, cfg = self.heal(rerouted("PCM 1"), desk())
        self.assertEqual((chosen, cfg, self.ran), (voice.SCARLETT, None, [STOP_SELECT]))


class Sequences(unittest.TestCase):
    """The desk through heal and the routing policy's "mic" rule, as the app runs them: a move to a backup or to no mic
    is said on the heal that made it; the return to the Scarlett once it holds on two heals (a debounced poke, ~2 s)."""

    def setUp(self):
        Fallback.setUp(self)
        self.state, self.notes, self.labels = {}, [], []

    heal = Fallback.heal

    def tick(self, state, dump, on=True):
        self.heal(state, dump, on)
        self.state, actions = routing.next(self.state, {"kind": "mic", "source": voice.CHOSEN})
        for kind, value in actions:
            (self.notes if kind == "notify" else self.labels).append(value)

    def test_scarlett_out_is_said_on_the_heal_that_moved_it_and_once(self):
        self.tick(None, ABSENT)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.PRO_X_MIC]], [voice.PRO_X_MIC]))
        for _ in range(3):
            self.tick(None, ABSENT)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.PRO_X_MIC]], [voice.PRO_X_MIC]))

    def test_scarlett_out_with_the_headset_off_is_said_at_once(self):
        self.tick(None, ABSENT, on=False)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.BRIO_MIC]], [voice.BRIO_MIC]))

    def test_scarlett_back_is_said_too(self):
        for state, dump in ((None, ABSENT), (STATE, desk()), (STATE, desk())):
            self.tick(state, dump)
        self.assertEqual(self.labels, [voice.PRO_X_MIC, voice.SCARLETT])
        self.assertEqual(self.notes[-1], routing.MIC_SAID[voice.SCARLETT])

    def test_a_scarlett_replugged_between_two_heals_is_said_both_ways(self):
        self.tick(None, ABSENT, on=False)
        self.tick(STATE, desk())
        self.tick(STATE, desk())
        self.assertEqual(self.notes, [routing.MIC_SAID[voice.BRIO_MIC], routing.MIC_SAID[voice.SCARLETT]])

    def test_flapping_is_said_once_each_way_once_it_settles(self):
        for _ in range(10):  # unplugged and replugged between every heal
            self.tick(None, ABSENT)
            self.tick(STATE, desk())
        self.assertEqual(self.notes, [routing.MIC_SAID[voice.PRO_X_MIC]])  # the backup at once, the return not yet
        self.tick(STATE, desk())
        self.assertEqual(self.labels, [voice.PRO_X_MIC, voice.SCARLETT])

    def test_the_scarlett_unplugged_while_pw_dump_fails_stays_the_scarlett_never_no_mic(self):
        for _ in range(2):
            self.tick(None, "")
        self.assertEqual((voice.CHOSEN, self.notes), (voice.SCARLETT, []))

    def test_the_scarlett_card_there_but_no_pipewire_source_is_said_once_as_no_input(self):
        for _ in range(3):
            self.tick(NO_NODE, ABSENT)
        self.assertEqual((self.notes, self.labels), ([routing.MIC_SAID[voice.NO_INPUT]], [voice.NO_INPUT]))

    def test_mute_is_preserved_booth_mic_is_never_restarted_and_nothing_touches_its_mute(self):
        for state, dump, on in ((None, ABSENT, True), (None, ABSENT, False)) + ((STATE, desk(), True),) * 2:
            self.tick(state, dump, on)
        self.assertEqual(self.labels, [voice.PRO_X_MIC, voice.BRIO_MIC, voice.SCARLETT])
        self.assertFalse([c for c in self.ran if UNITS[0] in c and c != ACTIVE])
        self.assertFalse([c for c in self.ran if "wpctl" in c or "set-mute" in c])


class Release(unittest.TestCase):
    """A backup selector runs only while a Booth primary instance runs to watch it and say it: Booth's shutdown stops it
    and removes its config, and booth-mic-select.service's ExecCondition (may_select) never starts one without Booth."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "voice.conf"
        self.select = self.file.with_name(voice.SELECT_CONF)
        self.ran = []
        self.addCleanup(setattr, voice, "CLOSED", False)
        self.backups = [voice.choose(None, on, ABSENT, None)[1]["mic"] for on in (True, False)]  # PRO X, BRIO

    def on(self, mic):
        self.select.write_text(voice.build_selector(mic))

    def test_booth_quitting_stops_a_backup_selector_and_removes_its_config(self):
        for mic in self.backups:
            self.on(mic)
            self.ran.clear()
            voice.release(run=lambda cmd: self.ran.append(cmd) or True, file=self.file)
            self.assertEqual((self.ran, self.select.exists()), ([STOP_SELECT], False), mic["node"])

    def test_booth_quitting_removes_the_config_before_the_stop_under_the_lock(self):  # nothing restarts it between
        self.on(self.backups[1])
        seen = []
        voice.release(run=lambda cmd: seen.append((cmd, self.select.exists(), locked())) or True, file=self.file)
        self.assertEqual(seen, [(STOP_SELECT, False, True)])

    def test_booth_quitting_leaves_the_scarlett_selector_running(self):
        self.on(MIC)
        voice.release(run=lambda cmd: self.ran.append(cmd) or True, file=self.file)
        self.assertEqual((self.ran, self.select.read_text()), ([], voice.build_selector(MIC)))

    def test_no_heal_after_booth_quit_moves_the_selector_again(self):  # a heal still running as Booth shuts down
        self.on(MIC)
        voice.release(run=lambda cmd: True, file=self.file)
        voice.heal(ON, read=lambda choice: None, run=lambda cmd: self.ran.append(cmd) or True, dump=lambda: ABSENT,
                   file=self.file, clock=lambda: 1000.0, on=lambda: False)
        self.assertEqual((self.ran, self.select.read_text()), ([], voice.build_selector(MIC)))

    def test_the_selector_unit_starts_a_backup_only_while_booth_runs_else_removes_it(self):
        for mic in self.backups:
            self.on(mic)
            self.assertTrue(voice.may_select(self.select, owned=lambda: True))
            self.assertTrue(self.select.exists())
            self.assertFalse(voice.may_select(self.select, owned=lambda: False))  # logout, reboot, Booth closed
            self.assertFalse(self.select.exists())  # Booth Mic silent until Booth heals and says it

    def test_the_selector_unit_never_removes_a_config_booth_wrote_while_it_asked(self):
        """Login: the unit's ExecCondition reads a crash-left backup config and asks the bus; Booth starts meanwhile
        and writes the Scarlett's. That one stays: never removed by path on the strength of the old read."""
        for mic in self.backups:
            self.on(mic)

            def owned():
                voice._save(selector(), self.select)  # Booth, at login: back on the Scarlett
                return False
            self.assertFalse(voice.may_select(self.select, owned=owned))  # the backup it read never starts
            self.assertEqual(json.loads(self.select.read_text()), selector(), mic["node"])
            self.assertEqual(sorted(p.name for p in self.select.parent.iterdir()), [voice.SELECT_CONF])

    def test_the_selector_unit_always_starts_the_scarlett_and_never_no_config(self):
        self.on(MIC)
        self.assertTrue(voice.may_select(self.select, owned=lambda: False))
        self.assertTrue(self.select.exists())
        for text in ("not json", "{}"):
            self.select.write_text(text)
            self.assertFalse(voice.may_select(self.select, owned=lambda: True), text)
        self.select.unlink()
        self.assertFalse(voice.may_select(self.select, owned=lambda: True))

    def test_booth_runs_while_its_bus_name_is_owned(self):
        asked = []
        for answer, owned in (("(true,)\n", True), ("(false,)\n", False), ("", False)):  # "": no bus, no gdbus
            self.assertIs(voice._owned(run=lambda *cmd: asked.append(cmd) or answer), owned, answer)
        self.assertEqual(asked[0][-2:], ("org.freedesktop.DBus.NameHasOwner", "io.github.apheem.Booth"))

    def test_the_running_selector_is_named_from_its_config(self):
        for mic, name in ((MIC, voice.SCARLETT), (self.backups[0], voice.PRO_X_MIC), (self.backups[1], voice.BRIO_MIC)):
            self.on(mic)
            self.assertEqual(voice.current(self.file), name)
        self.select.unlink()
        self.assertIsNone(voice.current(self.file))


if __name__ == "__main__":
    unittest.main()
