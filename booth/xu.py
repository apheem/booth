"""The BRIO controls that live in UVC extension units (XUs), not in v4l2: field of view and LED mode. Read and set with
the kernel's UVCIOC_CTRL_QUERY ioctl on the color node; each XU's unit id is found by GUID in the USB descriptors."""
import ctypes
import fcntl
import os
import uuid

SET_CUR, GET_CUR, GET_DEF = 0x01, 0x81, 0x87

# name → (XU GUID, selector, payload length, the payload byte that holds the value)
CONTROLS = {
    "fov": ("49e40215-f434-47fe-b158-0e885023e51b", 0x05, 1, 0),
    "led_mode": ("ffe52d21-8030-4e2c-82d9-f587d00540bd", 0x09, 5, 1),  # Logitech peripheral XU, LED1: [?, mode, …]
}
MENUS = {"fov": {0: "90°", 1: "78°", 2: "65°"}, "led_mode": {0: "Off", 1: "On", 2: "Blink", 3: "Auto"}}
DEFAULTS = {"led_mode": 3}  # the BRIO answers GET_DEF on the LED with zeros (Off); Auto is its power-on mode


class Query(ctypes.Structure):  # struct uvc_xu_control_query, <linux/uvcvideo.h>
    _fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8), ("query", ctypes.c_uint8),
                ("size", ctypes.c_uint16), ("data", ctypes.c_void_p)]


CTRL_QUERY = 0xC0000000 | ctypes.sizeof(Query) << 16 | ord("u") << 8 | 0x21  # _IOWR('u', 0x21, struct …)


def units(desc):
    """A raw USB config descriptor → {XU GUID: (unit id, {selectors in its bmControls})}, from the video-control
    interface only (0x24/0x06 elsewhere is an MJPEG format or an audio descriptor)."""
    found, i, vc = {}, 0, False
    while i + 1 < len(desc) and desc[i]:
        d = desc[i:i + desc[i]]
        if d[1] == 0x04 and len(d) > 6:  # interface: UVC VideoControl is class 0x0e, subclass 0x01
            vc = (d[5], d[6]) == (0x0E, 0x01)
        elif vc and d[1:3] == b"\x24\x06" and len(d) > 22 and len(d) >= 23 + d[21] + d[22 + d[21]]:
            pins = d[21]
            size = d[22 + pins]
            bits = int.from_bytes(d[23 + pins:23 + pins + size], "little")
            found[str(uuid.UUID(bytes_le=bytes(d[4:20])))] = (d[3], {s + 1 for s in range(size * 8) if bits >> s & 1})
        i += desc[i]
    return found


def find(desc, name):
    """The unit id that holds control `name` on this camera, or None when its XU or selector is missing."""
    guid, selector = CONTROLS[name][:2]
    unit, selectors = units(desc).get(guid, (None, set()))
    return unit if selector in selectors else None


def decode(name, payload):
    return payload[CONTROLS[name][3]]


def encode(name, current, value):
    """The payload that sets `name` to `value`: `current` (GET_CUR) with only the value's byte changed."""
    payload = bytearray(current)
    payload[CONTROLS[name][3]] = value
    return bytes(payload)


def descriptors(node):
    """The raw USB descriptors of the camera behind `node`, resolved on every call (the video node's USB device)."""
    with open(f"/sys/class/video4linux/{os.path.basename(os.path.realpath(node))}/device/../descriptors", "rb") as f:
        return f.read()


def ioctl(fd, unit, selector, query, data):
    """One UVCIOC_CTRL_QUERY → the data buffer after it. OSError when the camera refuses (EPIPE, EINVAL, ENOENT)."""
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    fcntl.ioctl(fd, CTRL_QUERY, Query(unit, selector, query, len(data), ctypes.addressof(buf)))
    return buf.raw


def model(desc, query):
    """{name: control model, like camera.parse_ctrls gives} for the XU controls this camera answers, current values
    read back. `query(unit, selector, query, data)` runs one ioctl. A control it lacks is left out: no row for it."""
    ctrls = {}
    for name, (_, selector, size, _) in CONTROLS.items():
        unit = find(desc, name)
        if unit is None:
            continue
        try:  # the kernel checks the payload length against the camera's GET_LEN: another length is refused
            value = decode(name, query(unit, selector, GET_CUR, bytes(size)))
            default = DEFAULTS[name] if name in DEFAULTS else decode(name, query(unit, selector, GET_DEF, bytes(size)))
        except OSError:
            continue
        menu = dict(MENUS[name])
        ctrls[name] = {"type": "menu", "inactive": False, "min": min(menu), "max": max(menu), "step": 1,
                       "default": default, "value": value, "menu": menu}
    return ctrls


def read(node):
    """The XU controls of the camera at `node` (its color node), or {} when it can't be read (unplugged)."""
    try:
        desc = descriptors(node)
        fd = os.open(node, os.O_RDWR)
    except OSError:
        return {}
    try:
        return model(desc, lambda *a: ioctl(fd, *a))
    finally:
        os.close(fd)


def write(node, name, value):
    """Set `name` to `value` (GET_CUR, then SET_CUR with only its byte changed) → False when the camera lacks it.
    OSError when the camera is gone or refuses the value."""
    unit = find(descriptors(node), name)
    if unit is None:
        return False
    _, selector, size, _ = CONTROLS[name]
    fd = os.open(node, os.O_RDWR)
    try:
        ioctl(fd, unit, selector, SET_CUR, encode(name, ioctl(fd, unit, selector, GET_CUR, bytes(size)), value))
    finally:
        os.close(fd)
    return True
