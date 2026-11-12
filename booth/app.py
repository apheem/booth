"""The Booth application: a single-instance libadwaita app. Logic lives in the pure modules; this file only wires."""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from booth import camera, devices, store  # noqa: E402
from booth.ui.window import Window  # noqa: E402

APP_ID = "io.github.apheem.Booth"


def restore_camera(settings):
    """Re-apply the saved camera controls to the BRIO, if it is plugged in. Returns its node or None."""
    dev = devices.discover()["camera"]
    if dev:
        for cmd in camera.restore_cmds(dev, camera.read_ctrls(dev), settings["camera"]):
            camera.run(cmd)
    return dev


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_activate(self):
        if self.window is None:
            settings = store.load()
            self.window = Window(self, settings, restore_camera(settings))
        self.window.present()


def main(argv):
    return Application().run(argv)
