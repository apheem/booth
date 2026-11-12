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
    # camera: {v4l2 control name: value}, re-applied to the camera at startup.
    return {"version": VERSION, "camera": {}}


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "booth" / "settings.json"


def load(file=None):
    file = Path(file or path())
    try:
        data = json.loads(file.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("camera", {}), dict):
            raise ValueError("not a settings object")
    except FileNotFoundError:
        return defaults()
    except ValueError:  # bad JSON, bad UTF-8 or the wrong shape
        os.replace(file, f"{file}.corrupt-{time.time_ns()}")
        return defaults()
    return defaults() | data


def save(settings, file=None):
    file = Path(file or path())
    file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=file.parent, prefix=f".{file.name}.", delete=False) as tmp:
        json.dump(settings, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, file)
