"""install.sh runs against a throwaway HOME only, never the real home folder."""
import filecmp
import os
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
        self.home = Path(tmp.name) / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
        self.env["HOME"] = str(self.home)
        self.app = self.home / ".local/share/booth/booth"
        self.launcher = self.home / ".local/bin/booth"
        self.desktop = self.home / f".local/share/applications/{APP_ID}.desktop"

    def install(self):
        return subprocess.run(["sh", str(REPO / "install.sh")], env=self.env, cwd=self.home,
                              capture_output=True, text=True, timeout=60)

    def test_installs_launcher_desktop_entry_and_a_copy_of_the_app(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.access(self.launcher, os.X_OK))
        self.assertIn(f"Exec={self.launcher}", self.desktop.read_text())
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

    def test_stays_inside_home(self):
        self.assertEqual(self.install().returncode, 0)
        code = [ln for ln in (REPO / "install.sh").read_text().splitlines() if not ln.lstrip().startswith("#")]
        self.assertFalse([ln for ln in code if "sudo" in ln])
        written = {p.relative_to(self.home).parts[:2] for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(written, {(".local", "bin"), (".local", "share")})

    def test_rerun_is_safe_and_replaces_stale_files(self):
        self.assertEqual(self.install().returncode, 0)
        (self.app / "stale.py").write_text("x = 1\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tree(self.app), tree(REPO / "booth"))
        self.assertEqual(list(self.app.parent.iterdir()), [self.app])

    def test_launcher_runs_the_installed_copy(self):
        self.assertEqual(self.install().returncode, 0)
        env = {k: v for k, v in self.env.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH")}
        # --help is answered before any window or display is touched.
        r = subprocess.run([str(self.launcher), "--help"], env=env, cwd=self.home, capture_output=True, text=True,
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
