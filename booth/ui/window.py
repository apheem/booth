"""The main window: a navigation split view with Camera, Microphone and Headphones; one column when narrow."""
from gi.repository import Adw, Gio, Gtk

from booth.ui.camera import CameraPage


def placeholder(title, icon):
    return Adw.StatusPage(title=title, description="Coming soon", icon_name=icon)


def page(title, child, menu=None):
    view, header = Adw.ToolbarView(content=child), Adw.HeaderBar()
    if menu:
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Main Menu"))
    view.add_top_bar(header)
    return Adw.NavigationPage(title=title, child=view)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, settings, camera_node, camera_ctrls):
        super().__init__(application=app, title="Booth", default_width=860, default_height=560)
        self.set_size_request(360, 300)
        camera_status = Gtk.Label(xalign=0, css_classes=["caption", "dim-label"])  # e.g. "Ready · 2.0×"
        self.pages = [
            ("Camera", "camera-web-symbolic", page("Camera", CameraPage(settings, camera_node, camera_ctrls, camera_status.set_label)),
             camera_status),
            ("Microphone", "audio-input-microphone-symbolic",
             page("Microphone", placeholder("Microphone", "audio-input-microphone-symbolic")), None),
            ("Headphones", "audio-headphones-symbolic",
             page("Headphones", placeholder("Headphones", "audio-headphones-symbolic")), None),
        ]

        sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for title, icon, _, subtitle in self.pages:
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            row.append(Gtk.Image(icon_name=icon))
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            labels.append(Gtk.Label(label=title, xalign=0))
            if subtitle:
                labels.append(subtitle)
            row.append(labels)
            sidebar.append(row)

        menu = Gio.Menu()
        menu.append("Run in Background", "app.run-in-background")  # a check item: booth.service enabled and running
        self.split = Adw.NavigationSplitView(sidebar=page("Booth", sidebar, menu), content=self.pages[0][2])
        sidebar.connect("row-selected", self.on_selected)
        sidebar.connect("row-activated", lambda *_: self.split.set_show_content(True))
        sidebar.select_row(sidebar.get_row_at_index(0))

        narrow = Adw.Breakpoint(condition=Adw.BreakpointCondition.parse("max-width: 500sp"))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)
        self.set_content(self.split)

    def on_selected(self, _box, row):
        content = self.pages[row.get_index()][2] if row else None
        if content and content is not self.split.get_content():
            self.split.set_content(content)
