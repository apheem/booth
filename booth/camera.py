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
