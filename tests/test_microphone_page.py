"""The Microphone page's Scarlett group, through its real __init__ and handlers with stand-in widgets: 48V goes on only
after the user confirms the dialog, never from a poll; each row writes its own control by name; outside changes and
unplug/replug show up on the next poll; the Input choice is saved."""
import types
import unittest
from unittest import mock

from gi.repository import GObject

from booth import meter, scarlett, voice
from booth.ui import microphone
from booth.ui import window as ui_window
from tests.fixture import read
from tests.test_app import Inline
from tests.test_headset import DUMP

CONTENTS = read("scarlett/amixer-contents.txt")


def state(**values):
    """A poll's result from the captured controls, with {control name: first value} replaced."""
    ctrls = scarlett.parse_contents(CONTENTS)
    for name, v in values.items():
        ctrls[name]["values"] = [v]
    return {"ctrls": ctrls, "mic": scarlett.mic(ctrls, DUMP, "XLR")}


class Boom(Exception):
    pass


def boom(*_args):
    raise Boom


class _W:
    """A widget stand-in that, like GTK, notifies its handler when a value really changes."""

    def __init__(self, *_args, **kwargs):
        self.handlers, self.active, self.selected, self.sensitive = {}, False, 0, True
        self.description = self.subtitle = self.model = None
        self.responses = []

    def connect(self, signal, handler, *args):
        self.handlers[signal] = (handler, args)

    def emit(self, signal, *values):
        handler, args = self.handlers[signal]
        handler(self, *values, *args)

    def get_active(self):
        return self.active

    def set_active(self, on):
        if on != self.active:
            self.active = on
            self.emit("notify::active", None)

    def get_selected(self):
        return self.selected

    def set_selected(self, i):
        if i != self.selected:
            self.selected = i
            if "notify::selected" in self.handlers:
                self.emit("notify::selected", None)

    def set_model(self, model):
        self.model = model

    def set_sensitive(self, on):
        self.sensitive = on

    def set_description(self, text):
        self.description = text

    def set_subtitle(self, text):
        self.subtitle = text

    def add_response(self, rid, _label):
        self.responses.append(rid)

    def set_default_response(self, rid):
        self.default_response = rid

    def set_close_response(self, rid):
        self.close_response = rid

    def __getattr__(self, _name):  # add, set_response_appearance…
        return lambda *_args: None


class _Meter(_W):
    """A LevelMeter stand-in: what it was last told to show."""

    def set_level(self, peak, decay):
        self.level, self.said = (peak, decay), None

    def idle(self, text):
        self.level, self.said = None, text


class _Gst:
    """A meter.Meter stand-in: every want(record, visible) it was given."""

    def __init__(self, post, failed):
        self.post, self.failed, self.wants, self.stopped = post, failed, [], 0

    def want(self, record, visible):
        self.wants.append((record, visible))

    def stop(self):
        self.stopped += 1


class _Root(_W):
    suspended = False

    def is_suspended(self):
        return self.suspended


Page = type("MicrophoneStandIn", (GObject.Object,), {
    **{k: v for k, v in vars(microphone.MicrophonePage).items() if callable(v) and not k.startswith("__")},
    "add": lambda self, _group: None, "poll": lambda self: True, "mapped": False,
    "connect": lambda self, signal, handler, *a: vars(self).setdefault("handlers", {}).update({signal: (handler, a)}),
    "emit": _W.emit, "get_mapped": lambda self: self.mapped, "get_root": lambda self: self.root_})


