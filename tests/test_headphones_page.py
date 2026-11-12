"""The Headphones page's wiring: its writes and polls run on a worker and always report back to the main loop, even
when the worker raises, so Writer.busy and the page's polling flag never stay stuck; "Use for all sound" keeps the
sink it replaces in the settings, saved on the main loop."""
import unittest
from unittest import mock

from booth.ui import headphones
from tests.test_app import Inline


class Boom(Exception):
    pass


def boom(*_args):
    raise Boom


class Worker(unittest.TestCase):
    def setUp(self):
        self.idle = []
        for patch in (mock.patch.object(headphones.threading, "Thread", Inline),
                      mock.patch.object(headphones.GLib, "idle_add", lambda f, *a: self.idle.append((f, a)))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_write_reports_done_even_when_it_raises(self):
        done = object()
        page = mock.Mock(write=lambda make, d: headphones.HeadphonesPage.write(page, make, d))
        with mock.patch.object(headphones.headset, "send", boom), self.assertRaises(Boom):
            headphones.HeadphonesPage.spawn(page, None, done)
        self.assertEqual(self.idle, [(done, ())])

    def test_a_poll_reports_back_even_when_it_raises(self):
        page = mock.Mock(polling=False, read=lambda since: headphones.HeadphonesPage.read(page, since))
        page.writer.writes = 4
        with mock.patch.object(headphones.headset, "state", boom), self.assertRaises(Boom):
            headphones.HeadphonesPage.poll(page)
        self.assertEqual(self.idle, [(page.show, (None, 4))])  # show() clears polling


class UseForAllSound(unittest.TestCase):
    def default(self, result, on=True):
        page = mock.Mock(settings={"headphones": {"previous_default": "old"}})
        idle = []
        with mock.patch.object(headphones.GLib, "idle_add", lambda f, *a: idle.append((f, a))), \
                mock.patch.object(headphones.headset, "default_cmd", return_value=result) as cmd:
            out = headphones.HeadphonesPage.default(page, "DUMP", on)
        cmd.assert_called_once_with("DUMP", on, "old")  # the remembered sink as of this write, not of the click
        return out, idle, page

    def test_the_sink_it_replaces_is_remembered_on_the_main_loop(self):
        out, idle, page = self.default((["wpctl", "set-default", "33"], "scarlett"))
        self.assertEqual(out, ["wpctl", "set-default", "33"])
        self.assertEqual(idle, [(page.remember, ("scarlett",))])

    def test_nothing_to_remember_saves_nothing(self):
        out, idle, _ = self.default((["wpctl", "clear-default", "0"], None), on=False)
        self.assertEqual((out, idle), (["wpctl", "clear-default", "0"], []))

    def test_remember_saves_the_settings(self):
        page = mock.Mock(settings={"camera": {}, "headphones": {}})
        with mock.patch.object(headphones.store, "save") as save:
            headphones.HeadphonesPage.remember(page, "scarlett")
        self.assertEqual(page.settings["headphones"], {"previous_default": "scarlett"})
        save.assert_called_once_with(page.settings)


if __name__ == "__main__":
    unittest.main()
