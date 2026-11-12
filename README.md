# Booth

One window for a desk setup: a Logitech BRIO camera, a Focusrite Scarlett microphone interface and a Logitech PRO X
headset. GNOME / libadwaita, Python, nothing to compile. GPL-3.0.

This first version has the app shell and a **Zoom** slider for the BRIO; the Microphone and Headphones pages are
placeholders.

## Requirements

Python 3.12, PyGObject, GTK 4.14, libadwaita 1.5 and `v4l2-ctl` (package `v4l-utils`), all from the system.
No pip installs, no sudo.

## Install

    ./install.sh

It copies the repo app folder `booth/` to the install folder `~/.local/share/booth/booth/` (a real copy, never a
symlink), and writes the launcher `~/.local/bin/booth` and the app-grid entry
`~/.local/share/applications/io.github.apheem.Booth.desktop`. It also writes the systemd user unit
`~/.config/systemd/user/booth.service` (`booth --background`, started with your desktop session; it re-applies the
camera settings at login and whenever the BRIO is plugged back in), enables it and (re)starts it. Opening Booth then
shows the window of that running instance; "Run in Background" in the main menu disables or enables the service; on, it also
keeps the open Booth running after its window closes.
Re-running it replaces the previous install.
To check an install matches the repo:

    diff -rq -x __pycache__ booth ~/.local/share/booth/booth

## Run

    booth                 # the installed launcher (needs ~/.local/bin on PATH), or "Booth" in the app grid
    python3 -m booth      # straight from the repo root, without installing

Settings are saved to `~/.config/booth/settings.json` (`$XDG_CONFIG_HOME/booth/settings.json`). A corrupt file is
renamed to `settings.json.corrupt-<time>` and defaults load.

## Test

    bash tools/run/sandbox.sh python3 -m unittest discover -s tests -t .

The tests use only the standard-library `unittest`, read captured fixtures instead of devices, and need no hardware.
The sandbox removes the display, session bus and PipeWire, and gives a throwaway home folder, so no window opens and
your real settings and install are never touched.
