"""The Booth application: a single-instance libadwaita app. Logic lives in the pure modules; this file only wires."""
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from booth import camera, devices, store  # noqa: E402
from booth.ui.window import Window  # noqa: E402

APP_ID = "io.github.apheem.Booth"


def restore_camera(settings):
    """Re-apply the saved camera controls to the BRIO, if it is plugged in. Returns its node or None."""
    dev = devices.discover()["camera"]
    for cmd in camera.restore(dev, settings["camera"]):
        print(f"Booth: the camera refused {cmd[-1]}", file=sys.stderr)
    return dev


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window, self.starting = None, False

    def do_activate(self):
        if self.window is not None:
            self.window.present()
        elif not self.starting:  # the camera can block for seconds per query: restore and read it off the main loop
            self.starting = True
            self.hold()  # no window yet: keeps the app alive until show_window()
            settings = store.load()
            threading.Thread(target=lambda: self.start(settings), daemon=True).start()

    def start(self, settings):
        node, ctrls = None, {}
        try:
            node = restore_camera(settings)
            ctrls = camera.read_ctrls(node) if node else {}  # the Camera page's model, read after the restore
        finally:
            GLib.idle_add(self.show_window, settings, node, ctrls)

    def show_window(self, settings, node, ctrls):
        self.window = Window(self, settings, node, ctrls)
        self.window.present()
        self.release()
        return GLib.SOURCE_REMOVE


def main(argv):
    return Application().run(argv)