class PageCase(unittest.TestCase):
    def setUp(self):
        self.ran, self.saved, self.dialogs, self.applied = [], [], [], []
        self.said, self.selecting = "live", True  # what voice.apply returns; whether Booth Mic's selector runs
        self.settings = {"microphone": {}}

        real_send = scarlett.send

        def send(make):
            real_send(make, lambda *cmd: self.ran.append(list(cmd)), lambda: "Gen")

        def dialog(**_kwargs):
            d = _W()
            d.present = lambda parent: self.dialogs.append(d)
            return d
        ns = types.SimpleNamespace
        for patch in (
                mock.patch.object(microphone, "super", lambda *a: ns(__init__=lambda: None), create=True),
                mock.patch.object(microphone, "Adw", ns(PreferencesGroup=_W, ActionRow=_W, SwitchRow=_W, ComboRow=_W,
                                                        AlertDialog=dialog, ResponseAppearance=ns(DESTRUCTIVE=2))),
                mock.patch.object(microphone, "Gtk", ns(StringList=ns(new=list), INVALID_LIST_POSITION=4294967295)),
                mock.patch.object(microphone.GLib, "timeout_add_seconds", lambda *a: 1),
                mock.patch.object(microphone.GLib, "idle_add", lambda f, *a: f(*a)),
                mock.patch.object(microphone.threading, "Thread", Inline),
                mock.patch.object(microphone.scarlett, "send", send),
                mock.patch.object(microphone.store, "save", self.saved.append),
                mock.patch.object(microphone.voice, "apply",
                                  lambda s, state: self.applied.append((dict(s["voice"]), state)) or self.said),
                mock.patch.object(microphone.voice, "selecting", lambda: self.selecting),
                mock.patch.object(microphone, "LevelMeter", _Meter),
                mock.patch.object(microphone.meter, "Meter", _Gst)):
            patch.start()
            self.addCleanup(patch.stop)
        self.page = Page()
        self.page.root_ = _Root()
        microphone.MicrophonePage.__init__(self.page, self.settings)
        self.assertEqual([n for n in vars(self.page) if hasattr(microphone.MicrophonePage, n)], [])  # none shadowed

    def show(self, s):
        self.page.show(s, self.page.writer.writes)

    def click(self, row, on):
        row.active = on
        row.emit("notify::active", None)

    def select(self, row, i):
        row.selected = i
        row.emit("notify::selected", None)

    def cset(self, name, value):
        return ["amixer", "-c", "Gen", "cset", f"name={name}", value]


