# Booth — agent rules

Booth runs on a live desk: the camera, the Scarlett and the headset may be in use on a call right now.
These rules apply to every agent working in this repo. Where tools/run/blocks.md is stricter, it wins.

- **Never open the BRIO IR node.** Face login uses it. Find the camera's color node by model (USB `046d:085e`) and
  the MJPEG format; never stream from, or hold open, the node that offers only GREY.
- **Never restart PipeWire or WirePlumber, and never touch system config. No sudo.** Everything Booth installs or
  writes lives under the user's home folder.
- **After any live test, put back every default sink/source and mixer value you changed.** Read each value first and
  restore it before you finish. **48V phantom power must never be left on** (and never turned on without the user).
- **Tests and smoke checks never open windows on the user's display.** Run tests through
  `bash tools/run/sandbox.sh …`, which removes the display, the session bus and PipeWire.
- **No device serials, real camera frames or third-party binaries are ever committed.** Fixtures use the one shared
  fake serial `FAKESER1`; `tests/test_fixtures.py` fails on any other serial.
- **Resolve devices by name or model every time**, because ALSA card numbers and PipeWire node IDs change on replug.
  Never cache a `/dev/videoN`, a card number or a node id across events.

## Layout

- `booth/` — the app: pure logic modules (`devices.py`, `camera.py`, `store.py`) and a thin GTK layer
  (`app.py`, `ui/`). Keep logic out of the widgets so it can be unit-tested.
- `tests/` — standard-library `unittest`; fixtures under `tests/fixtures/` are captured device output.
- `install.sh` — per-user install (see README).
- `tools/run/` — the agent executor's gate, sandbox and mutation runner.

## Commands

- Tests: `bash tools/run/sandbox.sh python3 -m unittest discover -s tests -t .`
- Gate: `bash tools/run/gate.sh`
