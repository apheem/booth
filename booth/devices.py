"""Device discovery: finds devices by model on every call, never by a cached node number (those change on replug)."""
import os
import subprocess

BY_ID = "/dev/v4l/by-id"
BRIO = ("046d", "085e")  # USB vendor, product


def parse_udev(text):
    """`udevadm info --query=property` output → {KEY: value}."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def offers_mjpeg(text):
    """`v4l2-ctl --list-formats` output → True when the node can stream Motion-JPEG."""
    return "'MJPG'" in text


def find_camera(paths, udev, formats):
    """The BRIO color node among `paths`, or None.

    `udev(path)` and `formats(path)` return the udevadm property text and the v4l2-ctl format listing. Only capture
    nodes of the BRIO model are probed, in the given order, and the first that offers MJPEG wins: the IR node offers
    only GREY, so it is never returned.
    """
    for path in paths:
        props = parse_udev(udev(path))
        if (props.get("ID_VENDOR_ID"), props.get("ID_MODEL_ID")) != BRIO:
            continue
        if ":capture:" not in props.get("ID_V4L_CAPABILITIES", ""):
            continue
        if offers_mjpeg(formats(path)):
            return path
    return None


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def discover():
    """The live devices record. The camera is its stable by-id path, or None when unplugged."""
    try:
        # index0 sorts first, so the IR node (index2) is only format-queried if the color node is missing.
        paths = sorted(os.path.join(BY_ID, name) for name in os.listdir(BY_ID) if "-video-index" in name)
    except OSError:
        paths = []
    camera = find_camera(paths, lambda p: _run("udevadm", "info", "--query=property", "--name", p),
                         lambda p: _run("v4l2-ctl", "-d", p, "--list-formats"))
    return {"camera": camera}
