"""The Camera page: a Zoom slider whose range comes from the camera itself."""
import threading

from gi.repository import Adw, GLib, Gtk

from booth import camera, store

ZOOM = "zoom_absolute"


class CameraPage(Adw.PreferencesPage):
    def __init__(self, settings, node):
        super().__init__()
        self.settings, self.node = settings, node
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
        self.row = Adw.ActionRow(title="Zoom")
        self.row.add_suffix(scale)
        group.add(self.row)
        self.setter = camera.Setter(settings, node, ZOOM, self.spawn, store.save)

    def on_zoom(self, scale):
        self.setter.set(camera.clamp(self.zoom, scale.get_value()))

    def spawn(self, cmd, done):
        # v4l2-ctl can block for seconds on a stalled or unplugged camera: never on the main loop.
        threading.Thread(target=lambda: GLib.idle_add(self.finished, camera.run(cmd), done), daemon=True).start()

    def finished(self, ok, done):
        self.row.set_subtitle("" if ok else "The camera did not take this zoom")
        try:
            done(ok)
        except Exception as e:  # OSError (disk full, read-only) or anything else json.dump raises
            self.row.set_subtitle(f"Zoom set, but not saved: {getattr(e, 'strerror', None) or e}")
        return GLib.SOURCE_REMOVE
