"""The Microphone page's Scarlett group, through its real __init__ and handlers with stand-in widgets: 48V goes on only
after the user confirms the dialog, never from a poll; each row writes its own control by name; outside changes and
unplug/replug show up on the next poll; the Input choice is saved."""
import types
import unittest
from unittest import mock

from gi.repository import GObject

from booth import meter, monitor, plugins, scarlett, voice
from booth.ui import microphone
from booth.ui import window as ui_window
from tests.fixture import read
from tests.test_app import Inline
from tests.test_fallback import locked
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
        self.description, self.subtitle, self.model = None, kwargs.get("subtitle"), None
        self.title, self.adjustment = kwargs.get("title"), kwargs.get("adjustment")
        self.value, self.rows = (self.adjustment or {}).get("value"), []
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

    def get_value(self):
        return self.value

    def set_value(self, value):
        if value != self.value:
            self.value = value
            self.emit("notify::value", None)

    def add_row(self, row):
        self.rows.append(row)

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
    """A LevelMeter stand-in: its name, and what it was last told to show."""

    def __init__(self, name, *_args, **_kwargs):
        super().__init__()
        self.name = name

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
        self.running = True  # voice.running: Booth Mic's own process is in PipeWire
        self.found = dict.fromkeys(plugins.MANIFEST, "/verified")  # voice.loaded: every plugin verified and running
        self.settings = {"microphone": {}}
        self.monitored, self.monitor_said = [], "on"  # every monitor.sync's switch; what it returns
        self.monitor_on = []  # every monitor.sync's `on`: the PRO X headset's HeadsetMonitor.on
        self.headset = types.SimpleNamespace(on=True)
        self.labels = []  # every Gtk.Label the page made, as {its keyword arguments}

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
                                                        SpinRow=_W, ExpanderRow=_W, AlertDialog=dialog,
                                                        ResponseAppearance=ns(DESTRUCTIVE=2))),
                mock.patch.object(microphone, "Gtk", ns(StringList=ns(new=list), INVALID_LIST_POSITION=4294967295,
                                                        Box=_W, Label=lambda **kw: self.labels.append(kw),
                                                        Adjustment=ns(new=lambda value, lower, upper, *_: dict(
                                                            value=value, lower=lower, upper=upper)))),
                mock.patch.object(microphone.GLib, "timeout_add_seconds", lambda *a: 1),
                mock.patch.object(microphone.GLib, "idle_add", lambda f, *a: f(*a)),
                mock.patch.object(microphone.threading, "Thread", Inline),
                mock.patch.object(microphone.scarlett, "send", send),
                mock.patch.object(microphone.store, "save", self.saved.append),
                mock.patch.object(microphone.voice, "apply",
                                  lambda s, state: self.applied.append((dict(s["voice"]), state)) or self.said),
                mock.patch.object(microphone.voice, "selecting", lambda: self.selecting),
                mock.patch.object(microphone.voice, "running", lambda: self.running, create=True),
                mock.patch.object(microphone.voice, "current", lambda: None),  # the selector config on disk: none
                # every plugin verifies; self.found: those also in the running chain (voice.loaded), the page's rows
                mock.patch.object(microphone.voice, "verified", lambda: dict.fromkeys(plugins.MANIFEST, "/verified")),
                mock.patch.object(microphone.voice, "loaded", lambda found=None: self.found, create=True),
                mock.patch.object(microphone, "LevelMeter", _Meter),
                mock.patch.object(microphone.meter, "Meter", _Gst),
                mock.patch.object(microphone.monitor, "LAST", None, create=True),  # no sync yet this run
                mock.patch.object(microphone.monitor, "sync",
                                  lambda settings, on: self.monitor_on.append(on)
                                  or self.monitored.append(dict(settings["voice"])) or self.monitor_said)):
            patch.start()
            self.addCleanup(patch.stop)
        self.page = Page()
        self.page.root_ = _Root()
        microphone.MicrophonePage.__init__(self.page, self.settings, self.headset)
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

    def test_page_poll_reads_and_applies_under_the_voice_lock_so_a_heal_never_lands_in_between(self):
        held = []  # a stale read applied after heal moved to a backup would put the Scarlett's config back on a gone node
        self.real_poll(lambda choice: held.append(locked()) or state())
        self.page.poll()
        self.assertEqual((held, len(self.applied)), ([True], 1))

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


