"""Fetch and verify the only third-party code Booth loads: RNNoise and two SWH LADSPA plugins.

fetch() downloads them once into Booth's data folder (install.sh runs it; nothing auto-updates them). verify() is the
trust boundary: a plugin may be loaded only from the path it returns, which holds bytes whose sha256 is pinned below.
    python3 -m booth.plugins [folder]    fetch what's missing or refused, then print each file's verdict
"""
import ctypes
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

# The pins. Each was hashed from the download and cross-checked against what upstream publishes (see PR #14's body).
MANIFEST = {
    "librnnoise_ladspa.so": "ba2b420c76cd2f1e7684430c96fef8c533041d5f67b94060f8eeda6af1701f97",
    "sc4m_1916.so": "e4a56d8a48e8a51a1ef710338a7b0f4feaa3c4736f9470b16c4bf0ff41684674",
    "fast_lookahead_limiter_1913.so": "e64806aee4f2f6a3c1c511e2e61082d57224ec3c87409da1e4195ee63f828674",
}
SWH_VERSION = "0.4.17-3build1"  # the swh-plugins version the two SWH pins above were hashed from
RNNOISE_URL = "https://github.com/werman/noise-suppression-for-voice/releases/download/v1.10/linux-rnnoise.zip"
RNNOISE_MEMBER = "linux-rnnoise/ladspa/librnnoise_ladspa.so"
SWH_FILES = ("sc4m_1916.so", "fast_lookahead_limiter_1913.so")  # from the Ubuntu archive's swh-plugins, not installed


class Refused(Exception):
    """A plugin file that must not be loaded."""


def folder():
    base = os.environ.get("XDG_DATA_HOME", "")
    if not os.path.isabs(base):  # the XDG spec: a relative path is invalid and ignored
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "booth" / "plugins"


def verify(name, where=None, manifest=MANIFEST):
    """The path of `name` in `where` if it's a regular file (never a symlink) whose sha256 is the pinned one."""
    if name not in manifest or os.path.basename(name) != name:
        raise Refused(f"{name}: not a pinned plugin")
    path = Path(where or folder()) / name
    try:  # O_NOFOLLOW: a symlink fails here; O_NONBLOCK: a fifo can't hang the open
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Refused(f"{name}: missing") from None
    except OSError as e:
        raise Refused(f"{name}: can't open ({e.strerror})") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refused(f"{name}: not a regular file")
    with os.fdopen(fd, "rb") as f:
        sha = hashlib.file_digest(f, "sha256").hexdigest()
    if sha != manifest[name]:
        raise Refused(f"{name}: sha256 mismatch")
    return path


def _rnnoise(stage):
    zip_ = stage / "linux-rnnoise.zip"
    subprocess.run(["curl", "-fsSL", "--proto", "=https", "-o", zip_, RNNOISE_URL], check=True,
                   stdin=subprocess.DEVNULL, timeout=600)
    with zipfile.ZipFile(zip_) as z:
        (stage / "librnnoise_ladspa.so").write_bytes(z.read(RNNOISE_MEMBER))


def _swh(stage):
    subprocess.run(["apt-get", "download", f"swh-plugins={SWH_VERSION}"], cwd=stage, check=True,
                   stdin=subprocess.DEVNULL, timeout=600)
    deb, = stage.glob("*.deb")
    subprocess.run(["dpkg-deb", "-x", deb, stage / "deb"], check=True, stdin=subprocess.DEVNULL, timeout=600)
    for name in SWH_FILES:
        shutil.copyfile(stage / "deb/usr/lib/ladspa" / name, stage / name, follow_symlinks=False)


def fetch(where=None, manifest=MANIFEST):
    """Download each missing or refused file, place only what verifies, and return what failed (empty: all good)."""
    where = Path(where or folder())
    sources = [("RNNoise", _rnnoise, ["librnnoise_ladspa.so"]),
               (f"SWH plugins {SWH_VERSION}", _swh, list(SWH_FILES))]
    failed = []
    for what, download, names in sources:
        names = [n for n in names if n in manifest]
        todo = []
        for name in names:
            try:
                verify(name, where, manifest)
            except Refused:
                todo.append(name)
        if not todo:
            continue
        where.parent.mkdir(parents=True, exist_ok=True)
        # Staged beside the folder, so a failed or partial download never lands in it and the move is atomic.
        stage = Path(tempfile.mkdtemp(prefix=".plugins-fetch-", dir=where.parent))
        try:
            download(stage)
            for name in todo:
                try:
                    verify(name, stage, manifest)
                except Refused as e:
                    failed.append(f"{what}: {e}")
                    continue
                where.mkdir(exist_ok=True)
                os.replace(stage / name, where / name)
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile, zlib.error, KeyError,
                ValueError) as e:
            failed.append(f"{what}: download failed ({e})")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    return failed


class _Hint(ctypes.Structure):
    _fields_ = [("hint", ctypes.c_int), ("lower", ctypes.c_float), ("upper", ctypes.c_float)]


class _Descriptor(ctypes.Structure):  # ladspa.h's LADSPA_Descriptor, up to the fields read here
    _fields_ = [("id", ctypes.c_ulong), ("label", ctypes.c_char_p), ("props", ctypes.c_int),
                ("name", ctypes.c_char_p), ("maker", ctypes.c_char_p), ("copyright", ctypes.c_char_p),
                ("count", ctypes.c_ulong), ("descs", ctypes.POINTER(ctypes.c_int)),
                ("names", ctypes.POINTER(ctypes.c_char_p)), ("hints", ctypes.POINTER(_Hint))]


def ports(name, where=None, manifest=MANIFEST):
    """Every LADSPA descriptor in a verified plugin: label, id, name and its ports with their exact names."""
    lib = ctypes.CDLL(str(verify(name, where, manifest)))
    lib.ladspa_descriptor.restype = ctypes.POINTER(_Descriptor)
    lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
    out, i = [], 0
    while d := lib.ladspa_descriptor(i):
        d = d.contents
        ps = []
        for p in range(d.count):
            kind = d.descs[p]
            port = {"index": p, "name": d.names[p].decode(), "type": "control" if kind & 0x4 else "audio",
                    "direction": "input" if kind & 0x1 else "output"}
            h = d.hints[p]
            if h.hint & 0x1:  # LADSPA_HINT_BOUNDED_BELOW
                port["lower"] = h.lower
            if h.hint & 0x2:  # LADSPA_HINT_BOUNDED_ABOVE
                port["upper"] = h.upper
            ps.append(port)
        out.append({"label": d.label.decode(), "id": d.id, "name": d.name.decode(), "ports": ps})
        i += 1
    return out


def main():
    where = Path(sys.argv[1]) if len(sys.argv) > 1 else folder()
    for line in fetch(where, MANIFEST):
        print(f"booth.plugins: {line}", file=sys.stderr)
    ok = True
    for name in MANIFEST:
        try:
            verify(name, where, MANIFEST)
            print(f"{name}: verified")
        except Refused as e:
            ok = False
            print(f"{name}: refused ({str(e).removeprefix(name + ': ')})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
