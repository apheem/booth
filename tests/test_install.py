"""install.sh runs against a throwaway HOME only, never the real home folder."""
import filecmp
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.github.apheem.Booth"


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if "__pycache__" not in p.parts)


class Install(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "my home"  # a space, which the desktop entry must survive
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        self.env["HOME"] = str(self.home)
        # A stub systemctl first on PATH records its argv: an install test never reaches the real user manager.
        stubs = Path(tmp.name) / "stubs"
        stubs.mkdir()
        self.calls = Path(tmp.name) / "systemctl.log"
        (stubs / "systemctl").write_text('#!/bin/sh\necho "$*" >> "$SYSTEMCTL_LOG"\nexit "${SYSTEMCTL_RC:-0}"\n')
        (stubs / "systemctl").chmod(0o755)
        self.env.update(PATH=f"{stubs}:{self.env['PATH']}", SYSTEMCTL_LOG=str(self.calls))
        self.app = self.home / ".local/share/booth/booth"
        self.launcher = self.home / ".local/bin/booth"
        self.desktop = self.home / f".local/share/applications/{APP_ID}.desktop"
        self.unit = self.home / ".config/systemd/user/booth.service"

    def install(self, repo=REPO):
        return subprocess.run(["sh", str(repo / "install.sh")], env=self.env, cwd=self.home,
                              capture_output=True, text=True, timeout=60)

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
        self.assertEqual(written, {(".local", "bin"), (".local", "share"), (".config", "systemd")})

    def test_installs_enables_and_starts_the_service(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        # restart, not start: a re-install must run the new copy (start leaves a running old one as it is).
        self.assertEqual(self.calls.read_text().splitlines(),
                         ["--user daemon-reload", "--user enable booth.service", "--user restart booth.service"])
        self.assertNotIn("couldn't start", r.stderr)
        unit = self.unit.read_text().splitlines()
        self.assertIn(f'ExecStart=/bin/sh "{self.launcher}" --background', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=graphical-session.target", unit)  # GTK needs the session's display
        self.assertIn("After=graphical-session.target", unit)
        self.assertNotIn(str(REPO), self.unit.read_text())

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

    def test_relative_xdg_data_home_is_ignored(self):
        self.env["XDG_DATA_HOME"] = "rel/share"  # invalid per the XDG spec: must not install relative to the cwd
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(tree(self.app), tree(REPO / "booth"))
        self.assertFalse((self.home / "rel").exists())
        self.assertIn(f"PYTHONPATH='{self.home}/.local/share/booth", self.launcher.read_text())

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
        if shutil.which("systemd-analyze"):  # read-only: parses the unit, starts nothing
            v = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True,
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
