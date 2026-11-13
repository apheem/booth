"""The Booth application: a single-instance libadwaita app. Logic lives in the pure modules; this file only wires.

Every primary instance keeps the camera's saved settings applied: at launch, and whenever the BRIO is plugged back in.
`booth --background` (booth.service, at login) does it with no window. A plain launch while Booth runs shows the window
in that same process (GApplication is single-instance over D-Bus). With "Run in background" on, the process is held:
closing the window leaves it running."""
import signal
import sys
import threading
import traceback

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from booth import camera, devices, headset, monitor, mute, revive, routing, service, stereo, store, voice  # noqa: E402
from booth.ui.window import SHORTCUTS, Window  # noqa: E402

APP_ID = service.APP_ID
REPLUG_MS = 2000  # quiet time after the last by-id event before a restore: a flapping replug restores once
RESTORING = camera.WRITING  # the startup, replug and window-open restores and the page's writes never interleave
BATTERY_S = 10  # the PRO X battery poll: also how soon "headset off" is noticed
ON_MAIN_S = 10  # how long a worker waits for the main loop before taking "no" (it is quitting)


def on_main(f, *args):
    """From a worker: f(*args) on the main loop, waited for → its result, or False when the main loop didn't run it."""
    done, out = threading.Event(), [False]

    def call():
        out[0] = f(*args)
        done.set()
    GLib.idle_add(call)
    done.wait(ON_MAIN_S)
    return out[0]


