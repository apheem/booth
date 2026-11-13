import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from booth import store


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "booth" / "settings.json"

    def test_missing_file_loads_defaults(self):
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())

    def test_round_trip(self):
        settings = store.defaults()
        settings["camera"]["zoom_absolute"] = 250
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["camera"], {"zoom_absolute": 250})
        self.assertEqual(json.loads(self.path.read_text())["version"], store.VERSION)

    def test_the_config_folder_is_never_group_writable_whatever_the_umask(self):
        old = os.umask(0o002)  # Ubuntu's default for a user with a private group
        try:
            store.save(store.defaults(), self.path)
        finally:
            os.umask(old)
        self.assertEqual(self.path.parent.stat().st_mode & 0o077, 0)  # it holds the config Booth Mic's pipewire loads

    def test_save_leaves_no_temp_file(self):
        store.save(store.defaults(), self.path)
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])

    def test_corrupt_file_is_backed_up_and_defaults_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"camera": {"zoom_absol')
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())
        backups = [p for p in self.path.parent.iterdir() if p.name.startswith("settings.json.corrupt")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), '{"camera": {"zoom_absol')

    def test_second_corrupt_file_keeps_the_first_backup(self):
        self.path.parent.mkdir(parents=True)
        for text in ("garbage one", "[1, 2]"):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults())
        texts = sorted(p.read_text() for p in self.path.parent.iterdir())
        self.assertEqual(texts, ["[1, 2]", "garbage one"])

    def test_wrong_shape_counts_as_corrupt(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"camera": 7}')
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())

    def test_non_integer_control_counts_as_corrupt(self):
        # Every saved control is clamped at launch: a string or null there would crash every start.
        self.path.parent.mkdir(parents=True)
        for text in ('{"camera": {"zoom_absolute": "abc"}}', '{"camera": {"zoom_absolute": null}}',
                     '{"camera": null}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)
            self.assertFalse(self.path.exists(), text)
        self.assertEqual(len(list(self.path.parent.iterdir())), 3)

    def test_the_headphones_previous_default_sink_round_trips(self):
        settings = store.load(self.path)
        settings["headphones"]["previous_default"] = "alsa_output.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00"
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["headphones"], settings["headphones"])

    def test_a_bad_headphones_value_counts_as_corrupt(self):
        # "Use for all sound" reads it on every turn-off: a wrong type there would break the switch
        self.path.parent.mkdir(parents=True)
        for text in ('{"headphones": 7}', '{"headphones": {"previous_default": 7}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)

    def test_the_routing_policys_saved_move_round_trips(self):
        settings = store.load(self.path)
        self.assertEqual(settings["routing"], {})  # no move saved
        settings["routing"] = {"moved": True, "direct": False, "direct_monitor": False}  # and the user's switch
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["routing"], {"moved": True, "direct": False, "direct_monitor": False})

    def test_a_bad_routing_value_counts_as_corrupt(self):
        # the policy writes Direct Monitor back from it: only a real on/off
        self.path.parent.mkdir(parents=True)
        for text in ('{"routing": 7}', '{"routing": {"moved": 1}}', '{"routing": {"direct": "off"}}',
                     '{"routing": {"direct": null}}', '{"routing": {"direct_monitor": "off"}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)
            self.assertFalse(self.path.exists(), text)

    def test_sidetone_and_auto_off_round_trip_at_their_limits(self):
        for sidetone, auto_off in ((0, 0), (1, 1), (128, 90)):
            settings = store.load(self.path)
            settings["headphones"].update(sidetone=sidetone, auto_off=auto_off)
            store.save(settings, self.path)
            self.assertEqual(store.load(self.path)["headphones"], {"sidetone": sidetone, "auto_off": auto_off})

    def test_a_bad_sidetone_or_auto_off_counts_as_corrupt(self):
        # both are sent to the headset on every power-on: only whole numbers in the headset's range
        self.path.parent.mkdir(parents=True)
        for text in ('{"headphones": {"sidetone": 129}}', '{"headphones": {"sidetone": -1}}',
                     '{"headphones": {"sidetone": "64"}}', '{"headphones": {"sidetone": 6.5}}', '{"headphones": {"sidetone": 64.0}}',
                     '{"headphones": {"sidetone": true}}', '{"headphones": {"auto_off": 91}}',
                     '{"headphones": {"auto_off": -1}}', '{"headphones": {"auto_off": null}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)
            self.assertFalse(self.path.exists(), text)

    def test_the_microphone_input_choice_round_trips(self):
        settings = store.load(self.path)
        self.assertEqual(settings["microphone"], {})  # no choice saved: XLR
        settings["microphone"]["input"] = "Instrument"
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["microphone"], {"input": "Instrument"})

    def test_a_bad_microphone_value_counts_as_corrupt(self):
        # the Microphone page selects the saved input by name: anything but a known input would break it
        self.path.parent.mkdir(parents=True)
        for text in ('{"microphone": 7}', '{"microphone": {"input": 7}}', '{"microphone": {"input": "Line"}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)

    def test_the_voice_switches_round_trip(self):
        settings = store.load(self.path)
        self.assertEqual(settings["voice"], {})  # nothing saved: processing and the rumble filter on
        settings["voice"].update(enabled=False, rumble=True)
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["voice"], {"enabled": False, "rumble": True})

    def test_a_bad_voice_value_counts_as_corrupt(self):
        # the Voice processing switches show and apply them as booleans
        self.path.parent.mkdir(parents=True)
        for text in ('{"voice": []}', '{"voice": {"enabled": 1}}', '{"voice": {"rumble": "off"}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)

    def test_the_voice_stages_and_knobs_round_trip(self):
        settings = store.load(self.path)
        settings["voice"].update(noise=False, leveling=True, limiter=False, noise_strength=0, leveling_amount=100,
                                 limiter_ceiling=-20.0)
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["voice"], settings["voice"])
        settings["voice"].update(noise_strength=100, leveling_amount=0, limiter_ceiling=0)
        store.save(settings, self.path)
        self.assertEqual(store.load(self.path)["voice"], settings["voice"])

    def test_a_bad_stage_or_knob_value_counts_as_corrupt(self):
        # the stage rows show and apply them; a knob out of range would reach the plugin
        self.path.parent.mkdir(parents=True)
        for text in ('{"voice": {"noise": 1}}', '{"voice": {"leveling": "on"}}', '{"voice": {"limiter": null}}',
                     '{"voice": {"noise_strength": 101}}', '{"voice": {"noise_strength": -1}}',
                     '{"voice": {"noise_strength": 50.5}}', '{"voice": {"leveling_amount": true}}',
                     '{"voice": {"leveling_amount": 200}}', '{"voice": {"limiter_ceiling": 0.5}}',
                     '{"voice": {"limiter_ceiling": -20.5}}', '{"voice": {"limiter_ceiling": NaN}}',
                     '{"voice": {"limiter_ceiling": false}}', '{"voice": {"limiter_ceiling": "-1"}}'):
            self.path.write_text(text)
            self.assertEqual(store.load(self.path), store.defaults(), text)

    def test_failed_save_keeps_the_previous_file(self):
        old = store.defaults()
        old["camera"]["zoom_absolute"] = 250
        store.save(old, self.path)

        def dump_then_crash(obj, f, **kw):
            f.write('{"version": 1, "cam')
            raise OSError("disk full")

        new = store.defaults()
        new["camera"]["zoom_absolute"] = 400
        with mock.patch("booth.store.json.dump", dump_then_crash), self.assertRaises(OSError):
            store.save(new, self.path)
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])  # no temp file left behind
        self.assertEqual(store.load(self.path)["camera"], {"zoom_absolute": 250})

    def test_too_deeply_nested_counts_as_corrupt(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[" * 200000)
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_settings_path_is_a_directory(self):
        self.path.mkdir(parents=True)
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())  # moved aside, so the next save can write the file
        store.save(store.defaults(), self.path)

    def test_unreadable_and_unrenamable_file_loads_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"camera": {}}')
        with mock.patch.object(Path, "open", side_effect=PermissionError(13, "denied")), \
                mock.patch("booth.store.os.replace", side_effect=PermissionError(13, "denied")):
            self.assertEqual(store.load(self.path), store.defaults())
        self.assertTrue(self.path.exists())

    def test_a_read_error_leaves_the_file_exactly_where_it_is(self):
        # EIO, EACCES (a root-owned file, a dotfiles symlink to an unreadable target), EMFILE: the file may be fine.
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"version": 1, "camera": {"zoom_absolute": 300}}')
        for code in (errno.EIO, errno.EACCES, errno.EMFILE):
            with mock.patch.object(Path, "open", side_effect=OSError(code, os.strerror(code))):
                self.assertEqual(store.load(self.path), store.defaults(), code)
            self.assertEqual(os.listdir(self.path.parent), ["settings.json"], code)
            self.assertEqual(self.path.read_text(), '{"version": 1, "camera": {"zoom_absolute": 300}}', code)

    def test_nested_too_deep_to_save_counts_as_corrupt(self):
        # Loads fine, but save's json.dump(indent=2) would raise RecursionError on every save after it.
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"version": 1, "camera": {}, "x": ' + "[" * 1500 + "]" * 1500 + "}")
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())
        store.save(store.defaults(), self.path)

    def test_invalid_utf8_counts_as_corrupt(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'{"camera": {"zoom\xff": 1}}')
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertFalse(self.path.exists())

    def test_missing_keys_take_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"version": 1}')
        self.assertEqual(store.load(self.path), store.defaults())
        self.assertTrue(self.path.exists())

    def test_defaults_are_fresh_copies(self):
        store.defaults()["camera"]["zoom_absolute"] = 5
        self.assertEqual(store.defaults()["camera"], {})

    def test_path_follows_xdg_config_home(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.dir)}):
            self.assertEqual(store.path(), self.dir / "booth" / "settings.json")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "", "HOME": str(self.dir)}):
            self.assertEqual(store.path(), self.dir / ".config" / "booth" / "settings.json")
        # The XDG spec says a relative path is invalid and must be ignored.
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "rel/config", "HOME": str(self.dir)}):
            self.assertEqual(store.path(), self.dir / ".config" / "booth" / "settings.json")


if __name__ == "__main__":
    unittest.main()
