"""Device discovery: finds devices by model or name on every call, never by a cached node number, card number or node
id (those change on replug)."""
import json
import os
import re
import subprocess

BY_ID = "/dev/v4l/by-id"
BRIO = ("046d", "085e")  # USB vendor, product
SND_BY_ID = "/dev/snd/by-id"  # ALSA control links; the PRO X dongle's: usb-Logitech_PRO_X_Wireless_Gaming_Headset-00
CARDS = "/proc/asound/cards"
PRO_X = "PRO X Wireless Gaming Headset"  # the dongle's ALSA card name
PRO_X_USB = "usb-Logitech_PRO_X_Wireless_Gaming_Headset-"  # in its by-id link and its PipeWire node names
SCARLETT = "Scarlett Solo 4th Gen"  # the interface's ALSA card name
_CARD = re.compile(r"^\s*\d+ \[(\S+)\s*\]: .* - (.*)$")


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


def replugged(name, event):
    """A by-id folder event (the entry's new name, the Gio event's nick) → True when a camera may be back: a color node
    (index0) appeared, or the folder itself, which udev removes with the last camera (Gio then reports only the folder,
    none of its entries). discover() decides whether it is the BRIO."""
    return event in ("created", "moved-in", "renamed") and (name.endswith("-video-index0") or name == "by-id")


def camera_moved(name, event):
    """A by-id folder event → True when the BRIO may have come or gone (#22's Camera page): a color node (index0) or the
    folder itself added or removed. discover() decides whether the BRIO is there."""
    return event in ("created", "deleted", "moved-in", "moved-out", "renamed") and (
        name.endswith("-video-index0") or name == "by-id")


def _run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def discover(run=_run, listdir=os.listdir):
    """The live devices record. The camera is its stable by-id path, or None when unplugged."""
    try:
        # Only a camera's index0 (the BRIO's color node) is ever queried: any query on index2 opens the IR node.
        paths = sorted(os.path.join(BY_ID, name) for name in listdir(BY_ID) if name.endswith("-video-index0"))
    except OSError:
        paths = []
    camera = find_camera(paths, lambda p: run("udevadm", "info", "--query=property", "--name", p),
                         lambda p: run("v4l2-ctl", "-d", p, "--list-formats"))
    return {"camera": camera}


def find_card(cards, name):
    """`/proc/asound/cards` text → the ALSA id (in brackets, e.g. "Headset") of the card called `name`, or None. The id,
    not the number: `amixer -c Headset` can't reach another card if the numbers shift between two commands."""
    for line in cards.splitlines():
        if (m := _CARD.match(line)) and m.group(2).strip() == name:
            return m.group(1)
    return None


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def headset_card(read=_read):
    """The PRO X dongle's ALSA card id now, or None when it is unplugged."""
    return find_card(read(CARDS), PRO_X)


def scarlett_card(read=_read):
    """The Scarlett's ALSA card id now (e.g. "Gen"), or None when it is unplugged."""
    return find_card(read(CARDS), SCARLETT)


def parse_sinks(text):
    """`pw-dump` JSON → ({node.name: (id, priority.session)} of the audio sinks, the default sink's node.name or None)."""
    try:
        objects = json.loads(text)
    except ValueError:
        return {}, None
    sinks, default = {}, None
    for o in objects if isinstance(objects, list) else []:
        props = (o.get("info") or {}).get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Sink":
            sinks[props.get("node.name")] = (o["id"], props.get("priority.session", 0))
        elif (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if m.get("key") == "default.audio.sink" and isinstance(m.get("value"), dict):
                    default = m["value"].get("name")
    return sinks, default


def find_sink(sinks):
    """The PRO X sink's node name among parse_sinks' sinks, or None."""
    return next((name for name in sinks if PRO_X_USB in (name or "")), None)


def dongle_appeared(name, event):
    """A /dev/snd/by-id event → True when the PRO X dongle may be back: its link appeared, or the folder itself (udev
    removes it with the last USB sound card). headset_card() decides whether it is there."""
    return event in ("created", "moved-in", "renamed") and (name.startswith(PRO_X_USB) or name == "by-id")


def dongle_present(listdir=os.listdir):
    """True while the PRO X dongle's /dev/snd/by-id link is there (by name, read now)."""
    try:
        return any(name.startswith(PRO_X_USB) for name in listdir(SND_BY_ID))
    except OSError:  # the folder goes with the last USB sound card
        return False
