"""The Microphone page's Scarlett Solo group: 48V, Air, Inst/Line, Direct Monitor, firmware and sync, and which input is
the mic. The rules live in booth.scarlett; this only wires. amixer and pw-dump run off the main loop.

Outside changes (the Scarlett's own buttons, alsamixer) and unplug/replug are followed by a poll every POLL_S seconds
while the window exists, as on the Headphones page: the card is found by name and its controls read by name each time.
A poll only ever shows what it read; writes come from the user's own clicks, and 48V on only after the dialog."""
import threading

from gi.repository import Adw, GLib, Gtk

from booth import headset, scarlett, store

POLL_S = 2


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
        self.firmware = Adw.ActionRow(title="Firmware")
        self.sync = Adw.ActionRow(title="Sync")
        self.rows = {scarlett.PHANTOM: self.phantom, scarlett.AIR: self.air, scarlett.LEVEL: self.level,
                     scarlett.DIRECT: self.direct, scarlett.FIRMWARE: self.firmware, scarlett.SYNC: self.sync}
        for row in (self.input, *self.rows.values()):
            self.group.add(row)
        for row in self.rows.values():
            row.set_sensitive(False)  # until the first poll finds the Scarlett
        self.add(self.group)
        self.poll()
        self.timer = GLib.timeout_add_seconds(POLL_S, self.poll)

    def choice(self):
        return self.settings["microphone"].get("input", scarlett.DEFAULT_INPUT)

    def stop(self):
        """The window closed: no more polls."""
        GLib.source_remove(self.timer)

    def poll(self):
        if not self.polling:  # a slow amixer never piles up threads
            self.polling, since = True, self.writer.writes
            threading.Thread(target=lambda: self.read(since), daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def read(self, since):
        state = None
        try:
            state = scarlett.read(self.choice())
        finally:  # always: show() clears polling, else the page stops following outside changes
            GLib.idle_add(self.show, state, since)

    def show(self, state, since):
        self.polling = False
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

    def on_input(self, row, _param):
        self.settings["microphone"]["input"] = list(scarlett.INPUTS)[row.get_selected()]
        try:  # on the main loop, like the other pages' saves of the same dict
            store.save(self.settings)
            self.unsaved = ""
        except OSError as e:  # disk full, read-only: set, but the user must know it won't last
            self.unsaved = f"Set, but not saved: {e.strerror or e}"
            self.input.set_subtitle(self.unsaved)
        self.poll()  # the subtitle shows the new input's channel

    def spawn(self, make, done):
        threading.Thread(target=lambda: self.write(make, done), daemon=True).start()

    def write(self, make, done):
        try:
            scarlett.send(make)
        finally:  # always: done() clears Writer.busy, else no write is ever sent again
            GLib.idle_add(done)