def off_main(work, done):
    """Run work() on a worker, then done(its result) on the main loop; done(None) when it raises."""
    def run():
        result = None
        try:
            result = work()
        finally:  # always: done() clears the caller's busy flag, else it never runs work again
            GLib.idle_add(done, result)
    threading.Thread(target=run, daemon=True).start()


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
        self.snd_monitor, self.dongle, self.healing = None, None, False
        self.output_watch, self.watching = revive.Watch(), False  # the Scarlett's stuck output, found and restarted
        self.mic, self.mic_source, self.mic_poke = {}, voice.SCARLETT, None  # #20: the mic rule's state, what it said
        self.headset_monitor = headset.HeadsetMonitor(self.low_battery, off_main)  # watch() subscribes the routing
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Run with no window, re-applying the camera settings now and on replug", None)
        self.run_in_background = Gio.SimpleAction.new_stateful("run-in-background", None, GLib.Variant.new_boolean(False))
        self.run_in_background.connect("change-state", self.on_background)
        self.add_action(self.run_in_background)
        quit_ = Gio.SimpleAction.new("quit", None)  # install.sh's way to stop a running copy: quit() ignores a hold
        quit_.connect("activate", lambda *_: self.quit())
        self.add_action(quit_)
        # #21: Booth Mic's mute, the header button's state; activating it (the button, Ctrl+M) toggles it: on_mute
        self.mute_known = None  # the booth_mic object.serial whose mute Booth follows (booth.mute.seen)
        self.mute_action = Gio.SimpleAction.new_stateful("mute", None, GLib.Variant.new_boolean(False))
        self.mute_action.connect("change-state", self.on_mute)
        self.add_action(self.mute_action)
        for _group, action, accel, _title in SHORTCUTS:  # Ctrl+M (#21), Ctrl+1/2/3 and Ctrl+0 (#22), Ctrl+?
            if action:  # None: GNOME's own Ctrl+Alt+M, listed only
                self.set_accels_for_action(action, [accel])
        Gtk.Window.set_default_icon_name(APP_ID)  # install.sh's icon, by the app's id (as the desktop entry's Icon=)

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
            self.headset_monitor.settings = self.settings  # never drops another writer's value
            self.watch()
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
        # #22: the open window's Camera page follows the BRIO coming and going, once a burst of by-id events settles
        self.camera_seen = service.Debounce(REPLUG_MS, self.check_camera, GLib.timeout_add, GLib.source_remove)
        # Booth Mic's mic fallback (#20) within seconds, not at the next tick: a heal once a sound card's (the
        # Scarlett's, the BRIO's, the dongle's) burst of links settles, or the PRO X turns on or off
        self.mic_poke = service.Debounce(REPLUG_MS, self.heal_voice, GLib.timeout_add, GLib.source_remove)
        # logout stops booth.service with SIGTERM: quit, so do_shutdown stops a backup selector no Booth would say
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)
        self.fix_headset()
        self.mic_at_start()  # before sync_voice, which may move the selector
        self.sync_voice()
        self.watch_mute()
        # the routing policy: output to the Scarlett when the PRO X turns off and back, window or not; subscribed
        # before the first poll, whose reading is the startup transition
        self.routing = routing.Executor(self.settings, lambda text: GLib.idle_add(self.routed, text),
                                        lambda state: on_main(self.save_routing, state))
        self.headset_monitor.listeners.append(self.routing.listener)
        self.headset_monitor.listeners.append(self.on_headset)
        self.headset_monitor.poll()  # the PRO X's battery and on/off, now and every BATTERY_S, with a window or not
        GLib.timeout_add_seconds(BATTERY_S, self.headset_monitor.poll)
        GLib.timeout_add_seconds(BATTERY_S, self.routing.retry)  # a rule not settled yet (the PRO X sink not back) again
        # Booth Mic's selector back within 10 s of its Scarlett: a replug, a login race or a profile switch (no by-id event)
        GLib.timeout_add_seconds(BATTERY_S, self.heal_voice)
        # the Scarlett playing into nothing (booth.revive): noticed within a tick and restarted, window or not
        GLib.timeout_add_seconds(BATTERY_S, self.watch_output)
        if self.background:
            self.hold()
            self.held = True
            self.restore_saved()

    def mic_at_start(self):
        """#20: the mic rule starts from the selector that runs: a backup left running (a Booth that crashed, or started
        after the selector) is said now and shown, and a later return to the Scarlett is said."""
        running = voice.current()
        if running in dict(voice.BACKUPS):
            self.mic, actions = routing.next(self.mic, {"kind": "mic", "source": running})
            self.mic_moved(actions)

    def do_shutdown(self):
        voice.release()  # every primary instance: no backup mic feeds Booth Mic with no Booth to say it
        mute.stop()  # #21: its pw-dump --monitor ends with Booth
        if self.window is not None:  # a quit closes no window by close-request: its pending voice change saved here
            self.window.microphone.save_voice()
            self.window.microphone.take.stop()  # and no test recording is left playing, or its file kept
        Adw.Application.do_shutdown(self)

    def on_by_id(self, _monitor, file, other, event):
        # the entry now in by-id: RENAMED's new name is `other`; MOVED_IN's `other` is its old path, outside the folder
        entry = other if event == Gio.FileMonitorEvent.RENAMED else file
        if devices.replugged(entry.get_basename(), event.value_nick):
            self.replug.poke()
        if devices.camera_moved(entry.get_basename(), event.value_nick):
            self.camera_seen.poke()

    def check_camera(self):
        """#22: the BRIO found now (on a worker: udevadm, v4l2-ctl) shown on the open window's Camera page. Its own
        camera back: restore_saved's reapply restores it. A camera the page lacks: restored and read for a new page."""
        window = self.window
        if window is None:  # a window opened later reads the camera in start()
            return
        page = window.camera

        def find():
            with RESTORING:  # after a restore or write in flight, so a new page reads what they left
                node = devices.discover()["camera"]
                if node is None or (node == page.node and camera.ZOOM in page.ctrls):
                    return node, None
                node = restore_camera(self.settings)
                return node, camera.read_ctrls(node) if node else None

        def show(found):
            if found is not None and self.window is window:  # not a window closed meanwhile
                window.show_camera(*found)
        off_main(find, show)

    def on_snd_by_id(self, _monitor, file, other, event):
        entry = other if event == Gio.FileMonitorEvent.RENAMED else file  # as in on_by_id
        if devices.dongle_appeared(entry.get_basename(), event.value_nick):
            self.dongle.poke()
        if event.value_nick in ("created", "deleted", "moved-in", "moved-out", "renamed"):  # a mic may be back or gone
            self.mic_poke.poke()
            self.show_dongle()

    def on_headset(self, event):
        if event in ("on", "off"):  # the PRO X mic is a backup only while the headset is on
            self.mic_poke.poke()
            self.show_dongle()

    def show_dongle(self):
        """#22: the open window's Headphones page: the PRO X dongle there or not, by its by-id link now."""
        if self.window is not None:
            self.window.show_headset(devices.dongle_present())

    def fix_headset(self):
        threading.Thread(target=headset.fix_hw_volume, daemon=True).start()  # amixer, off the main loop

    def sync_voice(self):
        """Booth Mic's config up to date with the Scarlett and its service started, then processed monitoring as its
        switch says, then Booth Stereo (amixer, pw-dump, systemctl: off the main loop)."""
        def sync():
            voice.sync(self.settings)
            monitor.sync(self.settings, on=self.headset_monitor.on)
            stereo.sync(self.settings, notify=self.stereo_switched)
        threading.Thread(target=sync, daemon=True).start()

    def watch_mute(self):
        """#21: pw-dump's monitor on a worker for good; each sighting of booth_mic goes to on_mute_seen. Events, not a
        poll: it prints only when the graph changes, and the record must follow GNOME's mute with the window closed too,
        so a booth-voice restart starts Booth Mic silent."""
        threading.Thread(target=lambda: mute.watch(lambda node: GLib.idle_add(self.on_mute_seen, node)),
                         daemon=True).start()

    def on_mute_seen(self, node):
        self.mute_known, record, write = mute.seen(node, self.mute_known, self.muted())
        self.record_mute(record)
        if write is not None:  # a recreated Booth Mic: its mute put back (by name, on a worker)
            threading.Thread(target=lambda: mute.write(write), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def on_mute(self, action, value):
        """The header button or Ctrl+M: Booth Mic's node mute set, and recorded at once."""
        on = value.get_boolean()
        self.record_mute(on)

        def write():  # refused: a mute's silence applied again here, so mute_failed says whether it landed
            if not mute.write(on):
                GLib.idle_add(self.mute_failed, on, voice.apply(self.settings, None) if on else None)
        threading.Thread(target=write, daemon=True).start()

    def mute_failed(self, on, said=None):
        """wpctl didn't take the button's `on`: the node keeps its mute, and the button shows it (pressing it again
        tries again). Fail closed: the chain stays (or goes back to) silent, and the node is no longer followed, so its
        next sighting puts the mute back instead of turning the record live. `said`: voice.apply's, for a mute."""
        self.mute_known = None
        self.record_mute(True)
        self.mute_action.set_state(GLib.Variant.new_boolean(not on))
        silenced = said in ("live", "unchanged", "restarted", voice.HELD)  # the muted chain set live, or started
        self.send_notification("mute-failed", Gio.Notification.new(
            "Couldn't unmute Booth Mic in GNOME — it stays muted; try again" if not on else
            "Couldn't mute Booth Mic in GNOME — the chain is silenced; try again" if silenced else
            f"Couldn't mute Booth Mic in GNOME, and its chain couldn't be silenced ({said}); try again"))
        return GLib.SOURCE_REMOVE

    def muted(self):
        return bool((self.settings.get("voice") or {}).get("muted"))

    def record_mute(self, on):
        """The mute shown (button, Ctrl+M, red dot) and, when it changed, recorded: saved for booth-voice's pre-start
        and Booth Mic's chain silenced or not, live (apply, on a worker)."""
        self.mute_action.set_state(GLib.Variant.new_boolean(on))
        if self.window is not None:
            self.window.show_mute(on)
        if self.muted() == on:
            return
        self.settings.setdefault("voice", {})["muted"] = on
        try:
            store.save(self.settings)
        except OSError as e:  # a booth-voice restart would start Booth Mic as last saved
            self.send_notification("mute-unsaved", Gio.Notification.new(f"Mute set, but not saved: {e.strerror or e}"))
        threading.Thread(target=lambda: voice.apply(self.settings, None), daemon=True).start()

    def heal_voice(self):
        if not self.healing:  # a slow heal (amixer, pw-dump, systemctl) never overlaps the next tick
            self.healing = True
            threading.Thread(target=self.heal_once, daemon=True).start()  # off the main loop
        return True

    def heal_once(self):
        try:
            voice.heal(self.settings, on=lambda: self.headset_monitor.on)
            source = voice.CHOSEN if voice.running(voice.SEEN) else routing.DOWN  # heal's pw-dump when it read one
            self.mic, actions = routing.next(self.mic, {"kind": "mic", "source": source})
            if "seen" in self.mic:  # a change seen once: looked at again once things settle, not at the next tick
                GLib.idle_add(self.mic_poke.poke)
            if actions:
                GLib.idle_add(self.mic_moved, actions)
        finally:
            try:  # monitoring back after Booth Mic or the PRO X dongle came back, window or not
                monitor.sync(self.settings, on=self.headset_monitor.on)  # paused while the PRO X is off
            finally:
                try:  # Booth Stereo (#19) back after Booth Mic or its source came back; it follows a new source
                    stereo.sync(self.settings, notify=self.stereo_switched)
                finally:
                    self.healing = False

    def watch_output(self):
        if not self.watching:  # a look takes a second or two (pw-dump, the sink's monitor, amixer): never two at once
            self.watching = True
            off_main(self.output_watch.check, self.output_watched)
        return True

    def output_watched(self, said):
        """The watch's verdict on the main loop: its notification when it restarted the output, or couldn't."""
        self.watching = False
        if said:
            self.send_notification("scarlett-output", Gio.Notification.new(said))

    def stereo_switched(self, text):
        """Booth Stereo's notice (computer sound moved to a new output), from sync's worker: on the main loop."""
        GLib.idle_add(lambda: self.send_notification("booth-stereo", Gio.Notification.new(text)))

    def low_battery(self, percent):
        note = Gio.Notification.new("Headset battery low")
        note.set_body(f"PRO X Wireless is at ~{percent} % (est.).")
        self.send_notification("headset-battery", note)

    def routed(self, text):
        self.send_notification("routing", Gio.Notification.new(text))  # one id: the newest replaces the last
        return GLib.SOURCE_REMOVE

    def mic_moved(self, actions):
        """The mic rule's actions, on the main loop: the notification (one id: the newest replaces the last), then the
        source for the sidebar and the Microphone page's banner, now and in a window opened later."""
        for kind, value in actions:
            if kind == "notify":
                self.send_notification("booth-mic", Gio.Notification.new(value))
            else:
                self.mic_source = value
                if self.window is not None:
                    self.window.show_mic(value, True)
        return GLib.SOURCE_REMOVE

    def save_routing(self, state):
        """The routing's record → True when it reached the disk. On the main loop, as every other save of the shared
        dict, so the user's switch next to it (the Microphone page's) is never lost."""
        self.settings["routing"] = state | {k: v for k, v in self.settings.get("routing", {}).items()
                                            if k == routing.SETTING}
        try:
            store.save(self.settings)
            return True
        except OSError as e:  # disk full, read-only: kept in memory, but a restart won't know Booth moved the sound
            self.send_notification("routing-unsaved",
                                   Gio.Notification.new(f"Sound routing set, but not saved: {e.strerror or e}"))
            return False

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
            self.window.show_mic(self.mic_source)  # opened on a backup: its sidebar line and banner say so
            self.window.show_mute(self.muted())
            self.window.show_headset(devices.dongle_present())
            self.window.connect("close-request", self.on_close)
            self.window.present()
            self.check_camera()  # a BRIO event while starting found no window: dropped, so looked at again now
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
