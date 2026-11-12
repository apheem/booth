"""The Scarlett Solo 4th Gen: its controls (48V, Air, Inst/Line, Direct Monitor, firmware, sync) through amixer, and the
routing that tells which capture channel carries each input.

The card is found by its full name on every use and addressed by its ALSA id (`amixer -c Gen`); controls by their
name, never a numid. The kernel's scarlett2 driver names them. On the Solo the XLR input is Analogue 2, routed through
DSP 2 to capture channel 2 (FR on PipeWire's 4-channel source); channel 1 is the instrument jack, 3–4 are loopback.

48V: Booth turns it on only from the user's click on the switch, after they confirm the dialog. It never saves it,
restores it or turns it on from a poll. cset_cmd() refuses phantom on without `confirmed=True`, which only the dialog
passes."""
import json
import re

from booth import devices

PHANTOM = "Line In 2 Phantom Power Capture Switch"
AIR = "Line In 2 Air Capture Enum"
LEVEL = "Line In 1 Level Capture Enum"
DIRECT = "Direct Monitor Playback Switch"
FIRMWARE = "Firmware Version"
SYNC = "Sync Status"
WRITABLE = (PHANTOM, AIR, LEVEL, DIRECT)  # never the routing enums or the mixes: they carry the user's mic
SWITCHES = (PHANTOM, DIRECT)
INPUTS = {"XLR": "Analogue 2", "Instrument": "Analogue 1"}  # Booth's choice → the routing's source name
DEFAULT_INPUT = "XLR"
SOURCE = "alsa_input.usb-Focusrite_Scarlett_Solo_4th_Gen_"  # its PipeWire capture node's name, then serial and profile
UNPLUGGED = "Scarlett not connected — plug it in"

_NAME = re.compile(r"name='(.*)'")
_ITEM = re.compile(r"^\s*; Item #\d+ '(.*)'$")
_VALUES = re.compile(r"^\s*: values=(.*)$")


def _value(v):
    if v in ("on", "off"):
        return v == "on"
    try:
        return int(v)
    except ValueError:
        return v


def parse_contents(text):
    """`amixer contents` output → {control name: {"items": enum item names, or [], "values": [int or bool]}}."""
    ctrls, ctrl = {}, None
    for line in text.splitlines():
        if line.startswith("numid="):
            m = _NAME.search(line)
            ctrl = ctrls.setdefault(m.group(1), {"items": [], "values": []}) if m else None
        elif ctrl is not None and (m := _ITEM.match(line)):
            ctrl["items"].append(m.group(1))
        elif ctrl is not None and (m := _VALUES.match(line)):
            ctrl["values"] = [_value(v) for v in m.group(1).split(",")]
    return ctrls


def value(ctrls, name):
    """The control's (first) value, or None when the card has no such control."""
    values = (ctrls.get(name) or {}).get("values")
    return values[0] if values else None


def text(ctrls, name):
    """The value as shown: an enum's item name ("Locked"), or the number ("2188")."""
    v, items = value(ctrls, name), (ctrls.get(name) or {}).get("items") or []
    if v is None:
        return "Unknown"
    return items[v] if items and isinstance(v, int) and 0 <= v < len(items) else str(v)


def routing(ctrls):
    """The `PCM 0N Capture Enum` routing → {"XLR": capture channel, "Instrument": capture channel} (1-based), the
    lowest channel each input reaches directly or through its DSP input. An input on no channel is absent; a mix
    (loopback) is never followed."""
    found, n = {}, 1
    while (pcm := f"PCM {n:02d} Capture Enum") in ctrls:
        source = text(ctrls, pcm)
        if source.startswith("DSP "):
            source = text(ctrls, f"DSP Input {source[4:]} Capture Enum")
        for choice, analogue in INPUTS.items():
            if source == analogue:
                found.setdefault(choice, n)
        n += 1
    return found


def _source(dump):
    """`pw-dump` JSON → (the Scarlett capture node's name, its channel positions), or (None, [])."""
    try:
        objects = json.loads(dump)
    except ValueError:
        return None, []
    for o in objects if isinstance(objects, list) else []:
        props = (o.get("info") or {}).get("props") or {}
        name = props.get("node.name") or ""
        if (o.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Source"
                and name.startswith(SOURCE)):
            return name, str(props.get("audio.position") or "").split(",")
    return None, []


def mic(ctrls, dump, choice):
    """The chosen input as the meters and the voice chain take it: {"input": "XLR", "channel": 2, "position": "FR",
    "node": the PipeWire source's name}, position and node None when PipeWire has no Scarlett source; None when the
    input reaches no capture channel."""
    channel = routing(ctrls).get(choice)
    if channel is None:
        return None
    node, positions = _source(dump)
    position = positions[channel - 1] if node and channel <= len(positions) else None
    return {"input": choice, "channel": channel, "position": position, "node": node}


def describe(record):
    """The Input row's subtitle."""
    if record is None:
        return "Not routed to a capture channel"
    return f"Capture channel {record['channel']}" + (f" ({record['position']})" if record["position"] else "")


def read(choice, run=devices._run, card=devices.scarlett_card):
    """The Scarlett now → {"ctrls": parse_contents, "mic": mic(choice)}, or None when it is unplugged."""
    card_id = card()
    if card_id is None:
        return None
    ctrls = parse_contents(run("amixer", "-c", card_id, "contents"))
    if not ctrls:  # unplugged between the card list and amixer
        return None
    return {"ctrls": ctrls, "mic": mic(ctrls, run("pw-dump"), choice)}


def cset_cmd(card, name, value, confirmed=False):
    """The amixer write of one of the page's controls by name: a switch takes a bool, an enum its item index. None for
    any other control or value, and for 48V on unless `confirmed is True` (only the confirmation dialog passes it)."""
    if name not in WRITABLE or isinstance(value, bool) != (name in SWITCHES) or not isinstance(value, int):
        return None
    if name == PHANTOM and value and confirmed is not True:
        return None
    return ["amixer", "-c", card, "cset", f"name={name}", ("on" if value else "off") if name in SWITCHES else str(value)]


def send(make, run=devices._run, card=devices.scarlett_card):
    """Resolve the card now and run make(card id)'s command, if any (nothing when unplugged)."""
    card_id = card()
    cmd = make(card_id) if card_id is not None else None
    if cmd:
        run(*cmd)
