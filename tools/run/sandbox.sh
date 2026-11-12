#!/bin/bash
# sandbox.sh — run a command with no way to reach the user's desktop session: no display (a window can't open), no
# session bus (no notifications, no `systemctl --user`), a throwaway XDG_RUNTIME_DIR (no PipeWire socket, so no
# wpctl / pw-dump against the real audio graph), in-memory gsettings (the mic-mute binding stays untouched), and a
# throwaway HOME and XDG folders (the real settings file and install are never read or written). Bytecode goes there
# too, so the tree stays clean. The gate and the mutation runner run every test through it.
#   bash tools/run/sandbox.sh <command> [args...]
# shortcut: /dev (the camera, hidraw, ALSA) is still reachable; tests must use fixtures. Add bwrap if one ever doesn't.
box=$(mktemp -d "${TMPDIR:-/tmp}/booth-sandbox-XXXX") || exit 2
trap 'rm -rf "$box"' EXIT
mkdir -p "$box"/{home,config,data,cache,state,run,pycache} && chmod 700 "$box/run"
env -u DISPLAY -u WAYLAND_DISPLAY -u DBUS_SESSION_BUS_ADDRESS -u PIPEWIRE_REMOTE -u PULSE_SERVER \
  HOME="$box/home" XDG_CONFIG_HOME="$box/config" XDG_DATA_HOME="$box/data" XDG_CACHE_HOME="$box/cache" \
  XDG_STATE_HOME="$box/state" XDG_RUNTIME_DIR="$box/run" GSETTINGS_BACKEND=memory \
  PYTHONPYCACHEPREFIX="$box/pycache" PYTHONDONTWRITEBYTECODE=1 "$@"
