"""The main window: a navigation split view with Camera, Microphone and Headphones; one column when narrow."""
from gi.repository import Adw, Gio, GLib, Gtk

from booth import camera, mute, routing, voice
from booth.ui.camera import CameraPage
from booth.ui.headphones import HeadphonesPage
from booth.ui.microphone import MicrophonePage

MUTE_ACTION = "app.mute"  # booth.app's: Booth Mic's node mute (#21)
NOT_CONNECTED = "Not connected"
# Every keyboard shortcut, as the Keyboard Shortcuts window lists it: (group, the action booth.app binds it to, or None
# for GNOME's own, accelerator, title)
SHORTCUTS = [
    ("Pages", "win.page(0)", "<Control>1", "Camera"),
    ("Pages", "win.page(1)", "<Control>2", "Microphone"),
    ("Pages", "win.page(2)", "<Control>3", "Headphones"),
    ("Camera", "win.reset-zoom", "<Control>0", "Reset zoom"),
    ("Microphone", MUTE_ACTION, "<Control>m", "Mute Booth Mic"),
    ("Microphone", None, mute.SHORTCUT, "Mute the microphone (GNOME, anywhere)"),
    ("General", "win.show-help-overlay", "<Control>question", "Keyboard shortcuts"),
]


def main_menu():
    menu = Gio.Menu()
    menu.append("Run in Background", "app.run-in-background")  # a check item: booth.service enabled, this process held
    menu.append("Keyboard Shortcuts", "win.show-help-overlay")
    return menu


def shortcuts_window():
    section, groups = Gtk.ShortcutsSection(section_name="shortcuts"), {}
    for group, _action, accel, title in SHORTCUTS:
        if group not in groups:
            groups[group] = Gtk.ShortcutsGroup(title=group)
            section.add_group(groups[group])
        groups[group].add_shortcut(Gtk.ShortcutsShortcut(title=title, accelerator=accel))
    help_overlay = Gtk.ShortcutsWindow(modal=True)
    help_overlay.add_section(section)
    return help_overlay


def page(title, child, menu=None, banner=None):
    view, header = Adw.ToolbarView(content=child), Adw.HeaderBar()
    if menu:
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
        # #21: pressed while Booth Mic is muted, by Booth, GNOME (Ctrl+Alt+M) or another app
        header.pack_start(Gtk.ToggleButton(icon_name="microphone-sensitivity-muted-symbolic", action_name=MUTE_ACTION,
                                           tooltip_text="Mute Booth Mic (Ctrl+M) — Booth Stereo keeps recording"))
    view.add_top_bar(header)
    if banner:
        view.add_top_bar(banner)
    return Adw.NavigationPage(title=title, child=view)


def missing(icon, title, description):
    """A Stack holding a not-connected status page, named "missing"; the device's page goes beside it."""
    stack = Gtk.Stack()
    stack.add_named(Adw.StatusPage(icon_name=icon, title=title, description=description), "missing")
    return stack


