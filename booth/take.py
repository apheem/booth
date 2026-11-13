"""A test recording for the Microphone page: SECONDS of Booth Mic as a call hears it (processed; silence while muted),
then played back once on the default output, where sound plays now, and deleted. pw-record and pw-play do the work.
`timeout` ends the recording, so a recorder never outlives its take, with Booth or without. The file lives in
XDG_RUNTIME_DIR (memory, this user's only), never in the home folder, and is deleted at every ending."""
import contextlib
import os
import subprocess
import threading

from gi.repository import GLib

from booth import voice

SECONDS = 10
HINT = f"{SECONDS} seconds of Booth Mic as a call hears it, then played back on your sound output and deleted"
RECORDING = "Recording — speak now: {} s left"
PLAYING = "Playing it back…"
FAILED = "Couldn't record Booth Mic (needs pw-record and pw-play, package pipewire-bin)"
FULL = 124  # timeout's exit code when it ended the command: the whole take was recorded


def path():
    return os.path.join(GLib.get_user_runtime_dir(), "booth-test-recording.wav")


def record_cmd(path):
    """Booth Mic by name, mono as it is; dont-reconnect: a missing Booth Mic never falls back to another mic."""
    return ["timeout", str(SECONDS), "pw-record", "--target", voice.NODE, "--channels", "1",
            "-P", "{ node.dont-reconnect = true }", path]


def play_cmd(path):
    return ["pw-play", path]


class Take:
    """toggle() is the button (Record, then Stop); stop() when the window closes or Booth quits. say(text or None,
    busy) is called on the main loop after each change. `proc`: the recorder or the player, None when idle."""

    def __init__(self, say, popen=subprocess.Popen):
        self.say, self.popen, self.proc, self.left = say, popen, None, 0

    def toggle(self):
        if self.proc:
            self.stop()
        elif self._run(record_cmd(path()), self._recorded):
            self.left = SECONDS
            GLib.timeout_add_seconds(1, self._tick, self.proc)
            self.say(RECORDING.format(self.left), True)

    def stop(self):
        if self.proc:
            self.proc.terminate()  # timeout passes it on to pw-record
            self._end()

    def _run(self, cmd, then):
        try:
            proc = self.proc = self.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError:
            self._end(FAILED)
            return False
        threading.Thread(target=lambda: (proc.wait(), GLib.idle_add(self._exited, proc, then)), daemon=True).start()
        return True

    def _exited(self, proc, then):
        if proc is self.proc:  # else stop() ended it: nothing follows
            then()
        return GLib.SOURCE_REMOVE

    def _tick(self, proc):
        """The countdown, only shown: timeout ends the recording."""
        self.left -= 1
        if proc is not self.proc or self.left <= 0:
            return GLib.SOURCE_REMOVE
        self.say(RECORDING.format(self.left), True)
        return GLib.SOURCE_CONTINUE

    def _recorded(self):
        if self.proc.returncode != FULL:  # it ended by itself: Booth Mic isn't there, or pw-record isn't
            self._end(FAILED)
        elif self._run(play_cmd(path()), self._end):
            self.say(PLAYING, True)

    def _end(self, text=None):
        self.proc = None
        with contextlib.suppress(OSError):
            os.unlink(path())
        self.say(text, False)
