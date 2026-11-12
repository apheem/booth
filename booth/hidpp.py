"""The PRO X Wireless's battery over HID++, through its hidraw node. No answer within TIMEOUT_S, or an error answer,
means the headset is off: its USB dongle stays plugged in (and in PipeWire) when the headset is off.

The battery request, the reply's layout and the voltage → percentage curve are ported from HeadsetControl
(https://github.com/Sapd/HeadsetControl, GPL-3.0-or-later, commit 0c8a76fa019cd02263a1c69e00e18e229b16d87f):
lib/devices/logitech_gpro.hpp (the request 0x06 0x0d and usage page 0xff43), lib/devices/protocols/hidpp_protocol.hpp
(sendHIDPPCommand and requestBatteryHIDPP: the long-report framing, the 0xff "offline" byte, the reply's mV and
charging bytes), lib/devices/protocols/logitech_calibrations.hpp (the G PRO curve) and lib/utility.cpp
(spline_battery_level). Booth is GPL-3.0 too. HeadsetControl itself is not used.

Only this one request is ever sent, and only to the node whose sysfs ids are 046d:0aba and whose report descriptor
declares the HID++ usage page; the node is resolved on every query (hidraw numbers change on replug)."""
import os
import select
import time

VENDOR, PRODUCT = 0x046D, 0x0ABA
USAGE_PAGE = 0xFF43  # HeadsetControl's capability detail for CAP_BATTERY_STATUS on the G PRO series
SYSFS = "/sys/class/hidraw"
TIMEOUT_S = 1.0
LONG, RECEIVER, LENGTH = 0x11, 0xFF, 20  # HIDPP_LONG_MESSAGE, HIDPP_DEVICE_RECEIVER, HIDPP_LONG_MESSAGE_LENGTH
FEATURE = bytes([0x06, 0x0D])  # LogitechGPro::getBattery
REQUEST = bytes([LONG, RECEIVER]) + FEATURE + bytes(LENGTH - 4)
PERCENTAGES = (100, 50, 30, 20, 5, 0)  # calibrations::LOGITECH_GPRO
VOLTAGES = (4150, 3830, 3780, 3740, 3670, 3320)
OFF = "off"


def usage_pages(descriptor):
    """A HID report descriptor's bytes → every Usage Page its items declare."""
    pages, i = set(), 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # a long item: its data size is the next byte
            i += 3 + (descriptor[i + 1] if i + 1 < len(descriptor) else 0)
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        if prefix & 0xFC == 0x04:  # global item, tag 0: Usage Page
            pages.add(int.from_bytes(descriptor[i + 1:i + 1 + size], "little"))
        i += 1 + size
    return pages


def _ids(uevent):
    """sysfs uevent text → (vendor, product) from HID_ID=bus:vendor:product, or None."""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            _bus, vendor, product = line[7:].split(":")
            return int(vendor, 16), int(product, 16)
    return None


def find_node(root=SYSFS):
    """The PRO X's HID++ hidraw node ("/dev/hidrawN") now, or None."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        device = os.path.join(root, name, "device")
        try:
            with open(os.path.join(device, "uevent")) as f:
                if _ids(f.read()) != (VENDOR, PRODUCT):
                    continue
            with open(os.path.join(device, "report_descriptor"), "rb") as f:
                if USAGE_PAGE in usage_pages(f.read()):
                    return f"/dev/{name}"
        except (OSError, ValueError):  # gone mid-scan, or not a HID uevent
            continue
    return None


def answer(report):
    """One report read from the node → (mV, charging) for the battery reply, OFF for its error reply (the dongle's
    "headset off") or a voltage below the curve (HeadsetControl: offline or invalid), None for any other report."""
    if len(report) < 7 or report[0] != LONG or report[1] != RECEIVER:
        return None
    if report[2] == 0xFF:  # HID++ 2.0 error: 0xff, then the feature and function it answers
        return OFF if report[3:5] == FEATURE else None
    if report[2:4] != FEATURE:
        return None
    mv = report[4] << 8 | report[5]
    return OFF if mv < VOLTAGES[-1] else (mv, report[6] == 0x03)


def estimate(mv):
    """Battery voltage in mV → an estimated percentage on the G PRO curve (spline_battery_level)."""
    for i, v in enumerate(VOLTAGES):
        if mv >= v:
            return PERCENTAGES[i]
        if i + 1 < len(VOLTAGES) and mv >= VOLTAGES[i + 1]:
            fraction = (mv - VOLTAGES[i + 1]) / (v - VOLTAGES[i + 1])
            return PERCENTAGES[i + 1] + int(fraction * (PERCENTAGES[i] - PERCENTAGES[i + 1]))
    return 0


def read_reply(fd, timeout):
    """Read reports from the non-blocking `fd` until the battery answer → (mV, charging), or OFF at `timeout` s."""
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        if not select.select([fd], [], [], left)[0]:
            break
        try:
            got = answer(os.read(fd, 64))
        except BlockingIOError:
            continue
        if got is not None:
            return got
    return OFF


def battery(find=find_node, open_=os.open, write=os.write, timeout=TIMEOUT_S):
    """Ask the PRO X for its battery now → (mV, charging), or None when it is off, unplugged or doesn't answer.
    Blocks up to ~`timeout` s: call it off the main loop."""
    node = find()
    if node is None:
        return None
    try:
        fd = open_(node, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        write(fd, REQUEST)
        got = read_reply(fd, timeout)
    except OSError:  # unplugged mid-query
        return None
    finally:
        os.close(fd)
    return None if got == OFF else got