class ScarlettGroup(PageCase):
    def test_turning_48v_on_asks_first_writes_nothing_and_leaves_the_switch_off(self):
        self.show(state())
        self.click(self.page.phantom, True)
        self.assertEqual(self.ran, [])
        self.assertFalse(self.page.phantom.active)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.dialogs[0].responses, ["cancel", "on"])

    def test_confirming_the_dialog_turns_48v_on(self):
        self.show(state())
        self.click(self.page.phantom, True)
        self.dialogs[0].emit("response", "on")
        self.assertEqual(self.ran, [self.cset(scarlett.PHANTOM, "on")])
        self.assertTrue(self.page.phantom.active)

    def test_cancelling_the_dialog_writes_nothing(self):
        self.show(state())
        self.click(self.page.phantom, True)
        self.dialogs[0].emit("response", "cancel")
        self.assertEqual(self.ran, [])
        self.assertFalse(self.page.phantom.active)

    def test_the_dialogs_default_and_close_are_cancel_and_only_on_writes(self):
        self.show(state())
        self.click(self.page.phantom, True)
        d = self.dialogs[0]
        self.assertEqual((d.default_response, d.close_response), ("cancel", "cancel"))
        d.emit("response", d.close_response)  # Escape or the window's close
        d.emit("response", d.default_response)  # Enter
        self.assertEqual((self.ran, self.page.phantom.active), ([], False))
        d.emit("response", "on")
        self.assertEqual(self.ran, [self.cset(scarlett.PHANTOM, "on")])

    def test_48v_off_is_written_without_asking(self):
        self.show(state(**{scarlett.PHANTOM: True}))
        self.click(self.page.phantom, False)
        self.assertEqual((self.ran, self.dialogs), ([self.cset(scarlett.PHANTOM, "off")], []))

    def test_a_poll_showing_outside_changes_writes_nothing(self):
        self.show(state(**{scarlett.PHANTOM: True, scarlett.DIRECT: True, scarlett.AIR: 2, scarlett.LEVEL: 1}))
        self.assertEqual((self.ran, self.dialogs, self.saved), ([], [], []))
        p = self.page
        self.assertEqual((p.phantom.active, p.direct.active, p.air.selected, p.level.selected), (True, True, 2, 1))
        self.show(state())  # and back
        self.assertEqual((self.ran, self.dialogs), ([], []))
        self.assertEqual((p.phantom.active, p.direct.active, p.air.selected, p.level.selected), (False, False, 1, 0))

    def test_the_combos_list_the_devices_own_items(self):
        self.show(state())
        self.assertEqual(self.page.air.model, ["Off", "Presence", "Presence + Drive"])
        self.assertEqual(self.page.level.model, ["Line", "Inst"])

    def test_each_row_writes_only_its_own_control(self):
        self.show(state())
        self.select(self.page.air, 2)
        self.select(self.page.level, 1)
        self.click(self.page.direct, True)
        self.assertEqual(self.ran, [self.cset(scarlett.AIR, "2"), self.cset(scarlett.LEVEL, "1"),
                                    self.cset(scarlett.DIRECT, "on")])

    def test_no_selection_writes_nothing(self):
        self.show(state())
        self.select(self.page.air, 4294967295)
        self.assertEqual(self.ran, [])

    def test_read_only_rows(self):
        self.show(state())
        self.assertEqual((self.page.firmware.subtitle, self.page.sync.subtitle), ("2188", "Locked"))
        self.assertEqual(self.page.input.subtitle, "Capture channel 2 (FR)")

    def test_unplugged_shows_plug_it_in_and_comes_back_on_replug(self):
        self.show(state())
        self.show(None)
        self.assertEqual(self.page.group.description, "Scarlett not connected — plug it in")
        self.assertFalse(any(row.sensitive for row in self.page.rows.values()))
        self.show(state())
        self.assertIsNone(self.page.group.description)
        self.assertTrue(all(row.sensitive for row in self.page.rows.values()))
        self.assertEqual(self.ran, [])

    def test_input_defaults_to_xlr_and_the_choice_is_saved(self):
        self.assertEqual(self.page.input.selected, 0)
        self.select(self.page.input, 1)
        self.assertEqual((self.settings["microphone"], self.saved, self.ran), ({"input": "Instrument"}, [self.settings], []))

    def test_a_failed_save_says_so_on_the_input_row_until_a_good_one(self):
        self.show(state())
        with mock.patch.object(microphone.store, "save", side_effect=OSError(30, "Read-only file system")):
            self.select(self.page.input, 1)
        self.assertEqual(self.settings["microphone"], {"input": "Instrument"})
        self.assertEqual(self.page.input.subtitle, "Set, but not saved: Read-only file system")
        self.show(state())  # the next poll keeps the note
        self.assertEqual(self.page.input.subtitle, "Set, but not saved: Read-only file system")
        self.select(self.page.input, 0)  # saved this time
        self.show(state())
        self.assertEqual(self.page.input.subtitle, "Capture channel 2 (FR)")

    def test_a_saved_input_is_selected_at_start(self):
        page = Page()
        microphone.MicrophonePage.__init__(page, {"microphone": {"input": "Instrument"}})
        self.assertEqual(page.input.selected, 1)


    def real_poll(self, read):
        """The page's own poll() over a stand-in scarlett.read; returns the main-loop queue its show() lands on."""
        idle = []
        for patch in (mock.patch.object(microphone.scarlett, "read", read),
                      mock.patch.object(microphone.GLib, "idle_add", lambda f, *a: idle.append((f, a)))):
            patch.start()
            self.addCleanup(patch.stop)
        self.page.poll = types.MethodType(microphone.MicrophonePage.poll, self.page)
        return idle

    def test_an_overlapping_poll_is_refused_and_the_next_one_after_show_runs(self):
        reads = []
        idle = self.real_poll(lambda choice: reads.append(choice) or state(**{scarlett.DIRECT: True}))
        self.page.poll()
        self.page.poll()  # the first one's read hasn't been shown yet
        self.assertEqual(reads, ["XLR"])
        f, a = idle.pop()
        f(*a)
        self.assertTrue(self.page.direct.active)
        self.page.poll()  # consecutive polls both run
        self.assertEqual(reads, ["XLR", "XLR"])
        self.assertEqual(len(idle), 1)

    def test_a_poll_that_read_before_a_write_landed_is_not_shown(self):
        self.show(state())
        since = self.page.writer.writes
        self.select(self.page.air, 2)  # the write lands after the poll read Air = 1
        self.page.show(state(), since)
        self.assertEqual(self.page.air.selected, 2)
        self.assertFalse(self.page.polling)
        self.page.show(state(), self.page.writer.writes)  # the next poll shows it
        self.assertEqual(self.page.air.selected, 1)

    def test_changing_the_input_polls_again(self):
        reads = []
        self.real_poll(lambda choice: reads.append(choice))
        self.select(self.page.input, 1)
        self.assertEqual(reads, ["Instrument"])

    def test_stop_removes_the_poll_timer(self):
        removed = []
        with mock.patch.object(microphone.GLib, "source_remove", removed.append):
            self.page.stop()
        self.assertEqual(removed, [self.page.timer])


