"""Shared fixture helpers. Every fixture uses the one fake serial below."""
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_SERIAL = "FAKESER1"
BY_ID = "/dev/v4l/by-id"


def read(name):
    return (FIXTURES / name).read_text()


def brio_nodes():
    """The four BRIO by-id entries as captured: index0 color (MJPEG), 1 and 3 metadata, 2 IR (GREY only)."""
    return {f"{BY_ID}/usb-046d_Logitech_BRIO_{FAKE_SERIAL}-video-index{n}": n for n in range(4)}
