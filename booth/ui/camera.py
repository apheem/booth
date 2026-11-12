"""The Camera page: every standard BRIO control grouped Framing / Image / Focus, ranges from the camera itself.
The rules (which rows are insensitive, what to send, the defaults, the order) live in booth.camera; this only wires."""
import threading

from gi.repository import Adw, GLib, Gtk

from booth import camera, store

CENTER = Gtk.Align.CENTER
ARROWS = [("go-previous-symbolic", "Pan left", camera.PAN, -1), ("go-up-symbolic", "Tilt up", camera.TILT, 1),
          ("go-down-symbolic", "Tilt down", camera.TILT, -1), ("go-next-symbolic", "Pan right", camera.PAN, 1)]


class CameraPage(Adw.PreferencesPage):
    def __init__(self, settings, node, status=lambda text: None):
        super().__init__()
        self.settings, self.node, self.status = settings, node, status
        self.ctrls = camera.read_ctrls(node) if node else {}
        self.values = {n: c["value"] for n, c in self.ctrls.items() if "value" in c}
        self.rows, self.widgets, self.setters, self.errors = {}, {}, {}, {}
        if camera.ZOOM not in self.ctrls:
            group = Adw.PreferencesGroup(title="Camera")
            group.add(Adw.ActionRow(title="BRIO not found", subtitle="Plug in the camera and reopen Booth"))
            self.add(group)
            status(camera.status(self.ctrls, self.values))
            return

        for title, main, advanced in camera.GROUPS:
            group = Adw.PreferencesGroup(title=title)
            if title == "Framing":
                group.set_description(camera.RESOLUTION_HINT)
            reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=CENTER, css_classes=["flat"],
                               tooltip_text=f"Reset {title} to the camera's defaults")
            reset.connect("clicked", lambda _b, names=main + advanced: self.reset(names))
            group.set_header_suffix(reset)
            for name in main:
                self.add_row(group.add, name)
            if any(n in self.ctrls for n in advanced):
                expander = Adw.ExpanderRow(title="Advanced")
                group.add(expander)
                for name in advanced:
                    self.add_row(expander.add_row, name)
            self.add(group)
        self.refresh()

    def add_row(self, add, name):
        if name not in self.ctrls or name == camera.TILT:  # tilt shares the pan row
            return
        ctrl, title = self.ctrls[name], camera.LABELS[name]
        if name == camera.PAN:
            row = self.pan_tilt_row()
        elif camera.kind(name, ctrl) == "switch":
            on, off = camera.switch_values(name)
            row = Adw.SwitchRow(title=title, active=self.values[name] == on)
            row.connect("notify::active", lambda r, _p: self.changed(name, on if r.get_active() else off))
            self.widgets[name] = lambda v: row.set_active(v == on)
        elif camera.kind(name, ctrl) == "combo":
            keys, labels = zip(*camera.choices(ctrl))
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
            if self.values[name] in keys:
                row.set_selected(keys.index(self.values[name]))
            row.connect("notify::selected", lambda r, _p: self.changed(name, keys[r.get_selected()]))
            self.widgets[name] = lambda v: row.set_selected(keys.index(v))
        else:
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, ctrl["min"], ctrl["max"], ctrl["step"] or 1)
            scale.set_value(self.values[name])
            scale.set_draw_value(True)
            scale.set_hexpand(True)
            fmt = {camera.ZOOM: lambda v: camera.zoom_label(ctrl, v),
                   "white_balance_temperature": lambda v: f"{v:.0f} K"}.get(name, lambda v: f"{v:.0f}")
            scale.set_format_value_func(lambda _s, v: fmt(v))
            scale.connect("value-changed", lambda s: self.changed(name, camera.clamp(ctrl, s.get_value())))
            row = Adw.ActionRow(title=title)
            row.add_suffix(scale)
            self.widgets[name] = scale.set_value
        self.setters[name] = camera.Setter(self.settings, self.node, name, self.spawn(name), store.save)
        self.rows[name] = row
        add(row)

    def pan_tilt_row(self):
        row = Adw.ActionRow(title=camera.LABELS[camera.PAN])
        axes = [c for c in (camera.PAN, camera.TILT) if c in self.ctrls]
        for icon, tip, c, d in ARROWS:
            if c in axes:
                b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=CENTER, css_classes=["flat"])
                b.connect("clicked", lambda _b, c=c, d=d: self.changed(c, camera.nudge(self.ctrls[c], self.values[c], d)))
                row.add_suffix(b)
        center = Gtk.Button(label="Center", valign=CENTER)
        center.connect("clicked", lambda _b: [self.changed(c, camera.clamp(self.ctrls[c], 0)) for c in axes])
        row.add_suffix(center)
        for c in axes:
            self.widgets[c] = lambda v, c=c: self.changed(c, v)
            self.setters[c] = camera.Setter(self.settings, self.node, c, self.spawn(c), store.save)
            self.rows[c] = row
        return row

    def changed(self, name, value):
        self.values[name] = value
        for n, v in camera.changes(self.values, name):
            self.setters[n].set(v)
        self.refresh()

    def reset(self, names):
        for name, value in camera.defaults(self.ctrls, names):  # auto modes first
            if name in self.widgets:
                self.widgets[name](value)

    def refresh(self):
        off = camera.insensitive(self.ctrls, self.values)
        for name, row in self.rows.items():
            row.set_sensitive(name not in off)
            hint = camera.PAN_HINT if name in (camera.PAN, camera.TILT) and name in off else ""
            row.set_subtitle(self.errors.get(row) or hint)
        self.status(camera.status(self.ctrls, self.values))

    def spawn(self, name):
        def go(cmd, done):
            # v4l2-ctl can block for seconds on a stalled or unplugged camera: never on the main loop.
            threading.Thread(target=lambda: GLib.idle_add(self.finished, name, camera.run(cmd), done),
                             daemon=True).start()
        return go

    def finished(self, name, ok, done):
        row = self.rows[name]
        self.errors[row] = None if ok else "The camera did not take this value"
        try:
            done(ok)
        except Exception as e:  # OSError (disk full, read-only) or anything else json.dump raises
            self.errors[row] = f"Set, but not saved: {getattr(e, 'strerror', None) or e}"
        self.refresh()
        return GLib.SOURCE_REMOVE