class DirectMonitorSetting(PageCase):
    """"Turn on Direct Monitor when the PRO X turns off": the routing policy's switch, saved, never a Scarlett write."""

    def test_it_starts_on_and_shows_the_saved_value(self):
        self.assertTrue(self.page.auto.active)
        self.assertEqual(self.page.auto.title, "Turn on Direct Monitor when the PRO X turns off")
        page = Page()
        microphone.MicrophonePage.__init__(page, {"microphone": {}, "routing": {"direct_monitor": False}})
        self.assertFalse(page.auto.active)

    def test_a_click_is_saved_and_never_writes_the_scarlett(self):
        self.show(state())
        self.click(self.page.auto, False)
        self.assertEqual((self.settings["routing"], self.saved), ({"direct_monitor": False}, [self.settings]))
        self.assertEqual(self.ran, [])
        self.settings["routing"] = {"moved": True}  # the routing saved its record since: a new dict
        self.click(self.page.auto, True)
        self.assertEqual(self.settings["routing"], {"moved": True, "direct_monitor": True})

    def test_a_failed_save_says_so_on_the_row(self):
        with mock.patch.object(microphone.store, "save", side_effect=OSError(28, "No space left on device")):
            self.click(self.page.auto, False)
        self.assertEqual(self.page.auto.subtitle, "Set, but not saved: No space left on device")
        self.click(self.page.auto, True)
        self.assertEqual(self.page.auto.subtitle, microphone.AUTO)


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

    def test_a_poll_with_booth_mic_stopped_says_so_never_blames_the_scarlett(self):
        s, self.selecting, self.running = state(), False, False  # its pre-start failed, or the user stopped it
        idle = self.real_poll(lambda choice: s)
        self.page.poll()
        f, a = idle[0]
        f(*a)
        self.assertEqual(self.page.voice.description, voice.STOPPED)

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

    def unplugged_poll(self, source):
        idle = self.real_poll(lambda choice: None)
        with mock.patch.object(microphone.voice, "current", lambda: source):
            self.page.poll()
        f, a = idle[0]
        f(*a)
        return self.page.voice.description

    def test_unplugged_on_a_backup_says_booth_mic_runs_from_it_never_waiting(self):
        for source in (voice.PRO_X_MIC, voice.BRIO_MIC):
            said = self.unplugged_poll(source)
            self.assertEqual(said, voice.describe(None, source=source))
            self.assertIn(f"from the {source}", said)
            self.assertNotEqual(said, voice.WAITING)
        self.assertEqual(self.unplugged_poll(voice.SCARLETT), voice.WAITING)  # its own selector: back when it is

    def test_unplugged_with_booth_mic_stopped_says_stopped_and_never_meters_it(self):
        self.running = False
        self.assertEqual(self.unplugged_poll(voice.BRIO_MIC), voice.STOPPED)
        self.assertEqual(self.page.after_record, None)
        self.assertEqual(self.page.after.said, voice.STOPPED)

    real_poll = ScarlettGroup.real_poll


