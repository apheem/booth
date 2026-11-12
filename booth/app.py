"""The Booth application: a single-instance libadwaita app. Logic lives in the pure modules; this file only wires.

Every primary instance keeps the camera's saved settings applied: at launch, and whenever the BRIO is plugged back in.
`booth --background` (booth.service, at login) does it with no window. A plain launch while Booth runs shows the window
in that same process (GApplication is single-instance over D-Bus). With "Run in background" on, the process is held:
closing the window leaves it running."""
import sys
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from booth import camera, devices, headset, service, store  # noqa: E402
from booth.ui.window import Window  # noqa: E402

APP_ID = "io.github.apheem.Booth"
REPLUG_MS = 2000  # quiet time after the last by-id event before a restore: a flapping replug restores once
RESTORING = camera.WRITING  # the startup, replug and window-open restores and the page's writes never interleave


def camera_page(window):
    """The open window's Camera page when it found the BRIO (its Setter is then the one camera writer), else None."""
    page = window.camera if window is not None else None
    return page if page is not None and camera.ZOOM in page.ctrls else None


def busy(page):
    """A closed window's Camera page whose Setter still has writes to send or save, else None."""
    return page if page is not None and (page.setter.batch or page.setter.queue) else None


def restore_alone(app, closed):
    """A replug restore with no Camera page to do it, on a worker. `closed`: the closed page restore_saved saw idle."""
    with RESTORING:  # after any page write in flight, so the saved values are read as that write left them
        # the window opened meanwhile, or another closed with its writes maybe unsaved: its Setter restores, behind them
        if page := camera_page(app.window) or (app.closed_page if app.closed_page is not closed else None):
            GLib.idle_add(page.reapply)
            return
        restore_camera(app.settings)


