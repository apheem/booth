"""install.sh runs against a throwaway HOME only, never the real home folder."""
import filecmp
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from booth import voice

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
VOICE_CALLS = VOICE_AGAIN[:2] + [f"systemctl --user try-restart {VOICE_UNITS}", VOICE_AGAIN[2], DEFAULT_MIC]
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
        # gsettings (#21): mic-mute reads as MIC_MUTE (GNOME 46's default [''] when unset), its calls in their own log
        self.gsettings = Path(tmp.name) / "gsettings.log"
        (stubs / "gsettings").write_text('#!/bin/sh\necho "gsettings $*" >> "$GSETTINGS_LOG"\n'
                                         '[ "$1" != get ] || printf "%s\\n" "${MIC_MUTE-[\'\']}"\n'
                                         'exit "${GSETTINGS_RC:-0}"\n')
        (stubs / "gsettings").chmod(0o755)
        self.env.update(PATH=f"{stubs}:{self.env['PATH']}", STUB_LOG=str(self.calls), FETCH_LOG=str(self.fetches),
                        GSETTINGS_LOG=str(self.gsettings))
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

    def test_installs_the_apps_own_icon_which_the_desktop_entry_uses(self):  # #22
        icon = self.home / f".local/share/icons/hicolor/scalable/apps/{APP_ID}.svg"
        for _ in range(2):  # re-runnable: replaced in place, no .new left
            r = self.install()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(filecmp.cmp(REPO / "data" / f"{APP_ID}.svg", icon, shallow=False))
            self.assertEqual(os.listdir(icon.parent), [icon.name])
        self.assertIn(f"Icon={APP_ID}", self.desktop.read_text().splitlines())

    def test_the_icon_is_a_plain_drawing_with_nothing_embedded_or_fetched(self):  # original art: no image, no link
        root = ElementTree.parse(REPO / "data" / f"{APP_ID}.svg").getroot()
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertEqual((root.get("width"), root.get("height")), ("128", "128"))  # GNOME's app icon grid
        tags = {e.tag.rpartition("}")[2] for e in root.iter()}
        self.assertFalse(tags & {"image", "script", "foreignObject", "use"}, tags)
        self.assertFalse([a for e in root.iter() for a in e.attrib if a.endswith("href")])

    def test_copies_never_symlinks_and_never_points_into_the_repo(self):
        self.assertEqual(self.install().returncode, 0)
        links = [p for p in self.home.rglob("*") if p.is_symlink()]
        self.assertEqual(links, [])
        for f in (self.launcher, self.desktop):
            self.assertNotIn(str(REPO), f.read_text())

    def test_a_symlink_in_the_source_is_installed_as_a_file(self):
        src = self.home.parent / "src"
        shutil.copytree(REPO / "booth", src / "booth", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(REPO / "data", src / "data")  # the icon
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
        self.assertEqual(self.calls.read_text().splitlines(), [QUIT, OWNER] + VOICE_CALLS + UNIT_CALLS)
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
        run = conf.with_name("voice-run.conf")  # written only by its pre-start
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{run}"', unit)
        self.assertIn(f'ExecCondition=/usr/bin/test -f "{conf}"', unit)  # no config yet: skipped, never a failure
        self.assertIn("BindsTo=pipewire.service", unit)
        self.assertIn("WantedBy=pipewire.service", unit)  # Booth Mic lives with PipeWire, not with Booth's window
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("graphical-session", voice_unit.read_text())
        self.assertIn("Booth Mic is waiting for the Scarlett", r.stdout)  # what booth.voice said
        self.assertEqual(sorted(os.listdir(self.unit.parent)),
                         ["booth-mic-select.service", "booth-monitor.service", "booth-stereo.service", "booth-voice.service",
                          "booth.service"])

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
                                "--write", str(self.home / ".config/booth/voice-run.conf"),
                                str(self.app.parent / "plugins")])  # the installed copy, never the repo
        # voice-run.conf written while sc4m verified; the file changed since: the next start must never load it
        plugin = self.app.parent / "plugins/sc4m_1916.so"
        plugin.parent.mkdir(exist_ok=True)
        plugin.write_bytes(b"changed after install")
        conf = self.home / ".config/booth/voice-run.conf"
        cfg = json.loads(conf.with_name("voice.conf").read_text())
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

    def test_booth_voice_loads_only_the_config_its_pre_start_wrote_never_the_one_booth_writes(self):
        """Booth's poll writes voice.conf at any time, a moment after a pre-start's check too: pipewire must load a
        file only the pre-start writes, from the plugins it verified just then."""
        self.assertEqual(self.install().returncode, 0)
        unit = (self.unit.parent / "booth-voice.service").read_text().splitlines()
        start = shlex.split(next(ln for ln in unit if ln.startswith("ExecStart=")).removeprefix("ExecStart="))
        self.assertEqual(start[:2], ["/usr/bin/pipewire", "-c"])
        self.assertEqual(start[2], self.pre_start()[7])  # the file --write writes
        self.assertNotEqual(start[2], str(voice.path().with_name("voice.conf")))
        self.assertNotEqual(Path(start[2]).name, "voice.conf")  # what apply() writes

    def test_skeptic_under_the_default_umask_the_pins_and_the_pre_start_code_are_group_writable(self):
        old = os.umask(0o002)  # Ubuntu's default for a user with a private group (this desk: umask 0002)
        try:
            r = self.install()
        finally:
            os.umask(old)
        self.assertEqual(r.returncode, 0, r.stderr)
        bad = {str(p.relative_to(self.home)): oct(stat.S_IMODE(p.stat().st_mode))
               for p in (self.app.parent, self.app, self.app / "plugins.py", self.app / "voice.py",
                         self.unit.parent / "booth-voice.service", self.home / ".config/booth")
               if p.stat().st_mode & 0o022}
        self.assertEqual(bad, {}, "verify() refuses a group-writable plugin, but the pins and --write's code are")

    def test_a_reinstall_takes_group_write_off_the_folders_an_older_install_left_group_writable(self):
        for d in (self.app.parent, self.home / ".config/booth"):
            d.mkdir(parents=True)
            d.chmod(0o777)  # writable by anyone: a umask 0 install, or a chmod by hand
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual([oct(d.stat().st_mode & 0o777) for d in (self.app.parent, self.home / ".config/booth")],
                         ["0o755", "0o755"])

    def test_booth_voice_doesnt_start_when_its_pre_start_cant_write_the_config(self):
        self.assertEqual(self.install().returncode, 0)
        unit = (self.unit.parent / "booth-voice.service").read_text().splitlines()
        self.assertLess(unit.index(next(ln for ln in unit if ln.startswith("ExecStartPre="))),
                        unit.index(next(ln for ln in unit if ln.startswith("ExecStart="))))
        self.assertNotIn("ExecStartPre=-", "\n".join(unit))  # a failure stops the start
        conf = self.home / ".config/booth/voice-run.conf"
        conf.mkdir()  # a folder where the file goes: the write fails
        r = subprocess.run(self.pre_start(), env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not saved", r.stderr)

    def test_skeptic_booth_voice_mutes_a_new_booth_mic_when_the_saved_record_is_muted_with_no_booth(self):  # #21
        """A kept voice-run.conf may carry live gains: the unit's ExecStartPost, the installed booth.mute, mutes the
        new booth_mic's node by name when the saved record is muted; never unmutes, never fails the start."""
        self.assertEqual(self.install().returncode, 0)
        unit = (self.unit.parent / "booth-voice.service").read_text().splitlines()
        line = next(ln for ln in unit if ln.startswith("ExecStartPost="))
        self.assertLess(unit.index(next(ln for ln in unit if ln.startswith("ExecStart="))), unit.index(line))
        argv = shlex.split(line.removeprefix("ExecStartPost=").replace("%%", "%").replace("$$", "$"))
        self.assertEqual(argv, ["-/usr/bin/env", f"PYTHONPATH={self.app.parent}", "python3", "-P", "-m", "booth.mute",
                                "--enforce"])  # "-": a booth_mic left unmuted beats none (the raw mic, unmuted)
        stubs = Path(self.env["PATH"].split(":")[0])
        (stubs / "pw-dump").write_text("#!/bin/sh\necho '" + json.dumps([{"id": 106, "info": {
            "props": {"node.name": "booth_mic", "object.serial": 87}, "params": {"Props": [{"mute": False}]}}}]) + "'\n")
        (stubs / "wpctl").write_text(STUB)
        (stubs / "wpctl").chmod(0o755)
        settings = self.home / ".config/booth/settings.json"
        for record, want in (({"muted": True}, ["wpctl set-mute 106 1"]), ({"muted": False}, [])):
            settings.write_text(json.dumps({"voice": record}))
            self.calls.write_text("")
            r = subprocess.run([argv[0].removeprefix("-")] + argv[1:], env=self.env, cwd=self.home,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([c for c in self.calls.read_text().splitlines() if c.startswith("wpctl")], want, record)

    def test_the_pre_start_rewrites_the_config_pipewire_loads_whatever_the_units_environment(self):
        self.assertEqual(self.install().returncode, 0)
        conf = self.home / ".config/booth/voice-run.conf"
        conf.write_text("{}")
        other = self.home / "other"  # the user manager's XDG_* may differ from the install's
        env = dict(self.env, XDG_CONFIG_HOME=str(other / "config"), XDG_DATA_HOME=str(other / "data"))
        r = subprocess.run(self.pre_start(), env=env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("libpipewire-module-filter-chain", conf.read_text())  # the file ExecStart's pipewire loads
        self.assertFalse(other.exists())

    def chain_with_a_plugin(self):
        """voice.conf as a running Booth Mic holds it: with sc4m, which doesn't verify now (no install test fetches)."""
        conf = self.home / ".config/booth/voice.conf"
        conf.write_text(voice.build({"voice": {}}, {"sc4m_1916.so": str(self.app.parent / "plugins/sc4m_1916.so")}))
        return conf

    def test_a_reinstall_restarts_booth_mic_for_its_new_plugins_once_the_old_booth_is_gone(self):
        self.assertEqual(self.install().returncode, 0)
        self.chain_with_a_plugin()
        self.env["OWNED_POLLS"] = "3"
        self.calls.write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls.read_text().splitlines()
        reload = f"systemctl --user try-restart {VOICE_UNITS}"  # only if it runs: its pre-start writes the new chain
        self.assertEqual(calls.count(reload), 1, calls)
        self.assertNotIn("Booth didn't quit", r.stderr)
        self.assertGreater(calls.index(reload), len(calls) - 1 - calls[::-1].index(OWNER))  # after the old Booth left
        self.assertLess(calls.index(reload), calls.index("systemctl --user restart booth.service"))

    def test_a_booth_that_wont_quit_never_has_booth_mic_restarted_under_it(self):
        """The old Booth's poll would write its chain back and restart Booth Mic, whose pre-start writes the new one:
        Booth Mic restarting every few seconds until Booth is closed. Left running: its next start loads the new one."""
        self.assertEqual(self.install().returncode, 0)
        conf = self.chain_with_a_plugin()
        self.env["OWNED_POLLS"] = "99"
        self.calls.write_text("")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("restart booth-voice", self.calls.read_text())
        self.assertEqual([n["name"] for n in json.loads(conf.read_text())["context.modules"][-1]["args"]["filter.graph"]
                          ["nodes"] if n["type"] == "ladspa"], ["leveling"])  # the running chain's shape, kept
        self.assertIn(STALE + " — close Booth", r.stderr)
        # never skipped silently: the new plugins wait for Booth Mic's next start, and the user is told how
        self.assertIn("Booth didn't quit, so Booth Mic wasn't restarted for its new plugins; once Booth is closed, run: "
                      "systemctl --user restart booth-voice.service", r.stderr)

    def test_installs_booth_mic_select_which_runs_the_selector_bound_to_booth_voice(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        unit = (self.unit.parent / "booth-mic-select.service").read_text().splitlines()
        conf = self.home / ".config/booth/voice-select.conf"
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{conf}"', unit)
        self.assertNotIn("ExecCondition=-", "\n".join(unit))
        self.assertIn("BindsTo=booth-voice.service", unit)  # it feeds Booth Mic's input: never runs without it
        self.assertIn("After=booth-voice.service", unit)
        self.assertIn("WantedBy=booth-voice.service", unit)
        self.assertIn("Restart=on-failure", unit)

    def condition(self):
        """booth-mic-select.service's ExecCondition as argv: systemd's %% and $$ undone, then its quoted words split."""
        line = next(ln for ln in (self.unit.parent / "booth-mic-select.service").read_text().splitlines()
                    if ln.startswith("ExecCondition="))
        return shlex.split(line.removeprefix("ExecCondition=").replace("%%", "%").replace("$$", "$"))

    def test_booth_mic_select_never_starts_a_backup_mic_without_booth_running(self):
        """#20: a backup (the BRIO's room mic, the PRO X's) feeds a call only while Booth runs to say so. Left by a Booth
        that exited, the installed code refuses it at start and removes it: Booth Mic is silent until Booth says why."""
        self.assertEqual(self.install().returncode, 0)
        conf = self.home / ".config/booth/voice-select.conf"
        argv = self.condition()
        self.assertEqual(argv, ["/usr/bin/env", f"PYTHONPATH={self.app.parent}", "python3", "-P", "-m", "booth.voice",
                                "--select", str(conf)])  # the installed copy, never the repo
        brio = {"node": "alsa_input.usb-046d_Logitech_BRIO_FAKESER1-03.analog-stereo", "position": "FL",
                "channel": None}
        scarlett_mic = {"node": "alsa_input.usb-Focusrite_Scarlett_Solo_4th_Gen_FAKESER1-00.pro-input-0",
                        "position": "FR", "channel": 2}

        def start(mic, owned):
            if mic:
                conf.write_text(voice.build_selector(mic))
            env = dict(self.env, OWNED_POLLS="99" if owned else "0")  # Booth's bus name owned, or not
            return subprocess.run(argv, env=env, cwd=self.home, capture_output=True, text=True, timeout=60).returncode
        self.assertEqual((start(brio, owned=True), conf.exists()), (0, True))  # Booth runs: it says it
        self.assertEqual((start(brio, owned=False), conf.exists()), (1, False))  # skipped, and gone for good
        self.assertEqual((start(scarlett_mic, owned=False), conf.exists()), (0, True))  # login, no Booth yet
        conf.unlink()
        self.assertEqual(start(None, owned=True), 1)  # no config yet: skipped, never a failure

    def test_installs_booth_monitor_which_only_booth_starts_and_which_never_pulls_booth_mic_up(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.unit.parent / "booth-monitor.service").read_text()
        unit, conf = text.splitlines(), self.home / ".config/booth/monitor.conf"
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{conf}"', unit)
        self.assertIn(f'ExecCondition=/usr/bin/test -f "{conf}"', unit)  # switched off: its config is gone, never starts
        self.assertIn("BindsTo=pipewire.service", unit)
        self.assertIn("After=pipewire.service", unit)
        self.assertIn("Restart=on-failure", unit)
        # never starts, stops or waits on another Booth unit: none is named on any line, whatever the directive
        self.assertEqual([ln for ln in unit if any(u in ln for u in ("booth-voice", "booth-mic-select", "booth.service"))],
                         [])
        self.assertNotIn("[Install]", text)
        self.assertNotIn("booth-monitor", self.calls.read_text())  # never enabled or started by the install

    def test_installs_booth_stereo_which_only_booth_starts_from_a_config_its_pre_start_writes(self):  # #19
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.unit.parent / "booth-stereo.service").read_text()
        unit, conf = text.splitlines(), self.home / ".config/booth/stereo.conf"
        self.assertIn(f'ExecStart=/usr/bin/pipewire -c "{conf}"', unit)
        self.assertIn("BindsTo=pipewire.service", unit)
        self.assertIn("After=pipewire.service", unit)
        self.assertIn("Restart=on-failure", unit)
        # a new side or source restarts only it: it never starts, stops or waits on another Booth unit
        self.assertEqual([ln for ln in unit if any(u in ln for u in ("booth-voice", "booth-mic-select", "booth.service"))],
                         [])
        self.assertNotIn("[Install]", text)
        self.assertNotIn("booth-stereo", self.calls.read_text())  # never enabled or started by the install
        line = next(ln for ln in unit if ln.startswith("ExecStartPre="))
        argv = shlex.split(line.removeprefix("ExecStartPre=").replace("%%", "%").replace("$$", "$"))
        self.assertEqual(argv, ["/usr/bin/env", f"PYTHONPATH={self.app.parent}", "python3", "-P", "-m", "booth.stereo",
                                "--write", str(conf), str(self.app.parent / "plugins")])  # the installed copy
        plugin = self.app.parent / "plugins/sc4m_1916.so"  # changed since it verified: never loaded
        plugin.parent.mkdir(exist_ok=True)
        plugin.write_bytes(b"changed after install")
        before = self.calls.read_text()
        r = subprocess.run(argv, env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"booth_stereo"', conf.read_text())
        self.assertNotIn("ladspa", conf.read_text())
        self.assertEqual(self.calls.read_text(), before)  # no systemctl from inside the unit's own start

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
        self.assertEqual(self.calls.read_text().splitlines()[2:2 + A], VOICE_AGAIN)

    def test_booth_mic_is_set_up_even_with_the_background_service_switched_off(self):
        self.assertEqual(self.install().returncode, 0)
        self.env["UNIT_STATE"] = "disabled"
        self.calls.write_text("")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines()[2:2 + A], VOICE_AGAIN)

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

    def gsettings_calls(self):
        return self.gsettings.read_text().splitlines() if self.gsettings.exists() else []

    def test_gnomes_mic_mute_gets_ctrl_alt_m_only_when_it_has_no_binding(self):  # #21
        get = "gsettings get org.gnome.settings-daemon.plugins.media-keys mic-mute"
        for current in ("['']", "@as []"):
            self.env["MIC_MUTE"] = current
            self.gsettings.unlink(missing_ok=True)
            r = self.install()
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.gsettings_calls(),
                             [get, "gsettings set org.gnome.settings-daemon.plugins.media-keys mic-mute "
                                   "['<Control><Alt>m']"], current)
        for current in ("['<Super>m']", "['<Control><Alt>m']"):  # the user's own, or already ours: never rewritten
            self.env["MIC_MUTE"] = current
            self.gsettings.unlink(missing_ok=True)
            self.assertEqual(self.install().returncode, 0)
            self.assertEqual(self.gsettings_calls(), [get], current)

    def test_an_unreadable_mic_mute_is_never_written_and_the_install_goes_on(self):
        self.env["GSETTINGS_RC"] = "1"  # no GNOME schema, no session
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.gsettings_calls()), 1)
        self.assertIn("Ctrl+Alt+M", r.stderr)
        self.assertTrue(self.launcher.exists())

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
        # first: the old Booth's heal or poll would restart Booth Mic once more under booth.voice's sync
        self.assertEqual(self.calls.read_text().splitlines(),
                         [QUIT] + [OWNER, "sleep 0.5"] * 3 + [OWNER] + VOICE_CALLS + UNIT_CALLS)
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
        self.assertEqual(self.calls.read_text().splitlines(), [QUIT, OWNER] + VOICE_CALLS + UNIT_CALLS)
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
                         ["booth-mic-select.service", "booth-monitor.service", "booth-stereo.service", "booth-voice.service",
                          "booth.service"])  # no .new left

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
        self.assertEqual(self.calls.read_text().splitlines(), [QUIT, OWNER] + VOICE_CALLS + UNIT_CALLS)  # the service still restarts
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
        monitor_unit = unit.parent / "booth-monitor.service"
        stereo_unit = unit.parent / "booth-stereo.service"  # #19
        for u, key, conf in ((voice_unit, "ExecStart=/usr/bin/pipewire -c", "voice-run.conf"),
                             (voice_unit, "ExecCondition=/usr/bin/test -f", "voice.conf"),
                             (select_unit, "ExecStart=/usr/bin/pipewire -c", "voice-select.conf"),
                             (monitor_unit, "ExecStart=/usr/bin/pipewire -c", "monitor.conf"),
                             (monitor_unit, "ExecCondition=/usr/bin/test -f", "monitor.conf"),
                             (stereo_unit, "ExecStart=/usr/bin/pipewire -c", "stereo.conf")):
            line = next(ln for ln in u.read_text().splitlines() if ln.startswith(key))
            m = re.fullmatch(re.escape(key) + r' "((?:[^"\\%$]|\\["\\]|%%|\$\$)*)"', line)
            self.assertIsNotNone(m, line)
            self.assertEqual(re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], m[1]),
                             str(self.home / ".config/booth" / conf))
        line = next(ln for ln in voice_unit.read_text().splitlines() if ln.startswith("ExecStartPre="))
        word = r'"((?:[^"\\%$]|\\["\\]|%%|\$\$)*)"'
        m = re.fullmatch(rf'ExecStartPre=/usr/bin/env "PYTHONPATH=((?:[^"\\%$]|\\["\\]|%%|\$\$)*)" python3 -P -m '
                         rf'booth\.voice --write {word} {word}', line)
        self.assertIsNotNone(m, line)
        self.assertEqual([re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], w) for w in m.groups()],
                         [str(self.home / ".local/share/booth"), str(self.home / ".config/booth/voice-run.conf"),
                          str(self.home / ".local/share/booth/plugins")])
        line = next(ln for ln in voice_unit.read_text().splitlines() if ln.startswith("ExecStartPost="))
        m = re.fullmatch(r'ExecStartPost=-/usr/bin/env "PYTHONPATH=((?:[^"\\%$]|\\["\\]|%%|\$\$)*)" python3 -P -m '
                         r'booth\.mute --enforce', line)
        self.assertIsNotNone(m, line)
        self.assertEqual(re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], m[1]),
                         str(self.home / ".local/share/booth"))
        line = next(ln for ln in select_unit.read_text().splitlines() if ln.startswith("ExecCondition="))
        m = re.fullmatch(rf'ExecCondition=/usr/bin/env "PYTHONPATH=((?:[^"\\%$]|\\["\\]|%%|\$\$)*)" python3 -P -m '
                         rf'booth\.voice --select {word}', line)
        self.assertIsNotNone(m, line)
        self.assertEqual([re.sub(r'\\(.)|%(%)|\$(\$)', lambda m: m[1] or m[2] or m[3], w) for w in m.groups()],
                         [str(self.home / ".local/share/booth"), str(self.home / ".config/booth/voice-select.conf")])
        self.unit = unit
        pre = subprocess.run(self.pre_start(), env=self.env, cwd=self.home, capture_output=True, text=True, timeout=60)
        self.assertEqual(pre.returncode, 0, pre.stderr)  # the installed copy found, whatever the home's name
        if shutil.which("systemd-analyze"):  # read-only: parses the unit, starts nothing
            for u in (unit, voice_unit, select_unit, monitor_unit, stereo_unit):
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
