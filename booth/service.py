"""The background service booth.service (`booth --background`, installed by install.sh): the "Run in background"
switch's systemctl commands, and the debounce that turns a burst of replug events into one camera restore."""
import subprocess

APP_ID = "io.github.apheem.Booth"  # the app's D-Bus name: owned while a Booth primary instance runs
UNIT = "booth.service"
IS_ENABLED = ["systemctl", "--user", "is-enabled", "--quiet", UNIT]


def switch(on):
    """The command for the switch turned `on` or off. It never starts or stops the unit: the window's own process is the
    primary instance, so it holds on (and the unit takes over at the next login) or lets go and exits with its window."""
    return ["systemctl", "--user", "enable" if on else "disable", UNIT]


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