class VoiceGroup(PageCase):
    """The Voice processing group: the master switch and the rumble filter, saved and applied live to Booth Mic."""

    def test_both_switches_start_on(self):
        self.assertEqual((self.page.master.active, self.page.rumble.active), (True, True))
        self.assertEqual(self.settings["voice"], {})

    def test_saved_switches_are_shown_at_start_and_rumble_waits_on_the_master(self):
        page = Page()
        microphone.MicrophonePage.__init__(page, {"microphone": {}, "voice": {"enabled": False, "rumble": False}})
        self.assertEqual((page.master.active, page.rumble.active, page.rumble.sensitive), (False, False, False))

    def test_a_switch_is_saved_and_applied_never_moving_the_selector(self):
        self.show(state())
        self.click(self.page.rumble, False)
        self.assertEqual((self.settings["voice"], self.saved), ({"rumble": False}, [self.settings]))
        self.assertEqual(self.applied, [({"rumble": False}, None)])  # the selector follows the polls, not a switch
        self.click(self.page.master, False)
        self.assertEqual(self.applied[-1], ({"rumble": False, "enabled": False}, None))
        self.assertFalse(self.page.rumble.sensitive)
        self.assertEqual(self.ran, [])  # never the Scarlett

    def test_a_failed_save_says_so_on_the_row_and_still_applies(self):
        with mock.patch.object(microphone.store, "save", side_effect=OSError(28, "No space left on device")):
            self.click(self.page.master, False)
        self.assertEqual(self.page.master.subtitle, "Set, but not saved: No space left on device")
        self.assertEqual(len(self.applied), 1)
        self.click(self.page.master, True)
        self.assertEqual(self.page.master.subtitle, microphone.MASTER)

    def test_a_change_booth_mic_didnt_take_says_so_on_the_row(self):
        self.said = "not applied: Booth Mic didn't take the change"
        self.click(self.page.rumble, False)
        self.assertEqual(self.page.rumble.subtitle, "Set, but not applied: Booth Mic didn't take the change")
        self.said = "live"
        self.click(self.page.rumble, True)
        self.assertEqual(self.page.rumble.subtitle, microphone.RUMBLE)

    def test_a_poll_with_the_selector_down_says_booth_mic_is_waiting_for_the_scarlett(self):
        s, self.selecting = state(), False  # the Scarlett just came back: Booth restarts the selector within 10 s
        idle = self.real_poll(lambda choice: s)
        self.page.poll()
        f, a = idle[0]
        f(*a)
        self.assertEqual(self.page.voice.description, voice.WAITING)

    def test_each_poll_says_where_booth_mic_takes_its_input(self):
        self.show(state())
        self.assertEqual(self.page.voice.description, voice.describe(state()))
        self.show(state(**{"PCM 02 Capture Enum": 5}))
        self.assertEqual(self.page.voice.description, voice.NOT_ROUTED)
        self.show(None)
        self.assertEqual(self.page.voice.description, voice.WAITING)

    def test_each_poll_brings_booth_mics_config_up_to_date(self):
        s = state()
        idle = self.real_poll(lambda choice: s)
        self.page.poll()
        self.assertEqual(self.applied, [({}, s)])
        self.assertEqual(len(idle), 1)

    def test_a_not_routed_poll_reaches_apply_with_the_scarlett_there(self):
        s = state(**{"PCM 02 Capture Enum": 5})  # capture 2 rerouted: Booth Mic's selector must stop
        self.assertIsNone(s["mic"])
        self.real_poll(lambda choice: s)
        self.page.poll()
        self.assertEqual(self.applied, [({}, s)])

    def test_a_selector_that_wont_stop_says_booth_mic_may_still_take_the_old_channel(self):
        s = state(**{"PCM 02 Capture Enum": 5})  # rerouted, and systemctl stop failed: the old selector still feeds
        self.said = "not stopped: Booth Mic's source still takes the mic's old channel"
        idle = self.real_poll(lambda choice: s)
        self.page.poll()
        f, a = idle[0]
        f(*a)
        self.assertEqual(self.page.voice.description, voice.STALE)

    def test_an_unplugged_poll_applies_nothing(self):
        self.real_poll(lambda choice: None)
        self.page.poll()
        self.assertEqual(self.applied, [])

    real_poll = ScarlettGroup.real_poll