class StageRows(PageCase):
    """Noise suppression, leveling and the limiter: a switch, one knob and the raw controls each, saved and applied live;
    a plugin missing or changed shows so and is bypassed."""

    def stage(self, key):
        return self.page.stages[key]

    def test_each_stage_has_a_switch_a_knob_and_its_raw_controls_in_chain_order(self):
        self.assertEqual(list(self.page.stages), ["noise", "leveling", "limiter"])
        self.assertEqual([(s.switch.title, s.knob.title) for s in self.page.stages.values()],
                         [("Noise Suppression", "Strength"), ("Leveling", "Amount"), ("Limiter", "Ceiling (dB)")])
        self.assertEqual([s.details.title for s in self.page.stages.values()], ["Show details"] * 3)
        self.assertEqual([r.title for r in self.stage("leveling").details.rows], list(voice.amount(50)))

    def test_stages_start_on_with_the_default_knobs(self):
        self.assertEqual([s.switch.active for s in self.page.stages.values()], [True] * 3)
        self.assertEqual([s.knob.value for s in self.page.stages.values()], [50, 50, -1.0])
        self.assertEqual([r.subtitle for r in self.stage("limiter").details.rows], ["0", "-1", "0.05"])
        self.assertEqual(self.settings["voice"], {})  # nothing saved by showing them

    def test_saved_values_are_shown_at_start(self):
        page = Page()
        microphone.MicrophonePage.__init__(page, {"microphone": {}, "voice": {
            "noise": False, "noise_strength": 80, "limiter_ceiling": -3.5}})
        self.assertEqual((page.stages["noise"].switch.active, page.stages["noise"].knob.value,
                          page.stages["limiter"].knob.value), (False, 80, -3.5))
        self.assertEqual(page.stages["noise"].details.rows[0].subtitle, "76")

    def test_the_knob_ranges_stay_inside_what_the_settings_accept(self):
        for s, (lo, hi) in zip(self.page.stages.values(), ((0, 100), (0, 100), (-12, 0))):
            self.assertEqual((s.knob.adjustment["lower"], s.knob.adjustment["upper"]), (lo, hi))

    def test_a_stage_switch_is_saved_and_applied_live_never_moving_the_selector(self):
        self.click(self.stage("noise").switch, False)
        self.assertEqual((self.settings["voice"], self.saved), ({"noise": False}, [self.settings]))
        self.assertEqual(self.applied, [({"noise": False}, None)])

    def test_a_knob_is_saved_applied_and_its_raw_controls_follow(self):
        self.stage("noise").knob.set_value(80.0)
        self.assertEqual(self.settings["voice"], {"noise_strength": 80})
        self.assertIs(type(self.settings["voice"]["noise_strength"]), int)  # the settings accept whole numbers only
        self.assertEqual(self.applied, [({"noise_strength": 80}, None)])
        self.assertEqual(self.stage("noise").details.rows[0].subtitle, "76")
        self.stage("limiter").knob.set_value(-2.5)
        self.assertEqual(self.settings["voice"]["limiter_ceiling"], -2.5)
        self.assertEqual(self.stage("limiter").details.rows[1].subtitle, "-2.5")

    def test_a_knob_that_cant_be_saved_says_so_and_still_applies(self):
        with mock.patch.object(microphone.store, "save", side_effect=OSError(28, "No space left on device")):
            self.stage("leveling").knob.set_value(70.0)
        self.assertEqual(self.stage("leveling").knob.subtitle, "Set, but not saved: No space left on device")
        self.assertEqual(len(self.applied), 1)

    def test_a_plugin_missing_or_changed_says_so_and_the_stage_is_bypassed(self):
        self.page.show(state(), self.page.writer.writes, True, "", {"leveling": microphone.MISSING})
        row = self.stage("leveling")
        self.assertEqual(row.switch.subtitle, microphone.MISSING)
        self.assertEqual((row.switch.active, row.switch.sensitive, row.knob.sensitive), (False, False, False))
        self.assertEqual((self.settings["voice"], self.saved, self.applied), ({}, [], []))  # shown, never saved
        self.assertTrue(self.stage("noise").switch.sensitive)
        self.page.show(state(), self.page.writer.writes, True, "", {})  # ./install.sh fetched it again
        self.assertEqual((row.switch.active, row.switch.sensitive, row.knob.sensitive), (True, True, True))
        self.assertNotEqual(row.switch.subtitle, microphone.MISSING)

    def poll_plugins(self):
        idle = ScarlettGroup.real_poll(self, lambda choice: state())
        self.page.poll()
        f, a = idle[0]
        f(*a)
        return {key: stage.switch.subtitle for key, stage in self.page.stages.items()}

    def test_each_poll_checks_the_plugins(self):
        # RNNoise doesn't verify (missing, or changed since): bypassed, ./install.sh fetches it again
        self.found = {k: v for k, v in self.found.items() if k != "librnnoise_ladspa.so"}
        with mock.patch.object(microphone.voice, "verified", lambda: self.found):
            subtitles = self.poll_plugins()
        self.assertEqual(subtitles["noise"], microphone.MISSING)
        self.assertEqual(subtitles["limiter"], microphone.STAGES["limiter"][1])

    def test_a_plugin_that_verifies_but_booth_mic_hasnt_loaded_yet_says_it_loads_at_its_restart(self):
        # an install while an old Booth ran: the plugins are in place, Booth Mic still runs the old chain
        self.found = {k: v for k, v in self.found.items() if k != "librnnoise_ladspa.so"}
        subtitles = self.poll_plugins()
        self.assertEqual(subtitles["noise"], microphone.PENDING)
        self.assertIn("loads when Booth Mic restarts", microphone.PENDING)
        self.assertNotIn("missing", microphone.PENDING.lower())
        row = self.stage("noise")
        self.assertEqual((row.switch.active, row.switch.sensitive), (False, False))  # not in the chain yet: dry

    def test_the_master_switch_off_greys_every_stage(self):
        self.click(self.page.master, False)
        self.assertEqual([(s.switch.sensitive, s.knob.sensitive) for s in self.page.stages.values()],
                         [(False, False)] * 3)
        self.click(self.page.master, True)
        self.assertEqual([(s.switch.sensitive, s.knob.sensitive) for s in self.page.stages.values()],
                         [(True, True)] * 3)


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

    def test_each_bar_names_itself_and_is_labelled_by_its_visible_label(self):
        p = self.page
        self.assertEqual((p.bar.name, p.after.name, p.mini.name),
                         ("Before processing", "After processing (Booth Mic)", "Input level"))
        # mnemonic_widget: GTK sets the bar's LABELLED_BY relation to the label
        self.assertEqual([(kw["label"], kw["mnemonic_widget"]) for kw in self.labels],
                         [("Before", p.bar), ("After", p.after)])

    def test_a_failure_says_so_on_both_meters(self):
        self.page.meter.failed(meter.UNAVAILABLE)
        self.assertEqual([m.said for m in self.meters], [meter.UNAVAILABLE] * 2)

    def test_closing_the_window_stops_the_meter(self):
        with mock.patch.object(microphone.GLib, "source_remove", lambda _t: None):
            self.page.stop()
        self.assertEqual(self.gst.stopped, 1)


