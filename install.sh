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
# A running Booth owns its bus name, so the restarted unit's --background would find it and exit 0 with the old code
# still running: ask it to quit (app.quit; "not running" errors ignored) and wait up to 5 s for the name to go.
gdbus call --session --dest io.github.apheem.Booth --object-path /io/github/apheem/Booth \
  --method org.gtk.Actions.Activate quit '[]' '{}' >/dev/null 2>&1 || true
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
# restart, not start: a re-install must run the new copy.
if ! { systemctl --user daemon-reload && systemctl --user enable booth.service && systemctl --user restart booth.service; }
then
  echo "install.sh: couldn't start booth.service; from your desktop session run: systemctl --user enable --now booth.service" >&2
fi
echo "Booth installed to $dest/booth; run it with: $bin/booth"
[ -z "$stale" ] || echo "Booth is still running the old version — close it (or log out and in) to finish." >&2
