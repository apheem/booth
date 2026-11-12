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
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):  # the XDG spec: a relative path is invalid and ignored
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "booth" / "settings.json"


def load(file=None):
    file = Path(file or path())
    try:
        data = json.loads(file.read_text())
        camera = data.get("camera", {}) if isinstance(data, dict) else None
        if not isinstance(camera, dict) or not all(isinstance(v, int) for v in camera.values()):
            raise ValueError("not a settings object")
    except FileNotFoundError:
        return defaults()
    except (ValueError, RecursionError, OSError):  # bad JSON or UTF-8, wrong shape, nested too deep, a dir, unreadable
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