class Meters(PageCase):
    """The before-meter and the sidebar meter: what they say, and when the Scarlett is metered."""

    def setUp(self):
        super().setUp()
        self.meters, self.gst = (self.page.bar, self.page.mini), self.page.meter

    def map(self, widget, on=True):
        widget.mapped = on
        widget.emit("map" if on else "unmap")

    def test_the_page_and_the_sidebar_meter_both_show_the_mic_channel(self):
        self.page.meter.post(-14.0, -10.0)
        self.assertEqual([m.level for m in self.meters], [(-14.0, -10.0)] * 2)

    def test_a_routed_mic_is_metered_with_the_coaching_text(self):
        s = state()
        self.show(s)
        self.assertEqual(self.gst.wants[-1], (s["mic"], False))  # not on screen yet
        self.assertEqual(self.page.levels.description, meter.COACHING)

    def test_unplugged_says_so_and_runs_nothing(self):
        self.show(None)
        self.assertEqual(self.gst.wants[-1][0], None)
        self.assertEqual([m.said for m in self.meters], [scarlett.UNPLUGGED] * 2)
        self.assertEqual(self.page.levels.description, scarlett.UNPLUGGED)

    def test_not_routed_says_so_and_runs_nothing(self):
        self.show(state(**{"PCM 02 Capture Enum": 5}))
        self.assertEqual(self.gst.wants[-1][0], None)
        self.assertEqual([m.said for m in self.meters], [meter.NOT_ROUTED] * 2)

    def test_runs_while_the_page_is_on_screen(self):
        s = state()
        self.show(s)
        self.map(self.page)
        self.assertEqual(self.gst.wants[-1], (s["mic"], True))
        self.map(self.page, False)
        self.assertEqual(self.gst.wants[-1], (s["mic"], False))

    def test_runs_while_only_the_sidebar_meter_is_on_screen(self):
        self.show(state())
        self.page.mini.get_mapped = lambda: self.page.mini.mapped
        self.page.mini.get_root = lambda: self.page.root_
        self.map(self.page.mini)
        self.assertTrue(self.gst.wants[-1][1])
        self.map(self.page.mini, False)
        self.assertFalse(self.gst.wants[-1][1])

    def test_a_minimised_window_stops_it(self):
        self.show(state())
        self.map(self.page)
        self.page.root_.suspended = True
        self.page.root_.emit("notify::suspended", None)
        self.assertFalse(self.gst.wants[-1][1])

    def test_a_failure_says_so_on_both_meters(self):
        self.page.meter.failed(meter.UNAVAILABLE)
        self.assertEqual([m.said for m in self.meters], [meter.UNAVAILABLE] * 2)

    def test_closing_the_window_stops_the_meter(self):
        with mock.patch.object(microphone.GLib, "source_remove", lambda _t: None):
            self.page.stop()
        self.assertEqual(self.gst.stopped, 1)