class Window(Adw.ApplicationWindow):
    def __init__(self, app, settings, camera_node, camera_ctrls):
        super().__init__(application=app, title="Booth", default_width=860, default_height=560)
        self.set_size_request(360, 300)
        self.settings, self.plugged, self.dongle = settings, False, True  # the dongle: booth.app says, at once
        # #22: each device page sits in a stack beside its not-connected status page, switched live (booth.app)
        self.camera_status = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])  # e.g. "Ready · 2.0×"
        self.camera_stack = missing("camera-disabled-symbolic", "Camera Not Connected",
                                    "Plug in the BRIO: this page comes back by itself")
        self.camera = CameraPage(settings, camera_node, camera_ctrls, self.camera_said)
        self.camera_stack.add_named(self.camera, "camera")
        self.headphones_status = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])  # "~70 % · Volume 63 %"
        self.headphones = HeadphonesPage(settings, self.headphones_said, app.headset_monitor)
        self.headphones_stack = missing("audio-headphones-symbolic", "Headset Not Connected",
                                        "Plug in the PRO X Wireless dongle: this page comes back by itself")
        self.headphones_stack.add_named(self.headphones, "headset")
        self.connect("close-request", lambda _w: self.headphones.stop())  # stops its poll; None lets the close go on
        self.microphone = MicrophonePage(settings, app.headset_monitor)
        self.connect("close-request", lambda _w: self.microphone.stop())
        # Booth Mic's source (#20): "Booth Mic · from PRO X mic"; the banner says a change on the Microphone page
        self.mic_label = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])
        self.mic_banner = Adw.Banner(button_label="Dismiss")
        self.mic_banner.connect("button-clicked", lambda banner: banner.set_revealed(False))
        self.pages = [
            ("Camera", "camera-web-symbolic", page("Camera", self.camera_stack), self.camera_status),
            ("Microphone", "audio-input-microphone-symbolic",
             page("Microphone", self.microphone, banner=self.mic_banner), self.microphone.mini, self.mic_label),
            ("Headphones", "audio-headphones-symbolic", page("Headphones", self.headphones_stack),
             self.headphones_status),
        ]

        self.sidebar = sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for title, icon, _, *subtitles in self.pages:
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            row.append(Gtk.Image(icon_name=icon))
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            labels.append(Gtk.Label(label=title, xalign=0))
            for subtitle in subtitles:
                labels.append(subtitle)
            row.append(labels)
            if title == "Microphone":  # #21: the red dot, while Booth Mic is muted
                self.mute_dot = Gtk.Label(label="●", css_classes=["error"], visible=False, hexpand=True, xalign=1,
                                          tooltip_text="Booth Mic is muted")
                row.append(self.mute_dot)
            sidebar.append(row)

        self.split = Adw.NavigationSplitView(sidebar=page("Booth", sidebar, main_menu()), content=self.pages[0][2])
        sidebar.connect("row-selected", self.on_selected)
        sidebar.connect("row-activated", lambda *_: self.split.set_show_content(True))
        sidebar.select_row(sidebar.get_row_at_index(0))

        narrow = Adw.Breakpoint(condition=Adw.BreakpointCondition.parse("max-width: 500sp"))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)
        self.set_content(self.split)
        # #22: Ctrl+1/2/3 and Ctrl+0 (booth.app binds the keys), and the Keyboard Shortcuts window (win.show-help-overlay)
        select = Gio.SimpleAction.new("page", GLib.VariantType.new("i"))
        select.connect("activate", lambda _a, i: self.select_page(i.get_int32()))
        self.add_action(select)
        zoom = Gio.SimpleAction.new("reset-zoom", None)
        zoom.connect("activate", lambda *_: self.reset_zoom())
        self.add_action(zoom)
        self.set_help_overlay(shortcuts_window())
        self.show_camera(camera_node)

    def select_page(self, i):
        self.sidebar.select_row(self.sidebar.get_row_at_index(i))
        self.split.set_show_content(True)  # collapsed: the page, not the sidebar

    def reset_zoom(self):
        if self.plugged:  # never a write to a camera that is gone
            self.camera.reset_zoom()

    def show_camera(self, node, ctrls=None):
        """The BRIO found now (its by-id node, or None), after a by-id event: its page, or the not-connected one.
        `ctrls`: read for a new page, when the page has no camera (opened unplugged) or another one."""
        if ctrls is not None:
            self.camera_stack.remove(self.camera)
            self.camera = CameraPage(self.settings, node, ctrls, self.camera_said)
            self.camera_stack.add_named(self.camera, "camera")
        self.plugged = node is not None and node == self.camera.node and camera.ZOOM in self.camera.ctrls
        self.camera_stack.set_visible_child_name("camera" if self.plugged else "missing")  # the preview follows: map
        self.camera_said(camera.status(self.camera.ctrls, self.camera.values))

    def camera_said(self, text):
        """The Camera page's sidebar line, e.g. "Ready · 2.0×"; never while the camera is unplugged (a refused write
        refreshes the page)."""
        self.camera_status.set_label(text if self.plugged else NOT_CONNECTED)

    def show_headset(self, plugged):
        """The PRO X dongle there or not, after a by-id event or a HeadsetMonitor on/off: its page or the not-connected
        one. Back, its page's next poll says battery · volume."""
        self.dongle = plugged
        self.headphones_stack.set_visible_child_name("headset" if plugged else "missing")
        if not plugged:
            self.headphones_status.set_label(NOT_CONNECTED)

    def headphones_said(self, text):
        """The Headphones page's sidebar line, e.g. "~70 % · Volume 63 %"; never while the dongle is unplugged (a
        battery poll's "off" arrives after it)."""
        self.headphones_status.set_label(text if self.dongle else NOT_CONNECTED)

    def show_mic(self, source, changed=False):
        """Booth Mic's source in the sidebar; the banner on a change (the return to the Scarlett too) and while on
        anything but the Scarlett."""
        self.mic_label.set_label(routing.label(source))
        self.mic_banner.set_title(routing.MIC_SAID[source])
        self.mic_banner.set_revealed(changed or source != voice.SCARLETT)

    def show_mute(self, on):
        self.mute_dot.set_visible(on)

    def on_selected(self, _box, row):
        content = self.pages[row.get_index()][2] if row else None
        if content and content is not self.split.get_content():
            self.split.set_content(content)
