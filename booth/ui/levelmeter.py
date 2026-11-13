"""LevelMeter, Booth's one custom-drawn widget: a horizontal bar in theme colours with the target zone shaded, the peak
as the bar and the decay as a tick, and an accessible label with the dB value. The scale lives in booth.meter. The
Microphone page's before-meter, its sidebar meter and Booth Mic's after-meter (#18) are all one of these."""
from gi.repository import Gtk

from booth import meter


class LevelMeter(Gtk.DrawingArea):
    def __init__(self, name, height=20, **kwargs):
        """name: what a screen reader calls this bar, e.g. "Before processing" (the page shows two at once)."""
        super().__init__(content_height=height, hexpand=True, accessible_role=Gtk.AccessibleRole.METER, **kwargs)
        self.name, self.peak, self.decay, self.said = name, None, None, None
        self.set_draw_func(self.draw)
        self.say(meter.label(name, None))

    def set_level(self, peak, decay):
        self.peak, self.decay = peak, decay
        self.say(meter.label(self.name, decay))
        self.set_tooltip_text(None)
        self.queue_draw()

    def idle(self, text):
        """Nothing to meter: an empty bar that says why."""
        self.peak = self.decay = None
        self.say(f"{self.name}: {text}")
        self.set_tooltip_text(text)
        self.queue_draw()

    def say(self, text):
        if text != self.said:  # 20 updates a second, but a screen reader hears only a new value
            self.said = text
            self.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def colour(self, name):
        found, rgba = self.get_style_context().lookup_color(name)  # libadwaita's named colours
        return rgba if found else self.get_color()

    def draw(self, _area, cr, width, height):
        def fill(rgba, alpha, x0, x1):
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha * alpha)
            cr.rectangle(x0 * width, 0, (x1 - x0) * width, height)
            cr.fill()

        fg = self.get_color()
        fill(fg, 0.1, 0, 1)  # the trough
        low, high = meter.zone()
        fill(self.colour("success_color"), 0.35, low, high)
        if self.peak is not None:
            name = "accent_bg_color" if self.peak <= meter.ZONE[1] else "warning_color" if self.peak < -1 else "error_color"
            fill(self.colour(name), 1, 0, meter.position(self.peak))
            x = meter.position(self.decay)
            fill(fg, 0.8, max(0, x - 2 / width), x)
