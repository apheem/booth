#!/bin/sh
# install.sh — install Booth for this user: no sudo, nothing outside the home folder, safe to re-run.
# Copies the repo's booth/ to ~/.local/share/booth/booth (a real copy, never a symlink into the repo or a worktree),
# writes the launcher ~/.local/bin/booth and the app-grid entry ~/.local/share/applications/io.github.apheem.Booth.desktop.
set -eu
repo=$(cd "$(dirname "$0")" && pwd)
share=${XDG_DATA_HOME:-$HOME/.local/share}
dest=$share/booth
bin=$HOME/.local/bin
mkdir -p "$dest" "$bin" "$share/applications"

# Stage then swap, so a failed copy never leaves a half-installed app and files removed from the repo don't linger.
rm -rf "$dest/booth.new"
cp -RL "$repo/booth" "$dest/booth.new"
find "$dest/booth.new" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$dest/booth"
mv "$dest/booth.new" "$dest/booth"

cat > "$bin/booth.new" <<EOF
#!/bin/sh
export PYTHONPATH="$dest\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m booth "\$@"
EOF
chmod 755 "$bin/booth.new"
mv "$bin/booth.new" "$bin/booth"

cat > "$share/applications/io.github.apheem.Booth.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Booth
Comment=Camera, microphone and headphones in one place
Exec=$bin/booth
Icon=camera-web
Categories=AudioVideo;Settings;
StartupNotify=true
EOF
echo "Booth installed to $dest/booth; run it with: $bin/booth"
