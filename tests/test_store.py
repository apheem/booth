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
        self.assertEqual(store.load(self.path)["camera"], {"zoom_absolute": 250})

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


if __name__ == "__main__":
    unittest.main()
