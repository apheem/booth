"""Camera controls: a control model parsed from `v4l2-ctl --list-ctrls-menus` (ranges come from the device), and the
commands that set them."""
import re
import subprocess

_CTRL = re.compile(r"^\s*(\w+) 0x[0-9a-f]+ \((\w+)\)\s*: (.*)$")
_MENU = re.compile(r"^\s+(\d+): (.*)$")
_NUM = re.compile(r"\b(min|max|step|default|value)=(-?\d+)")


def parse_ctrls(text):
    """→ {name: {"type", "inactive", "default", "value", and "min"/"max"/"step" or "menu" when present}}."""
    ctrls, last = {}, None
    for line in text.splitlines():
        if m := _CTRL.match(line):
            name, kind, rest = m.groups()
            if "disabled" in rest.partition("flags=")[2]:  # not usable on this camera: left out, menu lines too
                last = None
                continue
            last = ctrls[name] = {"type": kind, "inactive": "inactive" in rest}
            last.update((k, int(v)) for k, v in _NUM.findall(rest))
            if kind == "menu":
                last["menu"] = {}
        elif last is not None and "menu" in last and (m := _MENU.match(line)):
            last["menu"][int(m.group(1))] = m.group(2)
    return ctrls


def clamp(ctrl, value):
    """`value` snapped to the control's step and bounded by its range (bool and menu controls have none)."""
    if "min" not in ctrl:
        return int(value)
    lo, hi, step = ctrl["min"], ctrl["max"], ctrl.get("step") or 1
    return int(min(hi, max(lo, lo + round((value - lo) / step) * step)))


def set_cmd(device, name, value):
    return ["v4l2-ctl", "-d", device, f"--set-ctrl={name}={value}"]


ZOOM, PAN, TILT = "zoom_absolute", "pan_absolute", "tilt_absolute"

# The Camera page: (group, main rows, rows under Advanced), each row a control name.
GROUPS = [
    ("Framing", [ZOOM, PAN, TILT], []),
    ("Image", ["auto_exposure", "exposure_time_absolute", "exposure_dynamic_framerate", "backlight_compensation",
               "white_balance_automatic", "white_balance_temperature", "brightness"],
     ["contrast", "saturation", "sharpness", "gain", "power_line_frequency"]),
    ("Focus", ["focus_automatic_continuous", "focus_absolute"], []),
]
LABELS = {
    ZOOM: "Zoom", PAN: "Pan and tilt", "auto_exposure": "Auto exposure", "exposure_time_absolute": "Exposure time",
    "exposure_dynamic_framerate": "Keep 30 fps", "backlight_compensation": "HDR",
    "white_balance_automatic": "Auto white balance", "white_balance_temperature": "White balance",
    "brightness": "Brightness", "contrast": "Contrast", "saturation": "Saturation", "sharpness": "Sharpness",
    "gain": "Gain", "power_line_frequency": "Anti-flicker", "focus_automatic_continuous": "Autofocus",
    "focus_absolute": "Focus",
}
RESOLUTION_HINT = "The calling app (e.g. Zoom) picks the resolution, not Booth."
PAN_HINT = "Zoom in to move the frame"

# manual control → (its auto-mode control, the auto control's value that means manual)
AUTO = {
    "exposure_time_absolute": ("auto_exposure", 1),  # 1 = Manual Mode
    "white_balance_temperature": ("white_balance_automatic", 0),
    "focus_absolute": ("focus_automatic_continuous", 0),
}
AUTOS = {auto for auto, _ in AUTO.values()}
# switch rows whose control values aren't plain 1 = on / 0 = off: (value when on, value when off)
SWITCHES = {
    "auto_exposure": (3, 1),  # Aperture Priority (the BRIO's auto mode) / Manual Mode
    "exposure_dynamic_framerate": (0, 1),  # "Keep 30 fps" on = the camera may not lower the frame rate
}


def switch_values(name):
    return SWITCHES.get(name, (1, 0))


def kind(name, ctrl):
    """The row a control gets: "switch" (bools, 0..1 ints like the BRIO's HDR, auto exposure), "combo" or "slider"."""
    if name in SWITCHES or ctrl["type"] == "bool" or (ctrl.get("min"), ctrl.get("max")) == (0, 1):
        return "switch"
    return "combo" if ctrl["type"] == "menu" else "slider"


def choices(ctrl):
    """A menu's [(value, label)], "Disabled" shown as "Off"."""
    return [(v, "Off" if label == "Disabled" else label) for v, label in sorted(ctrl["menu"].items())]


def held(values):
    """Manual controls whose auto mode is on in `values`: the driver rejects a manual value then."""
    return {manual for manual, (auto, off) in AUTO.items() if values.get(auto, off) != off}


def insensitive(ctrls, values):
    """Rows the user can't change now: manual values held by their auto mode, and pan/tilt at 1× (they do nothing)."""
    at_1x = ZOOM in ctrls and values.get(ZOOM, ctrls[ZOOM]["min"]) <= ctrls[ZOOM]["min"]
    return held(values) | ({PAN, TILT} if at_1x else set())


