# Booth

One window for a desk setup: a Logitech BRIO camera, a Focusrite Scarlett microphone interface and a Logitech PRO X
headset. GNOME / libadwaita, Python, nothing to compile. GPL-3.0.

The **Camera** page has every BRIO control (framing, image, focus, field of view and LED), resettable and saved; a
background service re-applies them at login and on replug. The Microphone and Headphones pages are placeholders.

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
Re-running it replaces the previous install and keeps your "Run in Background" choice: it restarts the service only if
it is enabled, and never enables one you switched off. A Booth it had to close and didn't restart is named, as is a
service state it couldn't read (no user bus), with the command to restart it by hand.
It also writes and enables `~/.config/systemd/user/booth-voice.service`, which runs **Booth Mic** (`pipewire -c
~/.config/booth/voice.conf`, the voice chain Booth writes from your settings) with your PipeWire, whether Booth runs or
not, and `booth-mic-select.service` (`pipewire -c ~/.config/booth/voice-select.conf`), which feeds it the Scarlett's
mic channel; only that one restarts when the mic channel changes. The first install makes Booth Mic the default mic; a
re-install leaves your choice alone. With no Scarlett, Booth Mic is silent until Booth sees it again; with the mic routed
to no capture channel, Booth stops `booth-mic-select.service` and Booth Mic is silent until it is routed again.
`booth-monitor.service` (`pipewire -c ~/.config/booth/monitor.conf`) plays Booth Mic in the PRO X headset while the
Microphone page's "Hear Processed Voice in Headphones" switch is on; it is never enabled: Booth starts and stops it.
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
