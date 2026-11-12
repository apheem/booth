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


def restore_cmds(device, ctrls, saved):
    """Commands that re-apply saved {name: value} controls this camera has, each clamped to its live range."""
    return [set_cmd(device, name, clamp(ctrls[name], value)) for name, value in saved.items() if name in ctrls]


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
    """Sets one control off the main loop and saves it. At most one set is in flight; values asked for meanwhile
    collapse to the newest, so the last value asked for is the one applied. Only a value the camera took is saved.
    `spawn(cmd, done)` runs `cmd` elsewhere and calls `done(ok)` back on the main loop; `save(settings)` persists."""

    def __init__(self, settings, device, name, spawn, save):
        self.settings, self.device, self.name, self.spawn, self.save = settings, device, name, spawn, save
        self.pending, self.busy = None, False

    def set(self, value):
        self.pending = value
        if not self.busy:
            self._next()

    def _next(self):
        value, self.pending = self.pending, None
        self.busy = value is not None
        if self.busy:
            self.spawn(set_cmd(self.device, self.name, value), lambda ok: self._done(value, ok))

    def _done(self, value, ok):
        if ok:
            self.settings["camera"][self.name] = value
            self.save(self.settings)
        self._next()
