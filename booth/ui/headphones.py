"""The Headphones page: the PRO X's volume (0–100 %, never more) and "Use for all sound". The rules live in
booth.headset; this only wires. wpctl and pw-dump run off the main loop.

Outside changes (GNOME's volume keys, Settings) are followed by a poll every POLL_S seconds while the window exists:
two short commands (pw-dump, ~15 ms, and wpctl get-volume), no state kept between them, and the sink re-resolved on
every tick. A `pw-dump --monitor` stream would need a long-lived process and a merged JSON model of the whole graph to
learn the same two values."""
import threading

from gi.repository import Adw, GLib, Gtk

from booth import headset, store

POLL_S = 2
SWITCH_SUBTITLE = "Play all sound through the headset"
SUBTITLES = {"sidetone": "Hear your own voice in the headset", "auto_off": "Minutes idle before it turns off; 0 = never"}


class HeadphonesPage(Adw.PreferencesPage):
    def __init__(self, settings, status, monitor):
        """`monitor`: the Application's HeadsetMonitor; the page follows its battery polls while it is open."""
        super().__init__()
        self.settings, self.status, self.quiet, self.polling = settings, status, False, False
        self.monitor, self.state = monitor, None
        self.writer = headset.Writer(self.spawn)
        self.group = Adw.PreferencesGroup(title="PRO X Wireless")
        self.battery = Adw.ActionRow(title="Battery")
        self.group.add(self.battery)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_hexpand(True)
        self.scale.set_draw_value(True)
        self.scale.set_format_value_func(lambda _s, v: f"{v:.0f} %")
        self.scale.connect("value-changed", self.on_volume)
        volume = Adw.ActionRow(title="Volume")
        volume.add_suffix(self.scale)
        self.switch = Adw.SwitchRow(title="Use for all sound", subtitle=SWITCH_SUBTITLE)
        self.switch.connect("notify::active", self.on_default)
        self.rows = [volume, self.switch]
        for row in self.rows:
            self.group.add(row)
            row.set_sensitive(False)  # until the first poll finds the sink
        # HID++ settings: the values set before connecting, so showing them sends nothing; visible per on_headset
        saved = settings["headphones"]
        self.sidetone = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 128, 1)
        self.sidetone.set_hexpand(True)
        self.sidetone.set_value(saved.get("sidetone", headset.DEFAULTS["sidetone"]))
        self.sidetone.connect("value-changed", lambda s: self.on_setting("sidetone", round(s.get_value())))
        sidetone = Adw.ActionRow(title="Sidetone", subtitle=SUBTITLES["sidetone"])
        sidetone.add_suffix(self.sidetone)
        auto_off = Adw.SpinRow.new_with_range(0, 90, 5)
        auto_off.set_title("Auto power-off")
        auto_off.set_subtitle(SUBTITLES["auto_off"])
        auto_off.set_value(saved.get("auto_off", headset.DEFAULTS["auto_off"]))
        auto_off.connect("notify::value", lambda r, _p: self.on_setting("auto_off", round(r.get_value())))
        self.hidpp_rows = {"sidetone": sidetone, "auto_off": auto_off}
        for row in self.hidpp_rows.values():
            self.group.add(row)
        monitor.listeners.append(self.on_headset)
        self.on_headset()
        self.add(self.group)
        self.poll()
        self.timer = GLib.timeout_add_seconds(POLL_S, self.poll)

    def stop(self):
        """The window closed: no more polls. Returns None, so close-request goes on."""
        GLib.source_remove(self.timer)
        self.monitor.listeners.remove(self.on_headset)

    def on_headset(self, _event=None):
        """The service's battery poll changed something (or the page opened): the card and the sidebar."""
        title, battery = headset.card(self.monitor)
        self.group.set_title(title)
        self.battery.set_subtitle(battery or "")
        self.battery.set_visible(battery is not None)
        for kind, row in self.hidpp_rows.items():  # only once the headset acknowledged it: never half-working
            row.set_visible(self.monitor.acked.get(kind, False))
        self.status(headset.status(self.state, self.monitor))

    def on_setting(self, kind, value):
        """Sidetone or Auto power-off changed: sent through the service's monitor (one HID++ exchange at a time with
        its battery poll) and saved, so it is re-sent whenever the headset turns on."""
        self.monitor.set(kind, value)
        self.settings["headphones"][kind] = value
        row = self.hidpp_rows[kind]
        try:  # on the main loop, like the page's other saves of the same dict
            store.save(self.settings)
            row.set_subtitle(SUBTITLES[kind])
        except OSError as e:  # disk full, read-only: set, but the user must know it won't last
            row.set_subtitle(f"Set, but not saved: {e.strerror or e}")

    def poll(self):
        if not self.polling:  # a slow pw-dump never piles up threads
            self.polling, since = True, self.writer.writes
            threading.Thread(target=lambda: self.read(since), daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def read(self, since):
        state = None
        try:
            state = headset.state()
        finally:  # always: show() clears polling, else the page stops following outside changes
            GLib.idle_add(self.show, state, since)

    def show(self, state, since):
        self.polling = False
        if self.writer.fresh(since):  # else a write may have changed it since: the next poll shows it
            self.quiet = True
            for row in self.rows:
                row.set_sensitive(state is not None)
            self.group.set_description(None if state else "Not connected")
            if state:
                self.scale.set_value(headset.percent(state["volume"]))
                self.switch.set_active(state["default"])
            self.quiet = False
            self.state = state
            self.status(headset.status(state, self.monitor))
        return GLib.SOURCE_REMOVE

    def on_volume(self, scale):
        if not self.quiet:
            p = round(scale.get_value())
            self.writer.want("volume", lambda dump: headset.volume_cmd(dump, p))

    def on_default(self, row, _param):
        if not self.quiet:
            on = row.get_active()
            self.writer.want("default", lambda dump: self.default(dump, on))

    def default(self, dump, on):
        # on the writer's thread, after any earlier switch write's remember() ran: read the remembered sink now
        cmd, replaced = headset.default_cmd(dump, on, self.settings["headphones"].get("previous_default"))
        if replaced or (cmd and not on):  # off put the default back: nothing is left to remember
            GLib.idle_add(self.remember, replaced)
        return cmd

    def remember(self, name):
        if name:
            self.settings["headphones"]["previous_default"] = name
        else:
            self.settings["headphones"].pop("previous_default", None)
        try:  # on the main loop, like the Camera page's saves of the same dict
            store.save(self.settings)
            self.switch.set_subtitle(SWITCH_SUBTITLE)
        except OSError as e:  # disk full, read-only: set, but the user must know it won't last
            self.switch.set_subtitle(f"Set, but not saved: {e.strerror or e}")

    def spawn(self, make, done):
        threading.Thread(target=lambda: self.write(make, done), daemon=True).start()

    def write(self, make, done):
        try:
            headset.send(make)
        finally:  # always: done() clears Writer.busy, else no write is ever sent again
            GLib.idle_add(done)