def ordered(values):
    """{name: value} as [(name, value)], auto modes first, so a manual value is set once its auto mode is off."""
    return sorted(values.items(), key=lambda kv: kv[0] not in AUTOS)


def defaults(ctrls, names):
    """The camera's own defaults for the controls in `names` it has, auto modes first: what ↺ resets a group to."""
    return ordered({n: ctrls[n]["default"] for n in names if n in ctrls})


def reset_batch(ctrls, names):
    """What ↺ sends for `names`: their defaults, auto modes first, without a manual value whose auto mode the reset
    turns on (the driver would refuse it and stop the batch). Every manual control's auto mode is in its own group."""
    writes = defaults(ctrls, names)
    hold = held(dict(writes))
    return [(n, v) for n, v in writes if n not in hold]


def revert(values, taken, missed):
    """Put the page's `values` of the `missed` controls back to what the camera has (`taken`), so held() and
    insensitive() decide from the camera → the names whose widgets to move."""
    for n in missed:
        values[n] = taken[n]
    return missed


def changes(values, name):
    """What to set after `name` changed to values[name]: nothing for a manual value its auto mode holds; when an auto
    mode was just turned off, also the manual value shown, so the camera matches the page."""
    hold = held(values)
    sends = [] if name in hold else [(name, values[name])]
    return sends + [(m, values[m]) for m, (auto, _) in AUTO.items() if auto == name and m in values and m not in hold]


def nudge(ctrl, value, direction):
    """Pan or tilt one step (`direction` ±1), within range."""
    return clamp(ctrl, value + direction * (ctrl.get("step") or 1))


def zoom_label(ctrl, value):
    return f"{value / (ctrl['min'] or 100):.1f}×"  # BRIO zoom units: the minimum is 1× (100..500 → 1×..5×)


def status(ctrls, values):
    """The Camera sidebar subtitle, e.g. "Ready · 2.0×"."""
    if ZOOM not in ctrls:
        return "Not connected"
    return f"Ready · {zoom_label(ctrls[ZOOM], values.get(ZOOM, ctrls[ZOOM]['value']))}"


def restore_cmds(device, ctrls, saved):
    """Commands that re-apply saved {name: value} controls this camera has, each clamped to its live range: auto modes
    first, and no manual value whose auto mode stays on (the driver would refuse it)."""
    hold = held({n: c["value"] for n, c in ctrls.items() if "value" in c} | saved)
    return [set_cmd(device, name, clamp(ctrls[name], value)) for name, value in ordered(saved)
            if name in ctrls and name not in hold]


def read_ctrls(device):
    """The live control model of `device`, or {} when it can't be read (unplugged, busy)."""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", device, "--list-ctrls-menus"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ctrls(out)


def run(cmd):
    """Run one set command; False when it failed (the camera was unplugged or refused the value)."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def restore(device, saved):
    """Re-apply the saved {name: value} controls at launch (nothing when the camera is unplugged) → the refused commands."""
    if not device:
        return []
    return [cmd for cmd in restore_cmds(device, read_ctrls(device), saved) if not run(cmd)]


class Setter:
    """Sets camera controls off the main loop, one write at a time through one `spawn`, and saves each value the camera
    took (kept in `taken`). `set(writes)` queues an ordered batch [(name, value)]: a write starts only after the one
    before it finished ok, and at the first refusal the rest of its batch is not sent, so a manual value never races
    its auto-off. A queued batch for the same controls as the newest one collapses to the newest values (a drag sends
    the last value asked for). `spawn(name, cmd, done)` runs `cmd` elsewhere and calls `done(ok)` back on the main
    loop; `done` returns the names the camera did not take (refused or not sent, and not queued again), whose page
    value should go back to `taken`. `save(settings)` persists; its OSError is raised, after the next write started."""

    def __init__(self, settings, device, spawn, save, taken):
        self.settings, self.device, self.spawn, self.save = settings, device, spawn, save
        self.taken, self.batch, self.queue = dict(taken), [], []

    def set(self, writes):
        writes = list(writes)
        if not writes:
            return
        if self.queue and [n for n, _ in self.queue[-1]] == [n for n, _ in writes]:
            self.queue[-1] = writes
        else:
            self.queue.append(writes)
        if not self.batch:
            self._next()

    def _next(self):
        if not self.batch and self.queue:
            self.batch = self.queue.pop(0)
        if self.batch:
            name, value = self.batch[0]
            self.spawn(name, set_cmd(self.device, name, value), self._done)

    def _done(self, ok):
        (name, value), *rest = self.batch
        self.batch = rest if ok else []
        queued = {n for batch in self.queue for n, _ in batch}
        missed = [] if ok else [n for n, _ in [(name, value)] + rest if n not in queued]
        try:
            if ok:
                self.taken[name] = value
                self.settings["camera"][name] = value
                self.save(self.settings)
        finally:
            self._next()
        return missed