class AfterMeter(PageCase):
    """Booth Mic's after-meter: the same Meter on booth_mic, mono, on the page only, never while it isn't on screen."""

    def setUp(self):
        super().setUp()
        self.gst = self.page.after_meter

    map = Meters.map

    def test_it_reads_booth_mic_mono_by_name(self):
        self.assertEqual(microphone.AFTER, {"node": "booth_mic", "channel": 1, "channels": 1})
        self.assertIn('target-object="booth_mic"', meter.launch(microphone.AFTER["node"], microphone.AFTER["channels"]))

    def test_runs_only_while_the_page_is_on_screen(self):
        self.show(state())
        self.assertEqual(self.gst.wants[-1], (microphone.AFTER, False))
        self.map(self.page)
        self.assertEqual(self.gst.wants[-1], (microphone.AFTER, True))
        self.map(self.page, False)
        self.assertEqual(self.gst.wants[-1], (microphone.AFTER, False))

    def test_mapping_the_page_before_the_first_poll_never_opens_booth_mic(self):
        self.map(self.page)  # Booth Mic not yet known to be running
        self.assertEqual(self.gst.wants[-1], (None, True))
        self.show(state())
        self.assertEqual(self.gst.wants[-1], (microphone.AFTER, True))

    def test_the_sidebar_meter_alone_never_opens_booth_mic(self):
        self.show(state())
        self.page.mini.get_mapped = lambda: self.page.mini.mapped
        self.page.mini.get_root = lambda: self.page.root_
        self.map(self.page.mini)
        self.assertTrue(self.page.meter.wants[-1][1])  # the before-meter runs for the sidebar
        self.assertFalse(self.gst.wants[-1][1])

    def test_a_minimised_window_stops_it(self):
        self.show(state())
        self.map(self.page)
        self.page.root_.suspended = True
        self.page.root_.emit("notify::suspended", None)
        self.assertFalse(self.gst.wants[-1][1])

    def test_its_levels_go_to_the_after_bar_only(self):
        self.gst.post(-20.0, -18.0)
        self.assertEqual(self.page.after.level, (-20.0, -18.0))
        self.assertIsNone(vars(self.page.bar).get("level"))
        self.gst.failed(meter.UNAVAILABLE)
        self.assertEqual((self.page.after.said, vars(self.page.bar).get("level")), (meter.UNAVAILABLE, None))

    def test_booth_mic_stopped_says_so_and_runs_nothing(self):
        self.page.show(state(), self.page.writer.writes, True, "", None, False)
        self.assertEqual(self.gst.wants[-1][0], None)
        self.assertEqual(self.page.after.said, voice.STOPPED)

    def test_an_unplugged_scarlett_still_meters_booth_mic(self):
        self.show(None)  # Booth Mic carries silence (or, with #20, a backup mic): it is what the call hears
        self.assertEqual(self.gst.wants[-1][0], microphone.AFTER)

    def test_closing_the_window_stops_both_meters(self):
        with mock.patch.object(microphone.GLib, "source_remove", lambda _t: None):
            self.page.stop()
        self.assertEqual((self.page.meter.stopped, self.gst.stopped), (1, 1))


