"""The Microphone page's Scarlett Solo group: 48V, Air, Inst/Line, Direct Monitor, firmware and sync, and which input is
the mic, plus the routing's saved setting (booth.routing) to turn Direct Monitor on when the PRO X turns off. The rules
live in booth.scarlett; this only wires. amixer and pw-dump run off the main loop.

Outside changes (the Scarlett's own buttons, alsamixer) and unplug/replug are followed by a poll every POLL_S seconds
while the window exists, as on the Headphones page: the card is found by name and its controls read by name each time.
A poll only ever shows what it read; writes come from the user's own clicks, and 48V on only after the dialog.

On top, the raw before-meter (booth.meter) on the mic channel, with the gain coaching; `mini` is the same meter for the
sidebar. Each poll says which channel and node to meter; it runs while either is on screen (see meter.wanted). Below it
the after-meter, the same Meter on Booth Mic (mono), only while the page itself is on screen: never for the sidebar, so
a glance at the sidebar never holds Booth Mic, and through it the Scarlett, open.

Below, Voice processing (booth.voice): the master switch, the preset picker, the rumble filter, the plugin stages and
Tone, each a switch, its knobs and the raw controls they set. A switch, a knob or a preset (every stage's switch and
knob at once; a knob moved after shows Custom) is saved and applied live to Booth Mic through one throttle, in a worker
(voice.Throttle: about 20 set-params a second, the last change always lands); each poll also brings Booth Mic's config
up to date with the mic it found (a no-op when nothing changed) and checks the plugins: a stage whose plugin is missing
or changed says so, greyed out, and is bypassed. Last, processed monitoring's switch (booth.monitor),
saved and handed to monitor.sync off the main loop.

Then Booth Stereo (booth.stereo, #19): the voice's side, the other side's source (the dropdown lists what the last
stereo.sync found, and a saved source that is away stays chosen) and the privacy note. A change is saved and handed to
stereo.sync off the main loop, which restarts only Booth Stereo; every voice change reaches it too (stereo.live: its own
chain's values, live; its shape is the heal tick's)."""
import functools
import threading
import types

from gi.repository import Adw, GLib, Gtk

from booth import headset, meter, monitor, routing, scarlett, stereo, store, take, voice
from booth.ui.levelmeter import LevelMeter

POLL_S = 2
SAVE_MS = 500  # a voice change's save (store.save fsyncs): at most about twice a second, never per knob notify
AFTER = {"node": voice.NODE, "channel": 1, "channels": 1}  # the after-meter's record: Booth Mic, mono, by name
MASTER = "Off sends your mic to Booth Mic untouched"
RUMBLE = "Cuts low rumble and desk thumps below 80 Hz"
AUTO = "Booth turns it back off when the headset is on again"
MISSING = "Plugin missing or changed, so it is bypassed: run ./install.sh again"
PENDING = "Plugin installed: it loads when Booth Mic restarts, bypassed until then"  # verified, not in the running chain
PRESET = "Sets every stage at once"
SIDES = ["Left", "Right"]  # Booth Stereo's voice side, in stereo.SIDES' order
SIDE = "Your processed voice; the other side carries the source below"
ZOOM_TIPS = ("In Zoom's audio settings:\n"  # #22: Booth Mic does the processing; the speaker follows Booth's routing
             "• Pick Booth Mic as the microphone\n"
             "• Set Suppress background noise to Low\n"
             "• Turn off Automatically adjust microphone volume\n"
             "• Set the speaker to Same as System so the auto-switch carries the call")
PRESETS = list(voice.PRESETS) + ["Custom"]  # Custom: what no preset matches, never applied
# Each stage's rows (voice.STAGES's keys and "tone"): title, subtitle.
STAGES = {"noise": ("Noise Suppression", "Removes background noise, and the room between words"),
          "tone": ("Tone", "Warmth, presence and air"),
          "leveling": ("Leveling", "Evens out loud and quiet parts"),
          "limiter": ("Limiter", "Keeps your voice from ever clipping")}
