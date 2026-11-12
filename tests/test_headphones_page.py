"""The Headphones page's wiring: its writes and polls run on a worker and always report back to the main loop, even
when the worker raises, so Writer.busy and the page's polling flag never stay stuck; "Use for all sound" keeps the
sink it replaces in the settings, saved on the main loop."""
import types
import unittest
from unittest import mock

from gi.repository import GObject

from booth import headset
from booth.ui import headphones
from tests.test_app import Inline
from tests.test_headset import DUMP, PRO_X, SCARLETT, default_sink, dump


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

    def test_an_overlapping_poll_is_refused_and_the_next_one_after_show_runs(self):
        reads = []
        page = mock.Mock(polling=False, read=lambda since: headphones.HeadphonesPage.read(page, since))
        page.writer.fresh.return_value = False  # show() only clears polling here
        with mock.patch.object(headphones.headset, "state", lambda: reads.append(1)):
            headphones.HeadphonesPage.poll(page)
            headphones.HeadphonesPage.poll(page)  # a slow pw-dump: the first one's read hasn't been shown yet
            self.assertEqual(len(reads), 1)
            f, a = self.idle.pop()
            headphones.HeadphonesPage.show(page, *a)
            headphones.HeadphonesPage.poll(page)
        self.assertEqual(len(reads), 2)

    def test_stop_removes_the_poll_timer(self):
        page, removed = mock.Mock(timer=7), []
        with mock.patch.object(headphones.GLib, "source_remove", removed.append):
            headphones.HeadphonesPage.stop(page)
        self.assertEqual(removed, [7])


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
        out, idle, _ = self.default((["wpctl", "set-default", "33"], None))
        self.assertEqual((out, idle), (["wpctl", "set-default", "33"], []))

    def test_off_forgets_the_remembered_sink_once_it_is_put_back(self):
        out, idle, page = self.default((["wpctl", "set-default", "176"], None), on=False)
        self.assertEqual((out, idle), (["wpctl", "set-default", "176"], [(page.remember, (None,))]))

    def test_off_that_does_nothing_forgets_nothing(self):
        self.assertEqual(self.default((None, None), on=False)[:2], (None, []))

    def test_remember_none_drops_the_saved_sink(self):
        page = mock.Mock(settings={"camera": {}, "headphones": {"previous_default": "scarlett"}})
        with mock.patch.object(headphones.store, "save") as save:
            headphones.HeadphonesPage.remember(page, None)
        self.assertEqual(page.settings["headphones"], {})
        save.assert_called_once_with(page.settings)

    def test_remember_saves_the_settings(self):
        page = mock.Mock(settings={"camera": {}, "headphones": {}})
        with mock.patch.object(headphones.store, "save") as save:
            headphones.HeadphonesPage.remember(page, "scarlett")
        self.assertEqual(page.settings["headphones"], {"previous_default": "scarlett"})
        save.assert_called_once_with(page.settings)

    def test_a_failed_save_says_so_on_the_row_and_a_good_one_clears_it(self):
        page = mock.Mock(settings={"camera": {}, "headphones": {}})
        with mock.patch.object(headphones.store, "save", side_effect=OSError(28, "No space left on device")):
            headphones.HeadphonesPage.remember(page, "scarlett")
        self.assertEqual(page.settings["headphones"], {"previous_default": "scarlett"})
        page.switch.set_subtitle.assert_called_once_with("Set, but not saved: No space left on device")
        with mock.patch.object(headphones.store, "save"):
            headphones.HeadphonesPage.remember(page, None)
        page.switch.set_subtitle.assert_called_with(headphones.SWITCH_SUBTITLE)


class _W:
    """A widget stand-in: GTK can't start without a display, so the page's rows are these."""
    active = False

    def __init__(self, *_args, **_kwargs):
        self.handlers = {}

    def connect(self, signal, handler):
        self.handlers[signal] = handler

    def get_active(self):
        return self.active

    def __getattr__(self, _name):  # set_hexpand, add_suffix, set_sensitive…
        return lambda *_args: None


# A real GObject with the page's own methods (a widget can't be made headless); __init__ is the page's own, run below.
Page = type("HeadphonesStandIn", (GObject.Object,), {
    **{k: v for k, v in vars(headphones.HeadphonesPage).items() if callable(v) and not k.startswith("__")},
    "add": lambda self, _group: None, "poll": lambda self: True})


class Click(unittest.TestCase):
    """A click on "Use for all sound" through the page's real __init__, on_default, Writer and send to the wpctl
    command it runs and the settings it saves."""

    def click(self, on, pw_dump, settings):
        ran, saved = [], []

        def run(*cmd):
            ran.append(list(cmd))
            return pw_dump if cmd == ("pw-dump",) else ""
        ns, send = types.SimpleNamespace, headset.send
        page = Page()
        with mock.patch.object(headphones, "super", lambda *a: ns(__init__=lambda: None), create=True), \
                mock.patch.object(headphones, "Adw", ns(PreferencesGroup=_W, ActionRow=_W, SwitchRow=_W)), \
                mock.patch.object(headphones, "Gtk", ns(Scale=ns(new_with_range=_W), Orientation=ns(HORIZONTAL=0))), \
                mock.patch.object(headphones.GLib, "timeout_add_seconds", lambda *a: 1), \
                mock.patch.object(headphones.GLib, "idle_add", lambda f, *a: f(*a)), \
                mock.patch.object(headphones.threading, "Thread", Inline), \
                mock.patch.object(headphones.headset, "send", lambda make: send(make, run)), \
                mock.patch.object(headphones.store, "save", saved.append):
            headphones.HeadphonesPage.__init__(page, settings)
            self.assertEqual([n for n in vars(page) if hasattr(headphones.HeadphonesPage, n)], [])  # none shadowed
            switch = page.rows[1]
            switch.active = on
            switch.handlers["notify::active"](switch, None)
        self.assertFalse(page.writer.busy)
        return ran, saved

    def test_on_makes_the_pro_x_the_default_and_saves_the_sink_it_replaced(self):
        settings = {"headphones": {}}
        ran, saved = self.click(True, DUMP, settings)
        self.assertEqual(ran, [["pw-dump"], ["wpctl", "set-default", "33"]])
        self.assertEqual((settings["headphones"], saved), ({"previous_default": SCARLETT}, [settings]))

    def test_off_puts_the_saved_sink_back_and_forgets_it(self):
        settings = {"headphones": {"previous_default": SCARLETT}}
        ran, saved = self.click(False, dump(lambda o: default_sink(o, PRO_X)), settings)
        self.assertEqual(ran, [["pw-dump"], ["wpctl", "set-default", "176"]])
        self.assertEqual((settings["headphones"], saved), ({}, [settings]))


if __name__ == "__main__":
    unittest.main()