class Monitoring(PageCase):
    """The "Hear processed voice in headphones" switch: off by default, saved, and handed to monitor.sync off the main
    loop; never voice.apply, never the Scarlett."""

    def test_it_starts_off_and_says_the_delay_where_it_plays_and_what_it_keeps_open(self):
        self.assertFalse(self.page.monitoring.active)
        self.assertEqual(self.page.monitoring.subtitle, monitor.LABEL)
        self.assertEqual(self.monitored, [])  # showing the saved state starts nothing

    def test_the_saved_switch_is_shown_at_start(self):
        page = Page()
        page.root_ = _Root()
        microphone.MicrophonePage.__init__(page, {"microphone": {}, "voice": {"monitor": True}})
        self.assertTrue(page.monitoring.active)
        self.assertEqual(self.monitored, [])

    def test_a_click_is_saved_and_synced_never_touching_booth_mic(self):
        self.click(self.page.monitoring, True)
        self.assertEqual((self.settings["voice"], self.saved), ({"monitor": True}, [self.settings]))
        self.assertEqual(self.monitored, [{"monitor": True}])
        self.click(self.page.monitoring, False)
        self.assertEqual(self.monitored[-1], {"monitor": False})
        self.assertEqual((self.applied, self.ran), ([], []))  # never Booth Mic's chain, never the Scarlett

    def test_a_click_with_the_pro_x_off_hands_that_to_sync_which_pauses_it(self):
        for on in (False, None, True):
            self.headset.on = on
            self.click(self.page.monitoring, not self.page.monitoring.active)
        self.assertEqual(self.monitor_on, [False, None, True])
        self.monitor_said = monitor.PAUSED
        self.click(self.page.monitoring, True)
        self.assertEqual(self.page.monitoring.subtitle, "Paused: the PRO X is off")

    def test_what_didnt_happen_is_said_on_the_row(self):
        self.monitor_said = "not started: see systemctl --user status booth-monitor.service"
        self.click(self.page.monitoring, True)
        self.assertEqual(self.page.monitoring.subtitle, f"Set, but {self.monitor_said}")
        self.monitor_said = "off"
        self.click(self.page.monitoring, False)
        self.assertEqual(self.page.monitoring.subtitle, monitor.LABEL)

    def test_a_failed_save_says_so_and_still_syncs(self):
        with mock.patch.object(microphone.store, "save", side_effect=OSError(28, "No space left on device")):
            self.click(self.page.monitoring, True)
        self.assertEqual(self.page.monitoring.subtitle, "Set, but not saved: No space left on device")
        self.assertEqual(self.monitored, [{"monitor": True}])
        self.show(state())  # a poll keeps the note
        self.assertEqual(self.page.monitoring.subtitle, "Set, but not saved: No space left on device")
        self.click(self.page.monitoring, False)  # saved this time
        self.assertEqual(self.page.monitoring.subtitle, monitor.LABEL)

    def test_waiting_for_the_pro_x_or_booth_mic_is_said_on_the_row(self):
        for said in (monitor.NO_PROX, monitor.NO_MIC):
            self.monitor_said = said
            self.click(self.page.monitoring, not self.page.monitoring.active)
            self.assertEqual(self.page.monitoring.subtitle, said)

    def test_each_poll_shows_monitorings_latest_state(self):
        self.monitor_said = "not started: see systemctl --user status booth-monitor.service"
        self.click(self.page.monitoring, True)
        for last, subtitle in ((monitor.NO_PROX, monitor.NO_PROX), (monitor.NOT_RUNNING, f"Set, but {monitor.NOT_RUNNING}"),
                               ("on", monitor.LABEL), (monitor.NO_MIC, monitor.NO_MIC), ("off", monitor.LABEL)):
            with mock.patch.object(monitor, "LAST", last):  # what a heal tick's sync found since
                self.show(state())
            self.assertEqual(self.page.monitoring.subtitle, subtitle)

    def test_sync_runs_on_a_worker_thread_and_the_row_changes_only_on_the_main_loop(self):
        threads, idle = [], []
        self.monitor_said = monitor.NO_PROX
        self.page.monitoring.subtitle = "Set, but not started: see systemctl --user status booth-monitor.service"
        with mock.patch.object(microphone.threading, "Thread",
                               lambda target, daemon: types.SimpleNamespace(start=lambda: threads.append(target))), \
                mock.patch.object(microphone.GLib, "idle_add", lambda f, *a: idle.append((f, a))):
            self.click(self.page.monitoring, True)
            self.assertEqual(self.page.monitoring.subtitle, monitor.LABEL)  # the click clears an old note at once
            self.assertEqual((len(threads), self.monitored), (1, []))  # handed to a thread, never run inline
            threads.pop()()
        self.assertEqual(self.monitored, [{"monitor": True}])
        self.assertEqual(self.page.monitoring.subtitle, monitor.LABEL)  # the worker never touched the row
        for f, a in idle:  # the main loop
            f(*a)
        self.assertEqual(self.page.monitoring.subtitle, monitor.NO_PROX)


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
        self.assertEqual(self.idle, [(page.show, (None, 4, True, "", None, True, None))])  # None: the stage rows stay as they are


if __name__ == "__main__":
    unittest.main()
