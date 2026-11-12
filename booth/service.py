"""The background service booth.service (`booth --background`, installed by install.sh): the "Run in background"
switch's systemctl commands, and the debounce that turns a burst of replug events into one camera restore."""
import subprocess

UNIT = "booth.service"
IS_ENABLED = ["systemctl", "--user", "is-enabled", "--quiet", UNIT]


def switch(on, in_service):
    """The command for the switch turned `on` or off. Off from inside the service only disables it: stopping it would
    kill the window the switch is in, so the app lets go of its hold and exits cleanly when that window closes."""
    if on:
        return ["systemctl", "--user", "enable", "--now", UNIT]
    return ["systemctl", "--user", "disable", UNIT] if in_service else ["systemctl", "--user", "disable", "--now", UNIT]


def run(cmd):
    """Run `cmd` → True when it exited 0."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Debounce:
    """Calls `fire` once events stop for `ms` milliseconds, so ten fast replugs end in one restore. `add(ms, callback)`
    and `remove(source)` are GLib.timeout_add and GLib.source_remove."""

    def __init__(self, ms, fire, add, remove):
        self.ms, self.fire, self.add, self.remove = ms, fire, add, remove
        self.source = None

    def poke(self):
        if self.source is not None:
            self.remove(self.source)
        self.source = self.add(self.ms, self._fire)

    def _fire(self):
        self.source = None  # spent: GLib removes it on our False, so the next poke must not remove it again
        self.fire()
        return False