class WindowClose(unittest.TestCase):
    def test_closing_the_window_stops_the_microphone_pages_poll(self):
        ns, win, booth = types.SimpleNamespace, mock.Mock(), mock.Mock()
        with mock.patch.object(ui_window, "super", lambda *a: ns(__init__=lambda **k: None), create=True), \
                mock.patch.object(ui_window, "Adw"), mock.patch.object(ui_window, "Gtk"), \
                mock.patch.object(ui_window, "CameraPage"), mock.patch.object(ui_window, "HeadphonesPage"), \
                mock.patch.object(ui_window, "MicrophonePage") as mic:
            ui_window.Window.__init__(win, booth, {}, None, {})
        for c in win.connect.call_args_list:
            if c.args[0] == "close-request":
                c.args[1](win)
        mic.return_value.stop.assert_called_once_with()
        self.assertIs(win.pages[1][3], mic.return_value.mini)  # the sidebar's Microphone row shows its meter

    def test_closing_the_window_stops_the_headphones_pages_poll(self):
        ns, win, booth = types.SimpleNamespace, mock.Mock(), mock.Mock()
        with mock.patch.object(ui_window, "super", lambda *a: ns(__init__=lambda **k: None), create=True), \
                mock.patch.object(ui_window, "Adw"), mock.patch.object(ui_window, "Gtk"), \
                mock.patch.object(ui_window, "CameraPage"), mock.patch.object(ui_window, "MicrophonePage"), \
                mock.patch.object(ui_window, "HeadphonesPage") as hp:
            ui_window.Window.__init__(win, booth, {}, None, {})
        for c in win.connect.call_args_list:
            if c.args[0] == "close-request":
                c.args[1](win)
        hp.return_value.stop.assert_called_once_with()
        self.assertIs(hp.call_args.args[2], booth.headset_monitor)  # the page follows the app's one battery poll


class Worker(unittest.TestCase):
    def setUp(self):
        self.idle = []
        for patch in (mock.patch.object(microphone.threading, "Thread", Inline),
                      mock.patch.object(microphone.GLib, "idle_add", lambda f, *a: self.idle.append((f, a)))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_write_reports_done_even_when_it_raises(self):
        done = object()
        page = mock.Mock(write=lambda make, d: microphone.MicrophonePage.write(page, make, d))
        with mock.patch.object(microphone.scarlett, "send", boom), self.assertRaises(Boom):
            microphone.MicrophonePage.spawn(page, None, done)
        self.assertEqual(self.idle, [(done, ())])

    def test_a_poll_reports_back_even_when_it_raises(self):
        page = mock.Mock(polling=False, settings={"microphone": {}},
                         read=lambda since: microphone.MicrophonePage.read(page, since))
        page.writer.writes = 4
        with mock.patch.object(microphone.scarlett, "read", boom), self.assertRaises(Boom):
            microphone.MicrophonePage.poll(page)
        self.assertEqual(self.idle, [(page.show, (None, 4, True, ""))])


if __name__ == "__main__":
    unittest.main()
