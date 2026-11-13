#!/bin/sh
# install.sh — install Booth for this user: no sudo, nothing outside the home folder, safe to re-run.
# Copies the repo's booth/ to ~/.local/share/booth/booth (a real copy, never a symlink into the repo or a worktree),
# writes the launcher ~/.local/bin/booth and the app-grid entry ~/.local/share/applications/io.github.apheem.Booth.desktop.
set -eu
repo=$(cd "$(dirname "$0")" && pwd)
share=${XDG_DATA_HOME:-}
case $share in /*) ;; *) share=$HOME/.local/share ;; esac  # the XDG spec: a relative path is invalid and ignored
dest=$share/booth
bin=$HOME/.local/bin
mkdir -p "$dest" "$bin" "$share/applications"

# Stage then swap, so a failed copy never leaves a half-installed app and files removed from the repo don't linger.
rm -rf "$dest/booth.new"
cp -RL "$repo/booth" "$dest/booth.new"
find "$dest/booth.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$dest/booth"
mv "$dest/booth.new" "$dest/booth"

# The voice plugins, downloaded once into $dest/plugins (outside the app copy, which each run replaces) and verified
# against the repo's sha256 pins. A failure doesn't stop the install: Booth refuses to load an unverified plugin.
if ! PYTHONPATH=$dest python3 -P -m booth.plugins "$dest/plugins"; then
  echo "install.sh: some voice plugins couldn't be fetched or verified (above); Booth won't load those until ./install.sh is run again with a network" >&2
fi

# Single-quoted in the launcher, each ' as '\'', so any HOME (" $ ` \ ') reaches python3 unchanged.
dest_q=$(printf '%s' "$dest" | sed "s/'/'\\\\''/g")
cat > "$bin/booth.new" <<EOF
#!/bin/sh
export PYTHONPATH='$dest_q'"\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -P -m booth "\$@"
EOF
chmod 755 "$bin/booth.new"
mv "$bin/booth.new" "$bin/booth"

# Desktop-entry quoting: in a quoted Exec argument " ` $ \ take a backslash, itself escaped in the value; % is doubled.
exec_arg=$(printf '%s' "$bin/booth" | sed -e 's/\\/\\\\\\\\/g' -e 's/["`$]/\\\\&/g' -e 's/%/%%/g')
cat > "$share/applications/io.github.apheem.Booth.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Booth
Comment=Camera, microphone and headphones in one place
Exec="$exec_arg"
Icon=camera-web
Categories=AudioVideo;Settings;
StartupNotify=true
EOF

# booth.service runs the installed launcher with --background. It's wanted by graphical-session.target, not
# default.target: GTK needs the session's display, which default.target (also reached with no login screen) lacks.
# PartOf: it stops with the session. RestartSec spaces a crash's restarts (a --background start that finds Booth
# already running exits 0, so it is never restarted).
config=${XDG_CONFIG_HOME:-}
case $config in /*) ;; *) config=$HOME/.config ;; esac
units=$config/systemd/user
mkdir -p "$units"
first=  # the first install enables the unit; a re-install keeps what the "Run in background" switch chose
[ -e "$units/booth.service" ] || first=1
# Unit-file quoting: in a quoted word \ and " take a backslash, % is doubled and $ too. The executable is /bin/sh
# because systemd refuses an executable path with those characters; an argument may hold anything.
unit_arg=$(printf '%s' "$bin/booth" | sed -e 's/[\\"]/\\&/g' -e 's/%/%%/g' -e 's/\$/$$/g')
cat > "$units/booth.service.new" <<EOF
[Unit]
Description=Booth: keeps the camera settings applied, at login and on replug
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/bin/sh "$unit_arg" --background
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
mv "$units/booth.service.new" "$units/booth.service"

# booth-voice.service runs Booth Mic, the voice chain, as its own PipeWire client from the config Booth writes beside
# its settings, so Booth Mic stays in a call with Booth closed or crashed. It lives with the user's PipeWire. No config
# yet (the Scarlett never seen): ExecCondition skips it cleanly. Quoting as for ExecStart above.
voice_arg=$(printf '%s' "$config/booth/voice.conf" | sed -e 's/[\\"]/\\&/g' -e 's/%/%%/g' -e 's/\$/$$/g')
cat > "$units/booth-voice.service.new" <<EOF
[Unit]
Description=Booth Mic: the voice chain (a PipeWire filter-chain)
After=pipewire.service pipewire-session-manager.service
BindsTo=pipewire.service

[Service]
ExecCondition=/usr/bin/test -f "$voice_arg"
ExecStart=/usr/bin/pipewire -c "$voice_arg"
Restart=on-failure
RestartSec=5
Slice=session.slice

[Install]
WantedBy=pipewire.service
EOF
mv "$units/booth-voice.service.new" "$units/booth-voice.service"
# Enabled whatever the Run in Background switch says. booth.voice writes the config from the Scarlett now and starts
# it (restarts it when the config changed). Then Booth Mic is made the default mic by name: WirePlumber picks it as
# soon as it appears. None of this stops the install: Booth writes the config and starts the service each time it runs.
systemctl --user daemon-reload && systemctl --user enable booth-voice.service ||
  echo "install.sh: couldn't enable booth-voice.service (Booth Mic); from your desktop session run: systemctl --user enable --now booth-voice.service" >&2
PYTHONPATH=$dest python3 -P -m booth.voice || echo "install.sh: couldn't set up Booth Mic (above); Booth tries again when it starts" >&2
pw-metadata -n default 0 default.configured.audio.source '{ "name": "booth_mic" }' Spa:String:JSON >/dev/null 2>&1 ||
  echo "install.sh: couldn't make Booth Mic the default mic; choose Booth Mic in Settings → Sound → Input" >&2
# A running Booth owns its bus name, so the restarted unit's --background would find it and exit 0 with the old code
# still running: ask it to quit (app.quit; "not running" errors ignored) and wait up to 5 s for the name to go.
closed=
gdbus call --session --dest io.github.apheem.Booth --object-path /io/github/apheem/Booth \
  --method org.gtk.Actions.Activate quit '[]' '{}' >/dev/null 2>&1 && closed=1
owned() {
  gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner io.github.apheem.Booth 2>/dev/null | grep -q true
}
stale= n=0
while owned; do
  [ $n -lt 10 ] || { stale=1; break; }
  sleep 0.5
  n=$((n + 1))
done
state=new
[ -n "$first" ] || state=$(systemctl --user is-enabled booth.service 2>/dev/null) || true
# restart, not start: a re-install must run the new copy. Only an explicit "disabled" is the switch turned off; no
# answer (no user bus) or any other state can't be read as either, so say how to restart it by hand.
up=
case $state in
disabled) systemctl --user daemon-reload || true ;;
new|enabled)
  if systemctl --user daemon-reload && { [ "$state" = enabled ] || systemctl --user enable booth.service; } &&
     systemctl --user restart booth.service; then
    up=1
  else
    echo "install.sh: couldn't start booth.service; from your desktop session run: systemctl --user enable --now booth.service" >&2
  fi ;;
*) echo "install.sh: couldn't reach systemd; run: systemctl --user daemon-reload && systemctl --user restart booth.service" >&2 ;;
esac
echo "Booth installed to $dest/booth; run it with: $bin/booth"
if [ -n "$stale" ] && [ "$state" = disabled ]; then  # restarting a disabled unit would start it: reopen Booth instead
  echo "Booth is still running the old version — close Booth and open it again." >&2
elif [ -n "$stale" ]; then
  echo "Booth is still running the old version — close Booth, then run: systemctl --user restart booth.service (or log out and in)." >&2
elif [ -n "$closed" ] && [ -z "$up" ]; then
  echo "Booth was closed to update it — open it again from the app grid."
fi
