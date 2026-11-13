"""The Microphone page's Scarlett Solo group: 48V, Air, Inst/Line, Direct Monitor, firmware and sync, and which input is
the mic, plus the routing's saved setting (booth.routing) to turn Direct Monitor on when the PRO X turns off. The rules
live in booth.scarlett; this only wires. amixer and pw-dump run off the main loop.

Outside changes (the Scarlett's own buttons, alsamixer) and unplug/replug are followed by a poll every POLL_S seconds
while the window exists, as on the Headphones page: the card is found by name and its controls read by name each time.
A poll only ever shows what it read; writes come from the user's own clicks, and 48V on only after the dialog.

On top, the raw before-meter (booth.meter) on the mic channel, with the gain coaching; `mini` is the same meter for the
sidebar. Each poll says which channel and node to meter; it runs while either is on screen (see meter.wanted).

Below, Voice processing (booth.voice): the master switch, the rumble filter and the plugin stages, each a switch, one
knob and the raw controls the knob sets. A switch or a knob is saved and applied live to Booth Mic; each poll also brings
Booth Mic's config up to date with the mic it found (a no-op when nothing changed) and checks the plugins: a stage whose
plugin is missing or changed says so, greyed out, and is bypassed."""
import threading
import types

from gi.repository import Adw, GLib, Gtk

from booth import headset, meter, routing, scarlett, store, voice
from booth.ui.levelmeter import LevelMeter

POLL_S = 2
MASTER = "Off sends your mic to Booth Mic untouched"
RUMBLE = "Cuts low rumble and desk thumps below 80 Hz"
AUTO = "Booth turns it back off when the headset is on again"
MISSING = "Plugin missing or changed, so it is bypassed: run ./install.sh again"
# Each plugin stage's rows (voice.STAGES's keys): title, subtitle, the knob's title, range, step and digits. The knobs
# stay inside what the settings accept (booth.store).
STAGES = {"noise": ("Noise Suppression", "Removes background noise, and the room between words", "Strength", 0, 100, 5, 0),
          "leveling": ("Leveling", "Evens out loud and quiet parts", "Amount", 0, 100, 5, 0),
          "limiter": ("Limiter", "Keeps your voice from ever clipping", "Ceiling (dB)", -12, 0, 0.5, 1)}