def restore_camera(settings):
    """Re-apply the saved camera controls to the BRIO, if it is plugged in. Returns its node or None."""
    with RESTORING:
        dev = devices.discover()["camera"]
        for cmd in camera.restore(dev, dict(settings["camera"])):  # a copy: a page's save may change it meanwhile
            print(f"Booth: the camera refused {cmd[-1]}", file=sys.stderr)
    return dev


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window, self.starting, self.closed_page, self.settings = None, False, None, None
        self.background, self.held, self.monitor, self.replug = False, False, None, None
        self.snd_monitor, self.dongle = None, None
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Run with no window, re-applying the camera settings now and on replug", None)
        self.run_in_background = Gio.SimpleAction.new_stateful("run-in-background", None, GLib.Variant.new_boolean(False))
        self.run_in_background.connect("change-state", self.on_background)
        self.add_action(self.run_in_background)
        quit_ = Gio.SimpleAction.new("quit", None)  # install.sh's way to stop a running copy: quit() ignores a hold
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)

    def do_handle_local_options(self, options):
        if options.contains("background"):
            self.register(None)
            if self.get_is_remote():  # Booth already runs and does the work: a clean exit, no Restart= loop
                print("Booth: already running", file=sys.stderr)
                return 0
            self.background = True
        return -1  # register and activate: here, or in the running instance, which shows its window

    def do_activate(self):
        if self.monitor is None:  # the primary instance's first activate
            self.settings = store.load()  # once: every window, page, Setter and restore shares this dict, so a save
            self.watch()  # never drops another writer's value
            if self.background:
                return
        if self.window is not None:
            self.window.present()
        elif not self.starting:  # the camera can block for seconds per query: restore and read it off the main loop
            self.starting = True
            self.hold()  # no window yet: keeps the app alive until show_window()
            threading.Thread(target=lambda: self.start(self.settings), daemon=True).start()

    def watch(self):
        """Restore the camera on every replug. The background instance is held, so it lives with no window, and restores
        now; a window instance restores in start(), before it reads the camera for its page."""
        self.replug = service.Debounce(REPLUG_MS, self.restore_saved, GLib.timeout_add, GLib.source_remove)
        self.monitor = Gio.File.new_for_path(devices.BY_ID).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.monitor.connect("changed", self.on_by_id)
        # the PRO X dongle's hardware volume goes to full now and whenever it appears; the debounce also lets udev's
        # own mixer restore (alsactl, on card add) finish first
        self.dongle = service.Debounce(REPLUG_MS, self.fix_headset, GLib.timeout_add, GLib.source_remove)
        self.snd_monitor = Gio.File.new_for_path(devices.SND_BY_ID).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES,
                                                                                       None)
        self.snd_monitor.connect("changed", self.on_snd_by_id)
        self.fix_headset()
        if self.background:
            self.hold()
            self.held = True
            self.restore_saved()

    def on_by_id(self, _monitor, file, other, event):
        # the entry now in by-id: RENAMED's new name is `other`; MOVED_IN's `other` is its old path, outside the folder
        entry = other if event == Gio.FileMonitorEvent.RENAMED else file
        if devices.replugged(entry.get_basename(), event.value_nick):
            self.replug.poke()

    def on_snd_by_id(self, _monitor, file, other, event):
        entry = other if event == Gio.FileMonitorEvent.RENAMED else file  # as in on_by_id
        if devices.dongle_appeared(entry.get_basename(), event.value_nick):
            self.dongle.poke()

    def fix_headset(self):
        threading.Thread(target=headset.fix_hw_volume, daemon=True).start()  # amixer, off the main loop

    def restore_saved(self):
        # one writer: the page queues it behind its own writes, even a closed one's whose last write isn't saved yet
        if page := camera_page(self.window) or busy(self.closed_page):
            page.reapply()
            return
        closed = self.closed_page
        threading.Thread(target=lambda: restore_alone(self, closed), daemon=True).start()

    def start(self, settings):
        node, ctrls, enabled = None, {}, False
        try:
            with RESTORING:  # the page's first read comes after any restore already running (login's, a replug's)
                # a held instance keeps the camera restored itself; another restore here could race a replug's
                node = devices.discover()["camera"] if self.held else restore_camera(settings)
                ctrls = camera.read_ctrls(node) if node else {}  # the Camera page's model, read after the restore
            enabled = service.run(service.IS_ENABLED)
        finally:
            GLib.idle_add(self.show_window, settings, node, ctrls, enabled)

    def show_window(self, settings, node, ctrls, enabled):
        try:
            self.run_in_background.set_state(GLib.Variant.new_boolean(enabled))
            if enabled and not self.held:  # the switch shows ON: hold, as turning it on does (the unit isn't running)
                self.hold()
                self.held = True
            self.window = Window(self, settings, node, ctrls)
            self.window.connect("close-request", self.on_close)
            self.window.present()
        except Exception:  # no window: release lets Booth exit instead of living on with nothing to show
            traceback.print_exc()
        finally:
            self.starting = False
            self.release()
        return GLib.SOURCE_REMOVE

    def on_close(self, window):
        self.closed_page = camera_page(window) or self.closed_page  # set first: a worker reads window, then this
        self.window = None  # the closed window is gone: the next activate builds a new one
        return False

    def on_background(self, action, value):
        on = value.get_boolean()
        cmd = service.switch(on)
        threading.Thread(target=lambda: GLib.idle_add(self.switched, action, on, service.run(cmd)), daemon=True).start()

    def switched(self, action, on, ok):
        if not ok:
            print(f"Booth: couldn't {'enable' if on else 'disable'} {service.UNIT}", file=sys.stderr)
            return GLib.SOURCE_REMOVE
        action.set_state(GLib.Variant.new_boolean(on))
        if on != self.held:  # on: this process stays as the background instance; off: it exits when the window closes
            (self.hold if on else self.release)()
            self.held = on
        return GLib.SOURCE_REMOVE


def main(argv):
    return Application().run(argv)