# Each knob (its settings key): title, range, step and digits, inside what the settings accept (booth.store).
KNOBS = {"noise_strength": ("Strength", 0, 100, 5, 0), "tone_warmth": ("Warmth (dB)", -12, 12, 0.5, 1),
         "tone_presence": ("Presence (dB)", -12, 12, 0.5, 1), "tone_air": ("Air (dB)", -12, 12, 0.5, 1),
         "leveling_amount": ("Amount", 0, 100, 5, 0), "limiter_ceiling": ("Ceiling (dB)", -12, 0, 0.5, 1)}


class MicrophonePage(Adw.PreferencesPage):
    def __init__(self, settings, headset_monitor=None):
        super().__init__()
        self.settings, self.quiet, self.polling, self.items, self.unsaved = settings, False, False, {}, ""
        self.headset_monitor = headset_monitor  # monitoring is paused while the PRO X is off
        self.writer = headset.Writer(self.spawn)
        self.group = Adw.PreferencesGroup(title="Scarlett Solo")
        self.input = Adw.ComboRow(title="Input", model=Gtk.StringList.new(list(scarlett.INPUTS)))
        self.input.set_selected(list(scarlett.INPUTS).index(self.choice()))
        self.input.connect("notify::selected", self.on_input)
        self.phantom = Adw.SwitchRow(title="48V", subtitle="Phantom power, for condenser mics only")
        self.phantom.connect("notify::active", self.on_phantom)
        self.air = Adw.ComboRow(title="Air", subtitle="Brightens the XLR input")
        self.air.connect("notify::selected", self.on_enum, scarlett.AIR)
        self.level = Adw.ComboRow(title="Inst/Line", subtitle="Input 1, the instrument jack")
        self.level.connect("notify::selected", self.on_enum, scarlett.LEVEL)
        self.direct = Adw.SwitchRow(title="Direct Monitor", subtitle="Hear yourself in the Scarlett's jack")
        self.direct.connect("notify::active", self.on_direct)
        # the routing policy's setting (booth.routing), saved; never a Scarlett write, so never greyed out
        self.auto = Adw.SwitchRow(title="Turn on Direct Monitor when the PRO X turns off", subtitle=AUTO)
        self.auto.connect("notify::active", self.on_auto)
        self.firmware = Adw.ActionRow(title="Firmware")
        self.sync = Adw.ActionRow(title="Sync")
        self.rows = {scarlett.PHANTOM: self.phantom, scarlett.AIR: self.air, scarlett.LEVEL: self.level,
                     scarlett.DIRECT: self.direct, scarlett.FIRMWARE: self.firmware, scarlett.SYNC: self.sync}
        for row in (self.input, self.phantom, self.air, self.level, self.direct, self.auto, self.firmware, self.sync):
            self.group.add(row)
        for row in self.rows.values():
            row.set_sensitive(False)  # until the first poll finds the Scarlett
        self.levels = Adw.PreferencesGroup(title="Input Level", description=meter.COACHING)
        self.bar, self.after = LevelMeter("Before processing", 24), LevelMeter("After processing (Booth Mic)", 24)
        self.mini = LevelMeter("Input level", 6, margin_top=4)
        for title, bar in (("Before", self.bar), ("After", self.after)):
            line = Gtk.Box(spacing=12, margin_top=6)
            # mnemonic_widget: GTK makes the label the bar's LABELLED_BY (PyGObject 3.48 can't pass update_relation a list)
            line.append(Gtk.Label(label=title, xalign=0, width_chars=6, mnemonic_widget=bar))
            line.append(bar)
            self.levels.add(line)
        self.meter = meter.Meter(self.on_level, self.on_meter_failed)
        self.after_meter = meter.Meter(self.after.set_level, self.after.idle)
        self.after_record = None  # until a poll finds Booth Mic running: mapping the page never opens it before
        self.record, self.root, self.monitor_unsaved = None, None, ""
        for widget in (self, self.mini):
            widget.connect("map", self.on_map)
            widget.connect("unmap", self.meter_update)
        v = settings.setdefault("voice", {})
        self.voice = Adw.PreferencesGroup(title="Voice Processing", description=voice.WAITING)
        self.master = Adw.SwitchRow(title="Voice Processing", subtitle=MASTER)
        self.rumble = Adw.SwitchRow(title="Rumble Filter", subtitle=RUMBLE)
        self.master.connect("notify::active", self.on_voice, "enabled", MASTER)
        self.rumble.connect("notify::active", self.on_voice, "rumble", RUMBLE)
        self.monitoring = Adw.SwitchRow(title="Hear Processed Voice in Headphones", subtitle=monitor.LABEL)
        self.monitoring.connect("notify::active", self.on_monitoring)
        self.preset = Adw.ComboRow(title="Preset", subtitle=PRESET, model=Gtk.StringList.new(PRESETS))
        self.preset.connect("notify::selected", self.on_preset)
        # every voice change: one apply in a worker, coalesced; voice_row: the row that changed last, told if it failed
        self.pending, self.save_timer = {}, 0  # rows whose change isn't saved yet → their subtitle; the save's timer
        self.throttle = voice.Throttle(self.apply_voice, lambda work: threading.Thread(target=work, daemon=True).start())
        self.voice_row = None
        self.stages, self.knobs, self.missing = {}, {}, {}  # missing: {stage: MISSING or PENDING}, from a poll
        chain = [(key, label, [(knob, mapping)]) for key, _f, label, _i, _o, knob, _d, mapping in voice.STAGES]
        chain.insert(1, ("tone", "the builtin biquads", [(k, functools.partial(voice.band, k)) for k, *_ in voice.TONE]))
        for key, label, knobs in chain:  # in chain order: Tone after noise suppression, as voice.build has it
            title, subtitle = STAGES[key]
            stage = types.SimpleNamespace(key=key, subtitle=subtitle, switch=Adw.SwitchRow(title=title, subtitle=subtitle),
                                          details=Adw.ExpanderRow(title="Show details",
                                                                  subtitle=f"The raw controls on {label}"))
            stage.switch.connect("notify::active", self.on_voice, key, subtitle)
            for k, mapping in knobs:
                knob_title, lo, hi, step, digits = KNOBS[k]
                value = v.get(k, voice.ABSENT[k])
                knob = types.SimpleNamespace(
                    key=k, stage=key, kind=type(voice.ABSENT[k]), mapping=mapping, raw={},
                    # Adjustment.new: a value set before its range would be clamped to the default 0..0
                    row=Adw.SpinRow(title=knob_title, digits=digits,
                                    adjustment=Gtk.Adjustment.new(value, lo, hi, step, step * 4, 0)))
                knob.row.connect("notify::value", self.on_knob, knob)
                for name in mapping(value):  # by node:control where a stage has more than one node
                    knob.raw[name] = Adw.ActionRow(title=name if len(knobs) == 1 else f"{k}:{name}")
                    stage.details.add_row(knob.raw[name])
                self.knobs[k] = knob
            self.stages[key] = stage
        self.quiet = True  # showing the saved state is no change
        self.master.set_active(v.get("enabled", True))
        self.auto.set_active(settings.setdefault("routing", {}).get(routing.SETTING, True))
        self.monitoring.set_active(v.get("monitor", False))
        self.quiet = False
        self.show_voice()
        # a test recording (booth.take): hear what a call hears after a change, without a call
        self.take = take.Take(self.on_take)
        self.take_row = Adw.ActionRow(title="Test Recording", subtitle=take.HINT)
        self.take_button = Gtk.Button(label="Record", valign=Gtk.Align.CENTER)
        self.take_button.connect("clicked", lambda _b: self.take.toggle())
        self.take_row.add_suffix(self.take_button)
        self.voice.add(self.take_row)
        self.voice.add(self.master)
        self.voice.add(self.preset)
        self.voice.add(self.rumble)
        for stage in self.stages.values():
            self.voice.add(stage.switch)
            for knob in self.knobs.values():
                if knob.stage == stage.key:
                    self.voice.add(knob.row)
            self.voice.add(stage.details)
        self.voice.add(self.monitoring)
        self.stereo = Adw.PreferencesGroup(title="Booth Stereo")
        self.stereo.set_description(stereo.NOTE)  # privacy: the voice while Booth Mic is muted, the computer's sound
        self.side = Adw.ComboRow(title="Voice On", subtitle=SIDE, model=Gtk.StringList.new(SIDES))
        self.side.set_selected(list(stereo.SIDES).index(stereo.sides(settings)[0]))
        self.side.connect("notify::selected", self.on_side)
        self.other = Adw.ComboRow(title="Other Side")
        self.other.connect("notify::selected", self.on_other)
        self.other_items, self.stereo_unsaved = None, ""  # the dropdown's (value, label)s; a save that failed
        self.show_sources()
        self.stereo.add(self.side)
        self.stereo.add(self.other)
        self.add(self.levels)
        self.add(self.group)
        self.add(self.voice)
        self.add(self.stereo)
        self.tips = Adw.PreferencesGroup(title="Zoom Tips")
        self.tips.set_description(ZOOM_TIPS)
        self.add(self.tips)
        self.poll()
        self.timer = GLib.timeout_add_seconds(POLL_S, self.poll)

    def choice(self):
        return self.settings["microphone"].get("input", scarlett.DEFAULT_INPUT)

    def stop(self):
        """The window closed: no more polls, and the Scarlett is released; a pending voice change saved and a test
        recording ended."""
        self.save_voice()
        self.take.stop()
        GLib.source_remove(self.timer)
        self.meter.stop()
        self.after_meter.stop()

    def poll(self):
        if not self.polling:  # a slow amixer never piles up threads
            self.polling, since = True, self.writer.writes
            threading.Thread(target=lambda: self.read(since), daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def read(self, since):
        state, selecting, said, missing, running, source = None, True, "", None, True, None
        try:
            with voice._LOCK:  # read and applied as one: never a stale Scarlett written over a backup heal moved to
                state = scarlett.read(self.choice())
                if state:  # unplugged: the running chain keeps its last config
                    said = voice.apply(self.settings, state)  # not routed: the selector stops
                    selecting = voice.selecting()  # down until heal() restarts it once the Scarlett is back
                    running = selecting or voice.running()  # the selector runs only with Booth Mic (BindsTo)
                else:  # on a backup, or silent: said so, and a stopped Booth Mic is never metered
                    running, source, selecting = voice.running(), voice.current(), voice.selecting()
            found = voice.verified()  # verified and running: one verified since waits for a restart
            missing = dict.fromkeys(voice.missing(voice.loaded(found=found)), PENDING)
            missing.update(dict.fromkeys(voice.missing(found), MISSING))
        finally:  # always: show() clears polling, else the page stops following outside changes
            GLib.idle_add(self.show, state, since, selecting, said, missing, running, source)

    def show(self, state, since, selecting=True, said="", missing=None, running=True, source=None):
        self.polling = False
        if missing is not None and missing != self.missing:  # None: not checked, the rows stay as they are
            self.missing = missing
            for stage in self.stages.values():
                stage.switch.set_subtitle(missing.get(stage.key, stage.subtitle))
            self.show_voice()
        self.record, problem = meter.source(state)
        self.levels.set_description(problem or meter.COACHING)
        if problem:
            self.on_meter_failed(problem)
        self.after_record = AFTER if running else None
        self.take_button.set_sensitive(running or self.take.proc is not None)  # Stop stays; no take of a stopped mic
        if not running:
            self.after.idle(voice.STOPPED)
        self.meter_update()
        self.voice.set_description(voice.describe(state, selecting, said, running, source))
        self.show_monitoring(monitor.LAST)  # the latest sync, the switch's or a heal tick's
        self.show_sources()
        self.show_stereo(stereo.LAST)
        if self.writer.fresh(since):  # else a write may have changed it since: the next poll shows it
            self.quiet = True  # what a poll shows is never written back
            ctrls = state["ctrls"] if state else {}
            self.group.set_description(None if state else scarlett.UNPLUGGED)
            for name, row in self.rows.items():
                row.set_sensitive(name in ctrls)
            self.phantom.set_active(scarlett.value(ctrls, scarlett.PHANTOM) is True)
            self.direct.set_active(scarlett.value(ctrls, scarlett.DIRECT) is True)
            for name, row in ((scarlett.AIR, self.air), (scarlett.LEVEL, self.level)):
                items = (ctrls.get(name) or {}).get("items") or []
                if items != self.items.get(name):  # the device's own item names
                    self.items[name] = items
                    row.set_model(Gtk.StringList.new(items))
                if isinstance(v := scarlett.value(ctrls, name), int):
                    row.set_selected(v)
            self.firmware.set_subtitle(scarlett.text(ctrls, scarlett.FIRMWARE))
            self.sync.set_subtitle(scarlett.text(ctrls, scarlett.SYNC))
            self.input.set_subtitle(self.unsaved or (scarlett.describe(state["mic"]) if state else ""))
            self.quiet = False
        return GLib.SOURCE_REMOVE

    def on_map(self, widget):
        if self.root is None:  # the window: it outlives both meters
            self.root = widget.get_root()
            self.root.connect("notify::suspended", self.meter_update)
        self.meter_update()

    def meter_update(self, *_):
        suspended = self.root is None or self.root.is_suspended()
        self.meter.want(self.record, meter.wanted(self.get_mapped(), self.mini.get_mapped(), suspended))
        self.after_meter.want(self.after_record, meter.wanted(self.get_mapped(), False, suspended))

    def on_level(self, peak, decay):
        for m in (self.bar, self.mini):
            m.set_level(peak, decay)

    def on_meter_failed(self, text):
        for m in (self.bar, self.mini):
            m.idle(text)

    def on_take(self, text, busy):
        """The test recording said something: the countdown, playing back, why it failed, or None when it is over."""
        self.take_row.set_subtitle(text or take.HINT)
        self.take_button.set_label("Stop" if busy else "Record")

    def on_phantom(self, row, _param):
        if self.quiet:
            return
        if row.get_active():  # the switch stays off until the user confirms
            self.quiet = True
            row.set_active(False)
            self.quiet = False
            self.confirm()
        else:
            self.writer.want(scarlett.PHANTOM, lambda card: scarlett.cset_cmd(card, scarlett.PHANTOM, False))

    def confirm(self):
        dialog = Adw.AlertDialog(heading="Turn on 48V phantom power?",
                                 body="Only condenser mics need it. Check that yours is one: a dynamic mic doesn't "
                                      "need 48V, and a ribbon mic can be damaged by it.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("on", "Turn On 48V")
        dialog.set_response_appearance("on", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self.on_confirm)
        dialog.present(self)

    def on_confirm(self, _dialog, response):
        if response == "on":  # the only place 48V is turned on: the user's own confirmation
            self.quiet = True
            self.phantom.set_active(True)
            self.quiet = False
            self.writer.want(scarlett.PHANTOM, lambda card: scarlett.cset_cmd(card, scarlett.PHANTOM, True, confirmed=True))

    def on_enum(self, row, _param, name):
        i = row.get_selected()
        if not self.quiet and i < len(self.items.get(name, [])):  # not INVALID_LIST_POSITION
            self.writer.want(name, lambda card: scarlett.cset_cmd(card, name, i))

    def on_direct(self, row, _param):
        if not self.quiet:
            on = row.get_active()
            self.writer.want(scarlett.DIRECT, lambda card: scarlett.cset_cmd(card, scarlett.DIRECT, on))

    def on_auto(self, row, _param):
        if self.quiet:
            return
        # by key each time: the routing replaces settings["routing"] when it saves its record (also on the main loop)
        self.settings["routing"][routing.SETTING] = row.get_active()
        try:
            store.save(self.settings)
            row.set_subtitle(AUTO)
        except OSError as e:  # set for this session: the user must know it won't last
            row.set_subtitle(f"Set, but not saved: {e.strerror or e}")

    def on_monitoring(self, row, _param):
        """Saved, then monitor.sync starts or stops the loopback off the main loop; what didn't happen is said."""
        if self.quiet:
            return
        self.settings["voice"]["monitor"] = row.get_active()
        try:
            store.save(self.settings)
            self.monitor_unsaved = ""
        except OSError as e:  # applied anyway: the user must know it won't last
            self.monitor_unsaved = f"Set, but not saved: {e.strerror or e}"
        self.show_monitoring(None)

        def sync():
            said = monitor.sync(self.settings, on=self.headset_monitor.on if self.headset_monitor else None)
            GLib.idle_add(self.show_monitoring, said)
        threading.Thread(target=sync, daemon=True).start()

    def show_monitoring(self, said):
        """What isn't as switched (waiting, not started…) first, then a save that failed, else the label."""
        self.monitoring.set_subtitle(monitor.problem(said) or self.monitor_unsaved or monitor.LABEL)

    def show_sources(self):
        """The dropdown from the sources the last stereo.sync found, the saved one selected; shown, never written."""
        items = stereo.choices(self.settings, stereo.SOURCES)
        quiet, self.quiet = self.quiet, True
        if items != self.other_items:
            self.other_items = items
            self.other.set_model(Gtk.StringList.new([label for _value, label in items]))
        self.other.set_selected([v for v, _label in items].index(self.settings["voice"].get("stereo_source",
                                                                                        stereo.SCARLETT)))
        self.quiet = quiet

    def on_side(self, row, _param):
        if not self.quiet and row.get_selected() < len(SIDES):
            self.stereo_change("stereo_side", list(stereo.SIDES)[row.get_selected()])

    def on_other(self, row, _param):
        if not self.quiet and row.get_selected() < len(self.other_items):
            self.stereo_change("stereo_source", self.other_items[row.get_selected()][0])

    def stereo_change(self, key, value):
        """Saved, then stereo.sync restarts Booth Stereo (only it) off the main loop; what it did is said."""
        self.settings["voice"][key] = value
        try:
            store.save(self.settings)
            self.stereo_unsaved = ""
        except OSError as e:  # applied anyway: the user must know it won't last
            self.stereo_unsaved = f"Set, but not saved: {e.strerror or e}"
        self.show_stereo(None)
        threading.Thread(target=lambda: GLib.idle_add(self.show_stereo, stereo.sync(self.settings)),
                         daemon=True).start()

    def show_stereo(self, said):
        """A save that failed first, else what the other side carries or why it is silent (stereo.sync's)."""
        self.other.set_subtitle(self.stereo_unsaved or said)

    def on_input(self, row, _param):
        self.settings["microphone"]["input"] = list(scarlett.INPUTS)[row.get_selected()]
        try:  # on the main loop, like the other pages' saves of the same dict
            store.save(self.settings)
            self.unsaved = ""
        except OSError as e:  # disk full, read-only: set, but the user must know it won't last
            self.unsaved = f"Set, but not saved: {e.strerror or e}"
            self.input.set_subtitle(self.unsaved)
        self.poll()  # the subtitle shows the new input's channel

    def sensitize(self):
        """The rumble filter and the stages wait on the master switch; a stage also on its plugin."""
        on = self.master.get_active()
        self.rumble.set_sensitive(on)
        for stage in self.stages.values():
            stage.switch.set_sensitive(on and stage.key not in self.missing)
        for knob in self.knobs.values():
            knob.row.set_sensitive(on and knob.stage not in self.missing)

    def show_voice(self):
        """The saved stage switches, knobs and their raw controls, and the preset they match, shown, never written. A
        missing plugin's stage shows off: it is bypassed, whatever was saved."""
        v, self.quiet = self.settings["voice"], True
        self.rumble.set_active(v.get("rumble", True))
        for stage in self.stages.values():
            stage.switch.set_active(stage.key not in self.missing and v.get(stage.key, voice.ABSENT[stage.key]))
        for knob in self.knobs.values():
            knob.row.set_value(v.get(knob.key, voice.ABSENT[knob.key]))
            self.show_raw(knob, v.get(knob.key, voice.ABSENT[knob.key]))
        self.show_preset()
        self.quiet = False
        self.sensitize()

    def show_preset(self):
        quiet, self.quiet = self.quiet, True
        self.preset.set_selected(PRESETS.index(voice.preset_of(self.settings["voice"]) or "Custom"))
        self.quiet = quiet

    def show_raw(self, knob, value):
        for name, raw in knob.mapping(value).items():
            knob.raw[name].set_subtitle(f"{raw:g}")

    def on_knob(self, row, _param, knob):
        if not self.quiet:
            value = knob.kind(round(row.get_value(), 1))  # a whole number for Strength and Amount, as the store wants
            self.show_raw(knob, value)
            self.change(row, {knob.key: value}, "")

    def on_voice(self, row, _param, key, subtitle):
        if not self.quiet:
            self.change(row, {key: row.get_active()}, subtitle)

    def on_preset(self, row, _param):
        i = row.get_selected()
        if not self.quiet and i < len(voice.PRESETS):  # Custom (or none) sets nothing
            self.settings["voice"].update(voice.PRESETS[PRESETS[i]])
            self.show_voice()
            self.change(row, voice.PRESETS[PRESETS[i]], PRESET)

    def change(self, row, values, subtitle):
        """A switch, a knob or a preset: saved, then applied live to Booth Mic through the throttle."""
        self.settings["voice"].update(values)
        self.sensitize()
        self.show_preset()
        self.pending[row] = subtitle
        self.save_timer = self.save_timer or GLib.timeout_add(SAVE_MS, self.save_voice)
        self.voice_row = row
        self.throttle.kick()

    def save_voice(self):
        """The voice changes since the last save, saved: SAVE_MS after the first, so the latest always lands; also on
        the window's close and Booth's quit (booth.app)."""
        self.save_timer, rows, self.pending = 0, self.pending, {}
        if rows:
            try:
                store.save(self.settings)
            except OSError as e:  # applied anyway: the user must know it won't last
                rows = dict.fromkeys(rows, f"Set, but not saved: {e.strerror or e}")
            for row, subtitle in rows.items():
                row.set_subtitle(subtitle)
        return GLib.SOURCE_REMOVE

    def apply_voice(self):
        """The throttle's worker: the settings as they are now, live; a switch never moves the selector: the polls do."""
        row, said = self.voice_row, voice.apply(self.settings, None)
        if said.startswith("not "):  # not recorded: the next poll's apply tries again
            GLib.idle_add(row.set_subtitle, f"Set, but {said}")
        GLib.idle_add(self.show_stereo, stereo.live(self.settings))  # Booth Stereo's own chain, its values only

    def spawn(self, make, done):
        threading.Thread(target=lambda: self.write(make, done), daemon=True).start()

    def write(self, make, done):
        try:
            scarlett.send(make)
        finally:  # always: done() clears Writer.busy, else no write is ever sent again
            GLib.idle_add(done)