class MicrophonePage(Adw.PreferencesPage):
    def __init__(self, settings):
        super().__init__()
        self.settings, self.quiet, self.polling, self.items, self.unsaved = settings, False, False, {}, ""
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
        self.bar, self.mini = LevelMeter(24), LevelMeter(6, margin_top=4)
        self.levels.add(self.bar)
        self.meter = meter.Meter(self.on_level, self.on_meter_failed)
        self.record, self.root = None, None
        for widget in (self, self.mini):
            widget.connect("map", self.on_map)
            widget.connect("unmap", self.meter_update)
        v = settings.setdefault("voice", {})
        self.voice = Adw.PreferencesGroup(title="Voice Processing", description=voice.WAITING)
        self.master = Adw.SwitchRow(title="Voice Processing", subtitle=MASTER)
        self.rumble = Adw.SwitchRow(title="Rumble Filter", subtitle=RUMBLE)
        self.master.connect("notify::active", self.on_voice, "enabled", MASTER)
        self.rumble.connect("notify::active", self.on_voice, "rumble", RUMBLE)
        self.stages, self.missing = {}, []  # missing: until the first poll checks the plugins
        for key, _file, label, _ins, _out, knob, default, mapping in voice.STAGES:
            title, subtitle, knob_title, lo, hi, step, digits = STAGES[key]
            value = v.get(knob, default)
            stage = types.SimpleNamespace(
                key=key, subtitle=subtitle, knob_key=knob, kind=type(default), mapping=mapping, raw={},
                switch=Adw.SwitchRow(title=title, subtitle=subtitle),
                # Adjustment.new: a value set before its range would be clamped to the default 0..0
                knob=Adw.SpinRow(title=knob_title, digits=digits,
                                 adjustment=Gtk.Adjustment.new(value, lo, hi, step, step * 4, 0)),
                details=Adw.ExpanderRow(title="Show details", subtitle=f"What {knob_title.split()[0]} sets on {label}"))
            stage.switch.connect("notify::active", self.on_voice, key, subtitle)
            stage.knob.connect("notify::value", self.on_knob, stage)
            for name in mapping(value):
                stage.raw[name] = Adw.ActionRow(title=name)
                stage.details.add_row(stage.raw[name])
            self.show_raw(stage, value)
            self.stages[key] = stage
        self.quiet = True  # showing the saved state is no change
        self.master.set_active(v.get("enabled", True))
        self.rumble.set_active(v.get("rumble", True))
        for stage in self.stages.values():
            stage.switch.set_active(v.get(stage.key, True))
        self.auto.set_active(settings.setdefault("routing", {}).get(routing.SETTING, True))
        self.quiet = False
        self.sensitize()
        self.voice.add(self.master)
        self.voice.add(self.rumble)
        for stage in self.stages.values():
            for row in (stage.switch, stage.knob, stage.details):
                self.voice.add(row)
        self.add(self.levels)
        self.add(self.group)
        self.add(self.voice)
        self.poll()
        self.timer = GLib.timeout_add_seconds(POLL_S, self.poll)

    def choice(self):
        return self.settings["microphone"].get("input", scarlett.DEFAULT_INPUT)

    def stop(self):
        """The window closed: no more polls, and the Scarlett is released."""
        GLib.source_remove(self.timer)
        self.meter.stop()

    def poll(self):
        if not self.polling:  # a slow amixer never piles up threads
            self.polling, since = True, self.writer.writes
            threading.Thread(target=lambda: self.read(since), daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def read(self, since):
        state, selecting, said, missing, running = None, True, "", None, True
        try:
            state = scarlett.read(self.choice())
            if state:  # unplugged: the running chain keeps its last config
                said = voice.apply(self.settings, state)  # not routed: the selector stops
                selecting = voice.selecting()  # down until heal() restarts it once the Scarlett is back
                running = selecting or voice.running()  # the selector runs only with Booth Mic (BindsTo)
            missing = voice.missing(voice.loaded())  # verified and running: one verified since waits for a restart
        finally:  # always: show() clears polling, else the page stops following outside changes
            GLib.idle_add(self.show, state, since, selecting, said, missing, running)

    def show(self, state, since, selecting=True, said="", missing=None, running=True):
        self.polling = False
        if missing is not None and missing != self.missing:  # None: not checked, the rows stay as they are
            self.missing, self.quiet = missing, True
            for stage in self.stages.values():  # a missing plugin shows off: it is bypassed, whatever was saved
                stage.switch.set_active(stage.key not in missing and self.settings["voice"].get(stage.key, True))
                stage.switch.set_subtitle(MISSING if stage.key in missing else stage.subtitle)
            self.quiet = False
            self.sensitize()
        self.record, problem = meter.source(state)
        self.levels.set_description(problem or meter.COACHING)
        if problem:
            self.on_meter_failed(problem)
        self.meter_update()
        self.voice.set_description(voice.describe(state, selecting, said, running))
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
        on = self.root is not None and meter.wanted(self.get_mapped(), self.mini.get_mapped(), self.root.is_suspended())
        self.meter.want(self.record, on)

    def on_level(self, peak, decay):
        for m in (self.bar, self.mini):
            m.set_level(peak, decay)

    def on_meter_failed(self, text):
        for m in (self.bar, self.mini):
            m.idle(text)

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
            for row in (stage.switch, stage.knob):
                row.set_sensitive(on and stage.key not in self.missing)

    def show_raw(self, stage, value):
        for name, raw in stage.mapping(value).items():
            stage.raw[name].set_subtitle(f"{raw:g}")

    def on_knob(self, row, _param, stage):
        if not self.quiet:
            value = stage.kind(round(row.get_value(), 1))  # a whole number for Strength and Amount, as the store wants
            self.show_raw(stage, value)
            self.change(row, stage.knob_key, value, "")

    def on_voice(self, row, _param, key, subtitle):
        if not self.quiet:
            self.change(row, key, row.get_active(), subtitle)

    def change(self, row, key, value, subtitle):
        """A switch or a knob: saved, then applied live to Booth Mic off the main loop."""
        self.settings["voice"][key] = value
        self.sensitize()
        try:
            store.save(self.settings)
            row.set_subtitle(subtitle)
        except OSError as e:  # applied anyway: the user must know it won't last
            row.set_subtitle(f"Set, but not saved: {e.strerror or e}")

        def apply():
            said = voice.apply(self.settings, None)  # a switch never moves the selector: the polls do
            if said.startswith("not "):  # not recorded: the next poll's apply tries again
                GLib.idle_add(row.set_subtitle, f"Set, but {said}")
        threading.Thread(target=apply, daemon=True).start()

    def spawn(self, make, done):
        threading.Thread(target=lambda: self.write(make, done), daemon=True).start()

    def write(self, make, done):
        try:
            scarlett.send(make)
        finally:  # always: done() clears Writer.busy, else no write is ever sent again
            GLib.idle_add(done)
