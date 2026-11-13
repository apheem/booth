"""State store: Booth's settings as JSON in the user's XDG config folder, with defaults and a schema version.

A missing file loads defaults. A corrupt one is renamed to a backup (never deleted) and defaults load. Saves are
atomic, so a crash mid-write never leaves a half-written file.
"""
import json
import os
import tempfile
import time
from pathlib import Path

VERSION = 1


def defaults():
    # camera: {control name: value}, v4l2 names plus the extension units' "fov" and "led_mode"; re-applied at startup.
    # headphones: {"previous_default": the sink's node name "Use for all sound" replaced, put back when it is turned off;
    #   "sidetone": 0..128 and "auto_off": minutes 0..90 (0 = never), sent to the PRO X whenever it turns on}
    # microphone: {"input": "XLR" or "Instrument", the Scarlett input that is the mic; XLR when absent}. Never 48V.
    # voice: {"enabled": the Voice processing master switch, "rumble": the rumble filter}, booleans, True when absent.
    # routing: the routing policy's own record, {} or {"moved": True, "direct": Direct Monitor's value before Booth
    #   moved output to the Scarlett, put back when the PRO X turns on}; written only by booth.routing.
    return {"version": VERSION, "camera": {}, "headphones": {}, "microphone": {}, "voice": {}, "routing": {}}


def _whole(value, top):
    return type(value) is int and 0 <= value <= top  # not a bool (JSON true), not a float (64.0)


def path():
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):  # the XDG spec: a relative path is invalid and ignored
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "booth" / "settings.json"


def load(file=None):
    file = Path(file or path())
    try:
        raw = file.read_bytes()
    except FileNotFoundError:
        return defaults()
    except IsADirectoryError:
        raw = b""  # a folder where the file goes: moved aside below, so the next save can write the file
    except OSError:  # EIO, EACCES, EMFILE…: the file may well be fine, so it stays exactly where it is
        return defaults()
    try:
        data = json.loads(raw.decode())
        camera = data.get("camera", {}) if isinstance(data, dict) else None
        if not isinstance(camera, dict) or not all(isinstance(v, int) for v in camera.values()):
            raise ValueError("not a settings object")
        headphones = data.get("headphones", {})
        if not isinstance(headphones, dict) or not isinstance(headphones.get("previous_default", ""), str) \
                or not _whole(headphones.get("sidetone", 0), 128) or not _whole(headphones.get("auto_off", 0), 90):
            raise ValueError("not a settings object")
        microphone = data.get("microphone", {})
        if not isinstance(microphone, dict) or microphone.get("input", "XLR") not in ("XLR", "Instrument"):
            raise ValueError("not a settings object")
        voice = data.get("voice", {})
        if not isinstance(voice, dict) or not all(isinstance(voice.get(k, True), bool) for k in ("enabled", "rumble")):
            raise ValueError("not a settings object")
        routing = data.get("routing", {})
        if not isinstance(routing, dict) or not all(isinstance(routing.get(k, True), bool) for k in ("moved", "direct")):
            raise ValueError("not a settings object")
        json.dumps(data, indent=2)  # as save() writes it: nested too deep to save would fail every save after
    except (ValueError, RecursionError):  # bad JSON or UTF-8, wrong shape, nested too deep
        try:
            os.replace(file, f"{file}.corrupt-{time.time_ns()}")
        except OSError:
            pass  # can't move it aside (read-only folder): it stays as it is, and Booth still starts
        return defaults()
    return defaults() | data


def save(settings, file=None):
    file = Path(file or path())
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", dir=file.parent, prefix=f".{file.name}.", delete=False)
    try:
        with tmp:
            json.dump(settings, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, file)
    except BaseException:
        os.unlink(tmp.name)
        raise
