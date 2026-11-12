"""The main window: a navigation split view with Camera, Microphone and Headphones; one column when narrow."""
from gi.repository import Adw, Gtk

from booth.ui.camera import CameraPage


def placeholder(title, icon):
    return Adw.StatusPage(title=title, description="Coming soon", icon_name=icon)


def page(title, child):
    view = Adw.ToolbarView(content=child)
    view.add_top_bar(Adw.HeaderBar())
    return Adw.NavigationPage(title=title, child=view)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, settings, camera_node):
        super().__init__(application=app, title="Booth", default_width=860, default_height=560)
        self.set_size_request(360, 300)
        self.pages = [
            ("Camera", "camera-web-symbolic", page("Camera", CameraPage(settings, camera_node))),
            ("Microphone", "audio-input-microphone-symbolic",
             page("Microphone", placeholder("Microphone", "audio-input-microphone-symbolic"))),
            ("Headphones", "audio-headphones-symbolic",
             page("Headphones", placeholder("Headphones", "audio-headphones-symbolic"))),
        ]

        sidebar = Gtk.ListBox(css_classes=["navigation-sidebar"])
        for title, icon, _ in self.pages:
            row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            row.append(Gtk.Image(icon_name=icon))
            row.append(Gtk.Label(label=title, xalign=0))
            sidebar.append(row)

        self.split = Adw.NavigationSplitView(sidebar=page("Booth", sidebar), content=self.pages[0][2])
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
