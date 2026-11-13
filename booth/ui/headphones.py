"""The Headphones page: the PRO X's volume (0–100 %, never more) and "Use for all sound". The rules live in
booth.headset; this only wires. wpctl and pw-dump run off the main loop.

On top, Sound Output: where sound plays now. While that isn't the PRO X (the backup Booth moved sound to when the
headset turned off, or the user's own pick), that output's volume and, on the Scarlett, its Direct Monitor (the jack's
sidetone; the same switch as the Microphone page's).

Outside changes (GNOME's volume keys, Settings) are followed by a poll every POLL_S seconds while the window exists:
two short commands (pw-dump, ~15 ms, and wpctl get-volume), no state kept between them, and the sink re-resolved on
every tick. A `pw-dump --monitor` stream would need a long-lived process and a merged JSON model of the whole graph to
learn the same two values."""
import threading

from gi.repository import Adw, Gdk, GLib, Gtk

from booth import devices, headset, revive, scarlett, store

POLL_S = 2
DIRECT = "Hear yourself in the Scarlett's jack"
RESTART = "Restarts the Scarlett's output without unplugging it; Booth also does it when it sees it stopped"
SWITCH_SUBTITLE = "Play all sound through the headset"
SUBTITLES = {"sidetone": "Hear your own voice in the headset", "auto_off": "Minutes idle before it turns off; 0 = never"}
NOT_SET = "Not set in Booth"  # the headset can't report its own value: the widget's position means nothing yet
NAMES = {"sidetone": "sidetone", "auto_off": "auto power-off"}


def pass_scroll(controller, _dx, dy):
    """A wheel or touchpad scroll over Sidetone or Auto power-off, caught before the widget (GTK4 would change its
    value, which sends and saves one the user didn't pick): the page scrolls instead, by the step its own
    GtkScrolledWindow uses. shortcut: no kinetic glide after a touchpad flick; add one if it feels abrupt."""
    window = controller.get_widget().get_ancestor(Gtk.ScrolledWindow)
    if window is not None:
        adj = window.get_vadjustment()
        step = adj.get_page_size() ** (2 / 3) if controller.get_unit() == Gdk.ScrollUnit.WHEEL else 1
        adj.set_value(adj.get_value() + dy * step)
    return True


