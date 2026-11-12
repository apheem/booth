"""The Camera page: a Zoom slider whose range comes from the camera itself."""
from gi.repository import Adw, GLib, Gtk

from booth import camera, store

ZOOM = "zoom_absolute"


class CameraPage(Adw.PreferencesPage):
    def __init__(self, settings, node):
        super().__init__()
        self.settings, self.node, self.pending = settings, node, None
        group = Adw.PreferencesGroup(title="Camera")
        self.add(group)
        self.zoom = camera.read_ctrls(node).get(ZOOM) if node else None
        if self.zoom is None:
            group.add(Adw.ActionRow(title="BRIO not found", subtitle="Plug in the camera and reopen Booth"))
            return

        z = self.zoom
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, z["min"], z["max"], z["step"])
        scale.set_value(z["value"])
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        # BRIO zoom units: the minimum is 1× (100..500 → 1×..5×).
        scale.set_format_value_func(lambda _s, v: f"{v / (z['min'] or 100):.1f}×")
        scale.connect("value-changed", self.on_zoom)
        row = Adw.ActionRow(title="Zoom")
        row.add_suffix(scale)
        group.add(row)

    def on_zoom(self, scale):
        # Coalesce a drag into one v4l2-ctl call and one save every 50 ms.
        if self.pending is None:
            GLib.timeout_add(50, self.apply)
        self.pending = camera.clamp(self.zoom, scale.get_value())

    def apply(self):
        value, self.pending = self.pending, None
        if camera.run(camera.set_cmd(self.node, ZOOM, value)):
            self.settings["camera"][ZOOM] = value
            store.save(self.settings)
        return GLib.SOURCE_REMOVE
