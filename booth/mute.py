"""One mute (#21): Booth Mic's node mute (booth_mic's Props "mute"), the one GNOME's mic-mute action toggles on the
default source, the one `wpctl set-mute` sets, and the one Booth's header button, sidebar dot and Ctrl+M show and set.
Booth reads it from `pw-dump --monitor`, which prints booth_mic's node whenever it changes: no poll, window or not.
Booth keeps a record of it (settings voice "muted") and Booth Mic's chain follows that record (booth.voice: muted, its
master mixer is silent), so a Booth Mic recreated by a booth-voice.service restart starts silent and Booth then puts
its node mute back (seen()). Booth never unmutes a node it sees for the first time. Booth Stereo (#19) isn't muted.
    python3 -m booth.mute --bind   install.sh: GNOME's mic-mute on Ctrl+Alt+M, only when it has no binding"""
import ast
import json
import subprocess
import sys
import time

from booth import service, voice

SCHEMA, KEY = "org.gnome.settings-daemon.plugins.media-keys", "mic-mute"
SHORTCUT = "<Control><Alt>m"
WATCH = ["pw-dump", "--monitor", "--no-colors"]
RETRY_S = 5  # a monitor that ended (PipeWire restarted or not up yet) starts again this much later
UNBOUND = "install.sh: couldn't set GNOME's mic-mute shortcut; set Ctrl+Alt+M in Settings → Keyboard → Microphone Mute"


def binding(current):
    """`gsettings get` of mic-mute (None: unreadable) → the value to set it to, or None: left as it is. Only an empty
    binding ([], or [''], GNOME 46's default) gets Ctrl+Alt+M: never the user's own, never one it can't read."""
    text = (current or "").strip()
    try:
        keys = ast.literal_eval(text[4:] if text.startswith("@as ") else text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(keys, list) or any(k != "" for k in keys):
        return None
    return f"['{SHORTCUT}']"


def bind(run=subprocess.run):
    """install.sh: mic-mute set to Ctrl+Alt+M when binding() says so → None, or what to tell the user."""
    try:
        got = run(["gsettings", "get", SCHEMA, KEY], capture_output=True, text=True, timeout=10)
        if got.returncode != 0:
            return UNBOUND
        new = binding(got.stdout)
        if new is not None and run(["gsettings", "set", SCHEMA, KEY, new], capture_output=True, text=True,
                                   timeout=10).returncode != 0:
            return UNBOUND
    except (OSError, subprocess.SubprocessError):
        return UNBOUND
    return None


def find(objects):
    """Booth Mic's node in pw-dump's objects → (id, object.serial, muted), muted None when its Props aren't there (never
    read as unmuted); None without it."""
    for o in objects if isinstance(objects, list) else []:
        info = o.get("info") if isinstance(o, dict) else None
        if isinstance(info, dict) and (info.get("props") or {}).get("node.name") == voice.NODE:
            props = [p for p in (info.get("params") or {}).get("Props") or [] if isinstance(p, dict) and "mute" in p]
            return o.get("id"), info["props"].get("object.serial", o.get("id")), \
                (any(p["mute"] is True for p in props) if props else None)
    return None


def seen(found, known, muted):
    """Booth Mic's node as pw-dump shows it (find's), `known`: the object.serial whose mute Booth follows, `muted`: Booth's
    record → (known, record, write): `write` the mute to set on the node now, None: nothing. A followed node's mute is
    the record, whoever set it (GNOME, another app, Booth). A node not followed yet (recreated by a booth-voice restart,
    or Booth just started) is muted if the record or the node is, never unmuted, and followed once it shows that."""
    _id, serial, mute = found
    if mute is None:
        return known, muted, None
    if serial == known:
        return known, mute, None
    want = bool(muted or mute)
    return (serial, want, None) if mute == want else (known, want, want)


def write(on, run=service.run, dump=voice._dump):
    """Booth Mic's node mute set to `on`, the node found by name now (never a cached id) → True when wpctl took it."""
    try:
        node = find(json.loads(dump()))
    except ValueError:
        node = None
    return node is not None and run(["wpctl", "set-mute", str(node[0]), "1" if on else "0"])


def batches(lines):
    """pw-dump --monitor's output → each JSON array it prints (one per change, closed by a "]" alone on its line)."""
    buf = []
    for line in lines:
        buf.append(line)
        if line.rstrip("\n") == "]":
            try:
                yield json.loads("".join(buf))
            except ValueError:
                pass
            buf = []


def watch(found, popen=subprocess.Popen, sleep=time.sleep):
    """For good, on a worker: found(find's) whenever pw-dump's monitor shows Booth Mic's node (at its start, then on
    every change of it); a monitor that ends is started again RETRY_S later."""
    while True:
        try:  # shortcut: Booth quitting leaves pw-dump until its next print (then EPIPE ends it); kill it if that matters
            with popen(WATCH, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as p:
                for batch in batches(p.stdout):
                    if (node := find(batch)) is not None:
                        found(node)
        except OSError:
            pass
        sleep(RETRY_S)


def main():
    if sys.argv[1:] == ["--bind"]:
        said = bind()
        if said:
            print(said, file=sys.stderr)
        return 0  # never stops the install
    print("usage: python3 -m booth.mute --bind", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
