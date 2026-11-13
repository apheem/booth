"""install.sh runs against a throwaway HOME only, never the real home folder."""
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.apheem.Booth"


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if "__pycache__" not in p.parts)


STUB = """#!/bin/sh
echo "${0##*/} $*" >> "$STUB_LOG"
case $* in *is-enabled*) echo "${UNIT_STATE-enabled}"; [ "${UNIT_STATE-enabled}" = enabled ] || exit 1 ;; esac
case $* in *Activate\\ quit*) [ "${QUIT_RC:-0}" = 0 ] || exit "$QUIT_RC" ;; esac
case ${0##*/} in systemctl) exit "${SYSTEMCTL_RC:-0}" ;; gdbus) [ "${GDBUS_RC:-0}" = 0 ] || exit "$GDBUS_RC" ;; esac
case $* in *NameHasOwner*)
  if [ "$(grep -c NameHasOwner "$STUB_LOG")" -le "${OWNED_POLLS:-0}" ]; then echo "(true,)"; else echo "(false,)"; fi ;;
esac
"""
QUIT = (f"gdbus call --session --dest {APP_ID} --object-path /io/github/apheem/Booth"
        " --method org.gtk.Actions.Activate quit [] {}")
OWNER = ("gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus"
         f" --method org.freedesktop.DBus.NameHasOwner {APP_ID}")
UNIT_CALLS = ["systemctl --user daemon-reload", "systemctl --user enable booth.service",
              "systemctl --user restart booth.service"]
# Booth Mic: its two units enabled whatever the Run in Background switch says. With no Scarlett (amixer is stubbed
# empty) Booth Mic's chain is written and started, its selector not; the first install makes it the default mic by name.
DEFAULT_MIC = 'pw-metadata -n default 0 default.configured.audio.source { "name": "booth_mic" } Spa:String:JSON'
VOICE_UNITS = "booth-voice.service booth-mic-select.service"
VOICE_AGAIN = ["systemctl --user daemon-reload", f"systemctl --user enable {VOICE_UNITS}",
               f"systemctl --user start {VOICE_UNITS}"]  # a re-install: the same config, no new default mic
VOICE_CALLS = VOICE_AGAIN[:2] + [f"systemctl --user restart {VOICE_UNITS}", VOICE_AGAIN[2], DEFAULT_MIC]
V, A = len(VOICE_CALLS), len(VOICE_AGAIN)
STALE = "Booth is still running the old version"
CLOSED = "Booth was closed to update it — open it again from the app grid."
NO_SYSTEMD = "couldn't reach systemd; run: systemctl --user daemon-reload && systemctl --user restart booth.service"