class HeadphonesPage(Adw.PreferencesPage):
    def __init__(self, settings, status, monitor):
        """`monitor`: the Application's HeadsetMonitor; the page follows its battery polls while it is open."""
        super().__init__()
        self.settings, self.status, self.quiet, self.polling = settings, status, False, False
        self.monitor, self.state = monitor, None
        self.writer = headset.Writer(self.spawn)
        self.output = Adw.PreferencesGroup(title="Sound Output")
        self.playing = Adw.ActionRow(title="Playing Through")
        self.backup, self.backup_row = self.slider(self.on_backup)
        self.direct = Adw.SwitchRow(title="Direct Monitor", subtitle=DIRECT)
        self.direct.connect("notify::active", self.on_direct)
        self.restart = Adw.ActionRow(title="No Sound From the Scarlett?", subtitle=RESTART)  # booth.revive, by hand
        button = Gtk.Button(label="Restart Output", valign=Gtk.Align.CENTER)
        button.connect("clicked", self.on_restart)
        self.restart.add_suffix(button)
        for row in (self.playing, self.backup_row, self.direct, self.restart):
            self.output.add(row)
        for row in (self.backup_row, self.direct, self.restart):
            row.set_visible(False)  # until a poll finds sound on another output
        self.group = Adw.PreferencesGroup(title="PRO X Wireless")
        self.battery = Adw.ActionRow(title="Battery")
        self.group.add(self.battery)
        self.scale, volume = self.slider(self.on_volume)
        self.switch = Adw.SwitchRow(title="Use for all sound", subtitle=SWITCH_SUBTITLE)
        self.switch.connect("notify::active", self.on_default)
        self.rows = [volume, self.switch]
        for row in self.rows:
            self.group.add(row)
            row.set_sensitive(False)  # until the first poll finds the sink
        # HID++ settings: the user's saved values, set before connecting, so showing them sends nothing (an unsaved one
        # shows NOT_SET); visible per on_headset
        saved = settings["headphones"]
        self.sidetone = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 128, 1)
        self.sidetone.set_hexpand(True)
        if "sidetone" in saved:
            self.sidetone.set_value(saved["sidetone"])
        self.sidetone.connect("value-changed", lambda s: self.on_setting("sidetone", round(s.get_value())))
        sidetone = Adw.ActionRow(title="Sidetone")
        sidetone.add_suffix(self.sidetone)
        auto_off = Adw.SpinRow.new_with_range(0, 90, 5)
        auto_off.set_title("Auto power-off")
        if "auto_off" in saved:
            auto_off.set_value(saved["auto_off"])
        auto_off.connect("notify::value", lambda r, _p: self.on_setting("auto_off", round(r.get_value())))
        self.hidpp_rows = {"sidetone": sidetone, "auto_off": auto_off}
        for widget in (self.sidetone, auto_off):
            scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
            scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            scroll.connect("scroll", pass_scroll)
            widget.add_controller(scroll)
        for kind, row in self.hidpp_rows.items():
            row.set_subtitle(SUBTITLES[kind] if kind in saved else NOT_SET)
            self.group.add(row)
        self.note = Adw.ActionRow()  # "This headset doesn't accept …": a refused row hides rather than half-works
        self.group.add(self.note)
        monitor.listeners.append(self.on_headset)
        self.on_headset()
        self.add(self.output)
        self.add(self.group)
        self.poll()
        self.timer = GLib.timeout_add_seconds(POLL_S, self.poll)

    @staticmethod
    def slider(changed):
        """A 0–100 % volume slider in its row → (the scale, the row)."""
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_hexpand(True)
        scale.set_draw_value(True)
        scale.set_format_value_func(lambda _s, v: f"{v:.0f} %")
        scale.connect("value-changed", changed)
        row = Adw.ActionRow(title="Volume")
        row.add_suffix(scale)
        return scale, row

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
        on, refused = self.monitor.on is True, [k for k in self.hidpp_rows if k in self.monitor.refused]
        for kind, row in self.hidpp_rows.items():  # hidden until restart once it refused a value the user sent
            row.set_visible(on and kind not in refused)
        self.note.set_title("This headset doesn't accept " + " or ".join(NAMES[k] for k in refused))
        self.note.set_visible(on and bool(refused))
        self.say()

    def say(self):
        """Where sound plays, why the card's rows are away, and the sidebar: after a poll and a headset on/off."""
        self.playing.set_subtitle(headset.playing(self.state, self.monitor.on))
        self.group.set_description(headset.note(self.state, self.monitor.on))
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
            if state:
                self.scale.set_value(headset.percent(state["volume"]))
                self.switch.set_active(state["default"])
            output = state["output"] if state else None
            self.backup_row.set_visible(output is not None)
            for row in (self.direct, self.restart):  # the Scarlett's own rows
                row.set_visible(output is not None and output["direct"] is not None)
            if output:
                self.backup.set_value(headset.percent(output["volume"]))
                self.direct.set_active(output["direct"] is True)
            self.quiet = False
            self.state = state
            self.say()
        return GLib.SOURCE_REMOVE

    def on_backup(self, scale):
        output = self.state and self.state["output"]
        if not self.quiet and output:
            p, name = round(scale.get_value()), output["name"]  # the output shown: never another one's volume
            self.writer.want("backup", lambda dump: headset.volume_cmd(dump, p, name))

    def on_restart(self, _button):
        """The Scarlett's stuck output restarted by hand (booth.revive's pair of silent streams), off the main loop."""
        def work():
            found = revive.nodes(devices._run("pw-dump"))
            if found:
                revive.restart(found)
        threading.Thread(target=work, daemon=True).start()

    def on_direct(self, row, _param):
        if not self.quiet:
            on = row.get_active()
            # scarlett.send finds the card and writes; it returns None, so headset.send has no command left to run
            self.writer.want("direct", lambda _dump: scarlett.send(
                lambda card: scarlett.cset_cmd(card, scarlett.DIRECT, on)))

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