class Install(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "my home"  # a space, which the desktop entry must survive
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        self.env["HOME"] = str(self.home)
        # Stub systemctl, gdbus and sleep first on PATH record their argv in one log: an install test never reaches the
        # real user manager or session bus. Booth's bus name reads as owned for the first OWNED_POLLS queries.
        stubs = Path(tmp.name) / "stubs"
        stubs.mkdir()
        self.calls = Path(tmp.name) / "stubs.log"
        for name in ("systemctl", "gdbus", "sleep", "pw-metadata"):
            (stubs / name).write_text(STUB)
            (stubs / name).chmod(0o755)
        for name in ("amixer", "pw-dump"):  # the Scarlett reads as unplugged: no install test reads the real one
            (stubs / name).write_text("#!/bin/sh\n")
            (stubs / name).chmod(0o755)
        # The plugin fetch's tools fail as with no network, logging to their own file: no install test downloads.
        self.fetches = Path(tmp.name) / "fetch.log"
        for name in ("curl", "apt-get", "dpkg-deb"):
            (stubs / name).write_text('#!/bin/sh\necho "${0##*/} $*" >> "$FETCH_LOG"\nexit 1\n')
            (stubs / name).chmod(0o755)
        self.env.update(PATH=f"{stubs}:{self.env['PATH']}", STUB_LOG=str(self.calls), FETCH_LOG=str(self.fetches))
        self.app = self.home / ".local/share/booth/booth"
        self.launcher = self.home / ".local/bin/booth"
        self.desktop = self.home / f".local/share/applications/{APP_ID}.desktop"
        self.unit = self.home / ".config/systemd/user/booth.service"

    def install(self, repo=REPO):
        return subprocess.run(["sh", str(repo / "install.sh")], env=self.env, cwd=self.home,
                              capture_output=True, text=True, timeout=60)

    def test_install_script_is_executable_as_the_readme_runs_it(self):
        self.assertTrue(os.access(REPO / "install.sh", os.X_OK), "README says ./install.sh")

    def test_installs_launcher_desktop_entry_and_a_copy_of_the_app(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertIn(f'Exec="{self.launcher}"', self.desktop.read_text().splitlines())
        self.assertEqual(tree(self.app), tree(REPO / "booth"))
        for name in tree(REPO / "booth"):
            if (REPO / "booth" / name).is_file():
                self.assertTrue(filecmp.cmp(REPO / "booth" / name, self.app / name, shallow=False), name)

    def test_copies_never_symlinks_and_never_points_into_the_repo(self):
        self.assertEqual(self.install().returncode, 0)
        links = [p for p in self.home.rglob("*") if p.is_symlink()]
        self.assertEqual(links, [])
        for f in (self.launcher, self.desktop):
            self.assertNotIn(str(REPO), f.read_text())

    def test_a_symlink_in_the_source_is_installed_as_a_file(self):
        src = self.home.parent / "src"
        shutil.copytree(REPO / "booth", src / "booth", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(REPO / "install.sh", src)
        (src / "extra.py").write_text("x = 1\n")
        (src / "booth" / "extra.py").symlink_to("../extra.py")
        r = self.install(src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.app / "extra.py").is_symlink())
        self.assertEqual((self.app / "extra.py").read_text(), "x = 1\n")

    def test_stays_inside_home(self):
        self.assertEqual(self.install().returncode, 0)
        code = [ln for ln in (REPO / "install.sh").read_text().splitlines() if not ln.lstrip().startswith("#")]
        self.assertFalse([ln for ln in code if "sudo" in ln])
        written = {p.relative_to(self.home).parts[:2] for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(written, {(".local", "bin"), (".local", "share"), (".config", "systemd"), (".config", "booth")})

    def test_installs_enables_and_starts_the_service(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        # restart, not start: a re-install must run the new copy (start leaves a running old one as it is).
        self.assertEqual(self.calls.read_text().splitlines(), VOICE_CALLS + [QUIT, OWNER] + UNIT_CALLS)
        self.assertNotIn("couldn't start", r.stderr)
        self.assertNotIn(STALE, r.stdout + r.stderr)
        unit = self.unit.read_text().splitlines()
        self.assertIn(f'ExecStart=/bin/sh "{self.launcher}" --background', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5", unit)  # a crash loop stays under systemd's start limit
        self.assertIn("PartOf=graphical-session.target", unit)  # it stops with the session whose display it uses
        self.assertIn("WantedBy=graphical-session.target", unit)  # GTK needs the session's display
        self.assertIn("After=graphical-session.target", unit)
        self.assertNotIn(str(REPO), self.unit.read_text())

    def test_installs_booth_voice_which_runs_booth_mics_config_with_pipewire(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        voice_unit = self.unit.parent / "booth-voice.service"
        unit = voice_unit.read_text().splitlines()
        conf = self.home / ".config/booth/voice.conf"  # beside settings.json, where Booth writes it
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{conf}"', unit)
        self.assertIn(f'ExecCondition=/usr/bin/test -f "{conf}"', unit)  # no config yet: skipped, never a failure
        self.assertIn("BindsTo=pipewire.service", unit)
        self.assertIn("WantedBy=pipewire.service", unit)  # Booth Mic lives with PipeWire, not with Booth's window
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("graphical-session", voice_unit.read_text())
        self.assertIn("Booth Mic is waiting for the Scarlett", r.stdout)  # what booth.voice said
        self.assertEqual(sorted(os.listdir(self.unit.parent)),
                         ["booth-mic-select.service", "booth-voice.service", "booth.service"])

    def pre_start(self):
        """booth-voice.service's ExecStartPre as argv: systemd's %% and $$ undone, then its quoted words split."""
        line = next(ln for ln in (self.unit.parent / "booth-voice.service").read_text().splitlines()
                    if ln.startswith("ExecStartPre="))
        return shlex.split(line.removeprefix("ExecStartPre=").replace("%%", "%").replace("$$", "$"))

    def test_booth_voice_rewrites_its_config_from_freshly_verified_plugins_before_each_start(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.pre_start()
        self.assertEqual(argv, ["/usr/bin/env", f"PYTHONPATH={self.app.parent}", "python3", "-P", "-m", "booth.voice",
                                "--write"])  # the installed copy, never the repo
        # voice.conf written while sc4m verified; the file changed since: the next start must never load it
        plugin = self.app.parent / "plugins/sc4m_1916.so"
        plugin.parent.mkdir(exist_ok=True)
        plugin.write_bytes(b"changed after install")
        conf = self.home / ".config/booth/voice.conf"
        cfg = json.loads(conf.read_text())
        chain = next(m for m in cfg["context.modules"] if m["name"] == "libpipewire-module-filter-chain")["args"]
        chain["filter.graph"]["nodes"].append({"type": "ladspa", "name": "leveling", "plugin": str(plugin),
                                               "label": "sc4m"})
        conf.write_text(json.dumps(cfg))
        before = self.calls.read_text()
        r = subprocess.run(argv, env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ladspa", conf.read_text())
        self.assertNotIn(str(plugin), conf.read_text())
        self.assertEqual(self.calls.read_text(), before)  # no systemctl from inside the unit's own start

    def test_booth_voice_doesnt_start_when_its_pre_start_cant_write_the_config(self):
        self.assertEqual(self.install().returncode, 0)
        unit = (self.unit.parent / "booth-voice.service").read_text().splitlines()
        self.assertLess(unit.index(next(ln for ln in unit if ln.startswith("ExecStartPre="))),
                        unit.index(next(ln for ln in unit if ln.startswith("ExecStart="))))
        self.assertNotIn("ExecStartPre=-", "\n".join(unit))  # a failure stops the start
        conf = self.home / ".config/booth/voice.conf"
        conf.unlink()
        conf.mkdir()  # a folder where the file goes: the write fails
        r = subprocess.run(self.pre_start(), env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not saved", r.stderr)

    def test_installs_booth_mic_select_which_runs_the_selector_bound_to_booth_voice(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        unit = (self.unit.parent / "booth-mic-select.service").read_text().splitlines()
        conf = self.home / ".config/booth/voice-select.conf"
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{conf}"', unit)
        self.assertIn(f'ExecCondition=/usr/bin/test -f "{conf}"', unit)  # no mic channel seen yet: skipped
        self.assertIn("BindsTo=booth-voice.service", unit)  # it feeds Booth Mic's input: never runs without it
        self.assertIn("After=booth-voice.service", unit)
        self.assertIn("WantedBy=booth-voice.service", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_booth_mic_is_set_up_by_the_installed_copy_never_the_repo(self):
        # the repo may be a worktree on another branch, or deleted later: the units run what was installed
        python = shutil.which("python3", path=self.env["PATH"])
        log = self.calls.parent / "python.log"
        (self.calls.parent / "stubs" / "python3").write_text(
            f'#!/bin/sh\necho "$PYTHONPATH $*" >> "{log}"\nexec "{python}" "$@"\n')
        (self.calls.parent / "stubs" / "python3").chmod(0o755)
        self.assertEqual(self.install().returncode, 0)
        self.assertIn(f"{self.app.parent} -P -m booth.voice", log.read_text().splitlines())

    def test_only_the_first_install_makes_booth_mic_the_default_mic(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertIn(DEFAULT_MIC, self.calls.read_text().splitlines())
        self.calls.write_text("")  # the user may have chosen another mic since: a re-install never overrides it
        self.assertEqual(self.install().returncode, 0)
        self.assertNotIn("pw-metadata", self.calls.read_text())
        self.assertEqual(self.calls.read_text().splitlines()[:A], VOICE_AGAIN)

    def test_booth_mic_is_set_up_even_with_the_background_service_switched_off(self):
        self.assertEqual(self.install().returncode, 0)
        self.env["UNIT_STATE"] = "disabled"
        self.calls.write_text("")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines()[:A], VOICE_AGAIN)

    def test_without_a_user_manager_it_says_how_to_start_booth_mic(self):
        self.env["SYSTEMCTL_RC"] = "1"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"systemctl --user enable --now {VOICE_UNITS}", r.stderr)
        self.assertIn(DEFAULT_MIC, self.calls.read_text())  # by name: it still becomes the default when it appears

    def test_without_pipewire_it_says_how_to_choose_booth_mic(self):
        (self.calls.parent / "stubs" / "pw-metadata").write_text("#!/bin/sh\nexit 1\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("couldn't make Booth Mic the default mic", r.stderr)

    def test_the_voice_plugins_are_in_place_before_booth_is_quit_and_restarted(self):
        lines = [ln for ln in (REPO / "install.sh").read_text().splitlines() if not ln.lstrip().startswith("#")]
        first = lambda s: next(i for i, ln in enumerate(lines) if s in ln)
        self.assertLess(first("-m booth.plugins"), first("org.gtk.Actions.Activate quit"))
        self.assertLess(first("-m booth.plugins"), first("systemctl --user restart booth.service"))

    def test_a_running_booth_is_asked_to_quit_and_waited_for_before_the_restart(self):
        # Else the restarted unit's --background finds it, exits 0 and the old code keeps running.
        self.env["OWNED_POLLS"] = "3"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls.read_text().splitlines(),
                         VOICE_CALLS + [QUIT] + [OWNER, "sleep 0.5"] * 3 + [OWNER] + UNIT_CALLS)
        self.assertNotIn(STALE, r.stdout + r.stderr)

    def test_a_booth_that_wont_quit_is_waited_for_at_most_5_s_then_named(self):
        self.env["OWNED_POLLS"] = "99"  # an old copy with no app.quit
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls.count("sleep 0.5"), 10)
        self.assertEqual(len(calls), V + 1 + 11 + 10 + 3)
        self.assertEqual(calls[-3:], UNIT_CALLS)
        # nothing starts the new copy until it's gone: say how to start it then
        self.assertIn(STALE + " — close Booth, then run: systemctl --user restart booth.service (or log out and in).",
                      r.stderr)

    def test_a_reinstall_keeps_the_service_switched_off(self):
        self.assertEqual(self.install().returncode, 0)  # the first install enables it
        self.env["UNIT_STATE"] = "disabled"  # then "Run in background" was switched off
        self.calls.write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[A + 2:], ["systemctl --user is-enabled booth.service", "systemctl --user daemon-reload"])
        self.assertNotIn("couldn't start", r.stderr)

    def test_fsk_reinstall_with_no_user_bus_says_nothing(self):
        self.assertEqual(self.install().returncode, 0)
        self.env.update(UNIT_STATE="", SYSTEMCTL_RC="1")  # is-enabled can't reach the user manager: no answer
        self.calls.write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(NO_SYSTEMD, r.stderr)  # an enabled service would keep running the old copy: say how to restart
        self.assertNotIn("systemctl --user restart booth.service", self.calls.read_text())
        self.assertIn(CLOSED, r.stdout)

    def test_fsk_reinstall_switched_off_closes_the_open_window_silently(self):
        self.assertEqual(self.install().returncode, 0)
        self.env["UNIT_STATE"] = "disabled"
        r = self.install()  # the quit closed the open Booth window and nothing restarts it
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(CLOSED, r.stdout)
        self.env["QUIT_RC"] = "1"  # no Booth was running: nothing was closed
        r = self.install()
        self.assertNotIn(CLOSED, r.stdout + r.stderr)
        self.env.update(QUIT_RC="0", OWNED_POLLS="99")  # an old copy that won't quit, with the switch off
        self.calls.write_text("")
        r = self.install()
        self.assertIn(STALE + " — close Booth and open it again.", r.stderr)
        self.assertNotIn("restart booth.service", r.stdout + r.stderr)  # that would start a disabled unit
        self.assertNotIn(CLOSED, r.stdout)  # it wasn't closed

    def test_a_restarted_service_says_nothing_about_closing(self):
        self.assertEqual(self.install().returncode, 0)
        r = self.install()  # enabled: the restart brings Booth back
        self.assertNotIn(CLOSED, r.stdout + r.stderr)

    def test_a_reinstall_restarts_an_enabled_service_without_enabling_it_again(self):
        self.assertEqual(self.install().returncode, 0)
        self.calls.write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls.read_text().splitlines()[A + 2:],
                         ["systemctl --user is-enabled booth.service", "systemctl --user daemon-reload",
                          "systemctl --user restart booth.service"])

    def test_with_no_session_bus_the_restart_still_runs(self):
        self.env["GDBUS_RC"] = "1"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), VOICE_CALLS + [QUIT, OWNER] + UNIT_CALLS)
        self.assertNotIn(STALE, r.stdout + r.stderr)

    def test_the_unit_follows_xdg_config_home_unless_relative(self):
        self.env["XDG_CONFIG_HOME"] = str(self.home / "cfg")
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue((self.home / "cfg/systemd/user/booth.service").is_file())
        self.env["XDG_CONFIG_HOME"] = "rel/cfg"
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue(self.unit.is_file())
        self.assertFalse((self.home / "rel").exists())

    def test_without_a_user_manager_the_app_still_installs_and_says_how_to_start_it(self):
        self.env["SYSTEMCTL_RC"] = "1"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemctl --user enable --now booth.service", r.stderr)
        self.assertTrue(self.unit.is_file())
        self.assertEqual(tree(self.app), tree(REPO / "booth"))

    def test_rerun_is_safe_and_replaces_stale_files(self):
        self.assertEqual(self.install().returncode, 0)
        (self.app / "stale.py").write_text("x = 1\n")
        (self.app.parent / "booth.new").mkdir()  # left by an install killed mid-copy
        (self.app.parent / "booth.new" / "half.py").write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree(self.app), tree(REPO / "booth"))
        self.assertEqual(list(self.app.parent.iterdir()), [self.app])
        self.assertEqual(sorted(os.listdir(self.unit.parent)),
                         ["booth-mic-select.service", "booth-voice.service", "booth.service"])  # no .new left

    def test_relative_xdg_data_home_is_ignored(self):
        self.env["XDG_DATA_HOME"] = "rel/share"  # invalid per the XDG spec: must not install relative to the cwd
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(tree(self.app), tree(REPO / "booth"))
        self.assertFalse((self.home / "rel").exists())
        self.assertIn(f"PYTHONPATH='{self.home}/.local/share/booth", self.launcher.read_text())

    def test_fetches_the_plugins_into_the_data_folder_and_a_failure_does_not_stop_the_install(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("curl -fsSL --proto =https -o", self.fetches.read_text())
        self.assertIn("apt-get download swh-plugins", self.fetches.read_text())
        self.assertIn("booth.plugins: RNNoise: download failed", r.stderr)
        self.assertIn("booth.plugins: SWH plugins 0.4.17-3build1: download failed", r.stderr)
        self.assertIn("install.sh: some voice plugins couldn't be fetched or verified (above)", r.stderr)
        self.assertIn("librnnoise_ladspa.so: refused (missing)", r.stdout)
        self.assertFalse((self.app.parent / "plugins").exists())  # nothing unverified was placed
        self.assertEqual(self.calls.read_text().splitlines(), VOICE_CALLS + [QUIT, OWNER] + UNIT_CALLS)  # the service still restarts
        self.assertEqual(tree(self.app), tree(REPO / "booth"))

    def test_the_plugins_follow_xdg_data_home_and_survive_a_reinstall(self):
        self.env["XDG_DATA_HOME"] = str(self.home / "data")
        plugins = self.home / "data/booth/plugins"
        plugins.mkdir(parents=True)
        plugins.chmod(0o755)  # whatever the umask: this test is about the sha256
        (plugins / "sc4m_1916.so").write_bytes(b"downloaded once")
        (plugins / "sc4m_1916.so").chmod(0o644)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"-o {self.home}/data/booth/.plugins-fetch-", self.fetches.read_text())
        self.assertEqual((plugins / "sc4m_1916.so").read_bytes(), b"downloaded once")  # a failed fetch removes nothing
        self.assertIn("sc4m_1916.so: refused (sha256 mismatch)", r.stdout)

    def test_the_plugin_fetch_never_imports_a_booth_folder_from_the_cwd(self):
        decoy = self.home / "booth"  # install() runs from the home folder
        decoy.mkdir()
        (decoy / "__init__.py").write_text("")
        (decoy / "plugins.py").write_text(f"open({str(self.home / 'decoy-ran')!r}, 'w')\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.home / "decoy-ran").exists())
        self.assertIn("librnnoise_ladspa.so: refused (missing)", r.stdout)  # the installed module ran instead

    def test_launcher_runs_the_installed_copy(self):
        self.assertEqual(self.install().returncode, 0)
        env = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH")}
        # --help is answered before any window or display is touched.
        r = subprocess.run([str(self.launcher), "--help"], env=env, cwd=self.home, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage", r.stdout)
        self.assertIn("--background", r.stdout)  # the option the unit starts it with

    def test_a_home_with_shell_and_desktop_entry_specials(self):
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        self.home = self.home.rename(self.home.parent / "my \"home\" $HOME `x` back\\slash 100%d 'q'")
        self.env["HOME"] = str(self.home)
        launcher = self.home / ".local/bin/booth"
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        kf = GLib.KeyFile()
        kf.load_from_file(str(self.home / f".local/share/applications/{APP_ID}.desktop"), GLib.KeyFileFlags.NONE)
        exec_ = kf.get_string("Desktop Entry", "Exec")
        # The spec: in a quoted argument " ` $ \ each take a backslash; %% is a literal %, any other % a field code.
        self.assertRegex(exec_, r'^"(?:[^"`$\\]|\\["`$\\])*"$')
        exec_ = re.sub("%(.)", lambda m: "%" if m[1] == "%" else "<field code>", exec_)
        self.assertEqual(GLib.shell_parse_argv(exec_)[1], [str(launcher)])
        # Unit-file quoting: in a quoted word \ and " take a backslash; %% is a literal %, $$ a literal $.
        unit = self.home / ".config/systemd/user/booth.service"
        start = next(ln for ln in unit.read_text().splitlines() if ln.startswith("ExecStart="))
        m = re.fullmatch(r'ExecStart=/bin/sh "((?:[^"\\%$]|\\["\\]|%%|\$\$)*)" --background', start)
        self.assertIsNotNone(m, start)
        self.assertEqual(re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], m[1]), str(launcher))
        voice_unit, select_unit = unit.parent / "booth-voice.service", unit.parent / "booth-mic-select.service"
        for u, conf in ((voice_unit, "voice.conf"), (select_unit, "voice-select.conf")):
            for key in ("ExecStart=/usr/bin/pipewire -c", "ExecCondition=/usr/bin/test -f"):
                line = next(ln for ln in u.read_text().splitlines() if ln.startswith(key))
                m = re.fullmatch(re.escape(key) + r' "((?:[^"\\%$]|\\["\\]|%%|\$\$)*)"', line)
                self.assertIsNotNone(m, line)
                self.assertEqual(re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], m[1]),
                                 str(self.home / ".config/booth" / conf))
        line = next(ln for ln in voice_unit.read_text().splitlines() if ln.startswith("ExecStartPre="))
        m = re.fullmatch(r'ExecStartPre=/usr/bin/env "PYTHONPATH=((?:[^"\\%$]|\\["\\]|%%|\$\$)*)" python3 -P -m '
                         r'booth\.voice --write', line)
        self.assertIsNotNone(m, line)
        self.assertEqual(re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], m[1]),
                         str(self.home / ".local/share/booth"))
        self.unit = unit
        pre = subprocess.run(self.pre_start(), env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(pre.returncode, 0, pre.stderr)  # the installed copy found, whatever the home's name
        if shutil.which("systemd-analyze"):  # read-only: parses the unit, starts nothing
            for u in (unit, voice_unit, select_unit):
                v = subprocess.run(["systemd-analyze", "--user", "verify", str(u)], capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(v.returncode, 0, v.stderr)
        env = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH")}
        r = subprocess.run([str(launcher), "--help"], env=env, cwd=self.home, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage", r.stdout)

    def test_launcher_ignores_a_booth_package_in_the_cwd(self):
        # From a repo or worktree root, `python3 -m booth` would import that checkout's booth/ before the install.
        self.assertEqual(self.install().returncode, 0)
        decoy = self.home / "checkout"
        (decoy / "booth").mkdir(parents=True)
        (decoy / "booth" / "__init__.py").write_text("")
        (decoy / "booth" / "__main__.py").write_text("import sys\nprint('DECOY')\nsys.exit(7)\n")
        env = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH")}
        r = subprocess.run([str(self.launcher), "--help"], env=env, cwd=decoy, capture_output=True, text=True,
                           timeout=60)
        self.assertNotIn("DECOY", r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Usage", r.stdout)


if __name__ == "__main__":
    unittest.main()
