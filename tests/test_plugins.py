"""Fetch and verify: the only third-party code Booth loads. Stub curl/apt-get/dpkg-deb on PATH: no test hits the network."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from booth import plugins
from tests.fixture import FIXTURES

RNNOISE, SC4M, LIMITER = "librnnoise_ladspa.so", "sc4m_1916.so", "fast_lookahead_limiter_1913.so"
FAKE = {RNNOISE: b"rnnoise bytes", SC4M: b"sc4m bytes", LIMITER: b"limiter bytes"}
FAKE_MANIFEST = {name: hashlib.sha256(data).hexdigest() for name, data in FAKE.items()}

CURL = """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
while [ $# -gt 1 ]; do [ "$1" = -o ] && out=$2; shift; done
cp "$STUB_DIR/linux-rnnoise.zip" "$out"
exit "${CURL_RC:-0}"
"""
APT = """#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
[ "${APT_DEBS:-1}" -ge 1 ] && cp "$STUB_DIR/swh.deb" ./swh-plugins_0.4.17-3build1_amd64.deb
[ "${APT_DEBS:-1}" -ge 2 ] && cp "$STUB_DIR/swh.deb" ./swh-plugins_0.4.17-3_amd64.deb
exit "${APT_RC:-0}"
"""
DPKG = """#!/bin/sh
echo "dpkg-deb $*" >> "$STUB_LOG"
mkdir -p "$3/usr/lib/ladspa" && cp -P "$STUB_DIR"/ladspa/* "$3/usr/lib/ladspa/"
exit "${DPKG_RC:-0}"
"""


class Verify(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.folder = self.dir / "plugins"
        self.folder.mkdir(mode=0o755)
        self.folder.chmod(0o755)  # whatever the umask

    def put(self, name, data):
        (self.folder / name).write_bytes(data)
        (self.folder / name).chmod(0o644)

    def refused(self, name, manifest=FAKE_MANIFEST):
        with self.assertRaises(plugins.Refused) as cm:
            plugins.verify(name, self.folder, manifest)
        return str(cm.exception)

    def test_a_matching_file_passes(self):
        self.put(SC4M, FAKE[SC4M])
        self.assertEqual(plugins.verify(SC4M, self.folder, FAKE_MANIFEST), self.folder / SC4M)

    def test_a_mismatch_is_refused(self):
        self.put(SC4M, b"sc4m bytez")
        self.assertIn("sha256", self.refused(SC4M))

    def test_a_truncated_or_extended_file_is_refused(self):
        self.put(SC4M, FAKE[SC4M][:-1])  # a partial download
        self.refused(SC4M)
        self.put(SC4M, FAKE[SC4M] + b"\0")  # the whole file is hashed, not a prefix
        self.refused(SC4M)

    def test_a_large_file_is_hashed_in_full(self):
        data = os.urandom(3 << 20)
        self.put(SC4M, data)
        manifest = {SC4M: hashlib.sha256(data).hexdigest()}
        self.assertEqual(plugins.verify(SC4M, self.folder, manifest), self.folder / SC4M)
        self.put(SC4M, data[:-1] + bytes([data[-1] ^ 1]))  # the last byte differs
        self.refused(SC4M, manifest)

    def test_a_missing_file_is_refused(self):
        self.assertIn("missing", self.refused(SC4M))

    def test_a_symlink_is_refused_even_to_matching_bytes(self):
        (self.dir / "elsewhere.so").write_bytes(FAKE[SC4M])
        (self.dir / "elsewhere.so").chmod(0o644)  # safe modes whatever the umask: only the symlink is wrong
        self.dir.chmod(0o755)
        (self.folder / SC4M).symlink_to(self.dir / "elsewhere.so")
        self.assertIn("can't open", self.refused(SC4M))
        (self.folder / SC4M).unlink()
        shutil.copy(self.dir / "elsewhere.so", self.folder / "copy.so")
        (self.folder / SC4M).symlink_to("copy.so")  # inside the folder too: only the real file is trusted
        self.assertIn("can't open", self.refused(SC4M))

    def test_a_folder_or_fifo_in_its_place_is_refused(self):
        (self.folder / SC4M).mkdir(mode=0o755)
        (self.folder / SC4M).chmod(0o755)  # safe modes whatever the umask: only the type is wrong
        self.assertIn("not a regular file", self.refused(SC4M))
        os.mkfifo(self.folder / RNNOISE, 0o644)
        (self.folder / RNNOISE).chmod(0o644)
        out = []  # must not block waiting for a writer: a hang fails this test instead of the gate
        t = threading.Thread(target=lambda: out.append(self.refused(RNNOISE)), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "verify() blocked on a fifo")
        self.assertIn("not a regular file", out[0])

    def test_a_file_or_folder_others_can_write_is_refused(self):
        self.put(SC4M, FAKE[SC4M])
        for mode in (0o664, 0o646, 0o666):
            (self.folder / SC4M).chmod(mode)
            self.assertIn("writable by others", self.refused(SC4M))
        (self.folder / SC4M).chmod(0o644)
        for mode in (0o775, 0o757, 0o777):  # they could swap the file after it verifies
            self.folder.chmod(mode)
            self.assertIn("writable by others", self.refused(SC4M))
        self.folder.chmod(0o755)
        self.assertEqual(plugins.verify(SC4M, self.folder, FAKE_MANIFEST), self.folder / SC4M)

    def test_a_file_or_folder_that_is_not_yours_is_refused(self):
        self.put(SC4M, FAKE[SC4M])
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.assertIn("not yours", self.refused(SC4M))

    def test_a_name_not_in_the_manifest_is_refused(self):
        self.put("other.so", b"x")
        self.refused("other.so")
        # a path, even one a manifest pins, never reaches outside the folder
        self.refused("../plugins/other.so", {"../plugins/other.so": hashlib.sha256(b"x").hexdigest()})
        self.put(SC4M, FAKE[SC4M])
        self.refused("../plugins/" + SC4M)

    def test_the_manifest_pins_the_three_plugins(self):
        self.assertEqual(set(plugins.MANIFEST), {RNNOISE, SC4M, LIMITER})
        for sha in plugins.MANIFEST.values():
            self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_the_default_folder_follows_xdg_data_home_unless_relative(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        env["HOME"] = str(self.dir)
        code = "from booth import plugins; print(plugins.folder())"
        run = lambda: subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True,
                                     cwd=Path(__file__).parent.parent).stdout.strip()
        self.assertEqual(run(), f"{self.dir}/.local/share/booth/plugins")
        env["XDG_DATA_HOME"] = "rel/share"
        self.assertEqual(run(), f"{self.dir}/.local/share/booth/plugins")
        env["XDG_DATA_HOME"] = str(self.dir / "data")
        self.assertEqual(run(), f"{self.dir}/data/booth/plugins")


class Fetch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.folder = self.dir / "data" / "plugins"
        stubs, src = self.dir / "stubs", self.dir / "src"
        (src / "ladspa").mkdir(parents=True)
        stubs.mkdir()
        for name, text in (("curl", CURL), ("apt-get", APT), ("dpkg-deb", DPKG)):
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        with zipfile.ZipFile(src / "linux-rnnoise.zip", "w") as z:
            z.writestr("linux-rnnoise/ladspa/" + RNNOISE, FAKE[RNNOISE])
            z.writestr("linux-rnnoise/vst/librnnoise_mono.so", b"not this one")
        (src / "swh.deb").write_bytes(b"a deb")
        for name in (SC4M, LIMITER):
            (src / "ladspa" / name).write_bytes(FAKE[name])
        (src / "ladspa" / "sc4_1882.so").write_bytes(b"not needed")
        self.log = self.dir / "stubs.log"
        self.log.write_text("")
        self.env = {"PATH": f"{stubs}:{os.environ['PATH']}", "STUB_LOG": str(self.log), "STUB_DIR": str(src)}
        patch = unittest.mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def fetch(self):
        return plugins.fetch(self.folder, FAKE_MANIFEST)

    def verified(self):
        ok = []
        for name in FAKE_MANIFEST:
            try:
                plugins.verify(name, self.folder, FAKE_MANIFEST)
                ok.append(name)
            except plugins.Refused:
                pass
        return sorted(ok)

    def test_fetches_the_three_plugins_into_the_folder(self):
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.verified(), sorted(FAKE))
        self.assertEqual(sorted(os.listdir(self.folder)), sorted(FAKE))  # nothing else: no zip, deb or extras
        self.assertEqual(os.listdir(self.folder.parent), ["plugins"])  # no staging folder left behind
        calls = self.log.read_text().splitlines()
        self.assertIn(f"curl -fsSL --proto =https -o", calls[0])
        self.assertTrue(calls[0].endswith(plugins.RNNOISE_URL), calls[0])
        self.assertEqual(plugins.RNNOISE_URL, "https://github.com/werman/noise-suppression-for-voice/releases/"
                                              "download/v1.10/linux-rnnoise.zip")
        self.assertEqual(calls[1], "apt-get download swh-plugins=0.4.17-3build1")  # pinned, not apt's latest
        self.assertRegex(calls[2], r"^dpkg-deb -x \S+/swh-plugins_0.4.17-3build1_amd64.deb \S+$")
        self.assertEqual(len(calls), 3)

    def test_verified_files_are_never_downloaded_again(self):
        self.assertEqual(self.fetch(), [])
        self.log.write_text("")
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.log.read_text(), "")

    def test_a_failed_download_leaves_nothing_that_verifies_and_the_rest_still_installs(self):
        os.environ["CURL_RC"] = "56"  # the stub wrote the file, then curl failed: a partial download
        failed = self.fetch()
        self.assertEqual(len(failed), 1)
        self.assertIn("RNNoise", failed[0])
        self.assertEqual(self.verified(), sorted([SC4M, LIMITER]))
        self.assertFalse((self.folder / RNNOISE).exists())
        self.assertEqual(os.listdir(self.folder.parent), ["plugins"])

    def test_a_failed_apt_or_dpkg_step_is_named(self):
        os.environ["APT_RC"] = "100"
        failed = self.fetch()
        self.assertEqual(len(failed), 1)
        self.assertIn("SWH", failed[0])
        self.assertEqual(self.verified(), [RNNOISE])
        del os.environ["APT_RC"]
        os.environ["DPKG_RC"] = "2"
        self.assertEqual(len(self.fetch()), 1)
        self.assertEqual(self.verified(), [RNNOISE])

    def test_a_download_that_does_not_match_its_pin_is_refused_and_not_placed(self):
        (Path(self.env["STUB_DIR"]) / "ladspa" / SC4M).write_bytes(b"sc4m bytes, updated upstream")
        failed = self.fetch()
        self.assertEqual(len(failed), 1)
        self.assertIn(SC4M, failed[0])
        self.assertIn("sha256", failed[0])
        self.assertIn("0.4.17-3build1", failed[0])  # names the version the pins were taken from
        self.assertFalse((self.folder / SC4M).exists())
        self.assertEqual(self.verified(), sorted([RNNOISE, LIMITER]))

    def test_a_swapped_file_is_replaced_by_a_verified_download(self):
        self.folder.mkdir(parents=True)
        (self.folder / LIMITER).write_bytes(b"swapped")
        self.assertEqual(self.fetch(), [])
        self.assertEqual((self.folder / LIMITER).read_bytes(), FAKE[LIMITER])

    def test_a_corrupt_zip_fails_rnnoise_alone(self):
        data = b"\0" * 400000
        zip_ = Path(self.env["STUB_DIR"]) / "linux-rnnoise.zip"
        with zipfile.ZipFile(zip_, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(plugins.RNNOISE_MEMBER, data)
        raw = bytearray(zip_.read_bytes())
        raw[30 + len(plugins.RNNOISE_MEMBER)] ^= 0xFF  # the directory is intact, the deflate stream isn't
        zip_.write_bytes(bytes(raw))
        failed = plugins.fetch(self.folder, {**FAKE_MANIFEST, RNNOISE: hashlib.sha256(data).hexdigest()})
        self.assertEqual(len(failed), 1)
        self.assertIn("RNNoise: download failed", failed[0])
        self.assertEqual(self.verified(), sorted([SC4M, LIMITER]))  # the SWH fetch still ran

    def test_what_fetch_creates_verifies_under_a_loose_umask(self):
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.verified(), sorted(FAKE))
        self.assertEqual(self.folder.stat().st_mode & 0o022, 0)
        self.assertEqual(os.umask(0o002), 0o002)  # put back

    def rezip(self, offset=None, value=None, member=plugins.RNNOISE_MEMBER):
        zip_ = Path(self.env["STUB_DIR"]) / "linux-rnnoise.zip"
        with zipfile.ZipFile(zip_, "w") as z:
            z.writestr(member, FAKE[RNNOISE])
        raw = bytearray(zip_.read_bytes())
        if offset is not None:  # the local header's field, then the central directory's (2 bytes further in)
            for sig, extra in ((b"PK\x03\x04", 0), (b"PK\x01\x02", 2)):
                raw[raw.index(sig) + offset + extra] = value
        zip_.write_bytes(bytes(raw))

    def assert_rnnoise_alone_fails(self):
        failed = self.fetch()
        self.assertEqual(len(failed), 1, failed)
        self.assertIn("RNNoise: download failed", failed[0])
        self.assertEqual(self.verified(), sorted([SC4M, LIMITER]))  # the SWH fetch still ran

    def test_a_zip_without_the_plugin_fails_rnnoise_alone(self):
        self.rezip(member="linux-rnnoise/ladspa/renamed.so")
        self.assert_rnnoise_alone_fails()

    def test_an_encrypted_member_fails_rnnoise_alone(self):
        self.rezip(6, 0x01)  # general purpose flag bit 0: encrypted
        self.assert_rnnoise_alone_fails()

    def test_an_unsupported_compression_fails_rnnoise_alone(self):
        self.rezip(8, 99)  # compression method 99 (AES), which zipfile can't read
        self.assert_rnnoise_alone_fails()

    def test_apt_leaving_no_deb_or_two_fails_swh_alone(self):
        for debs in ("0", "2"):
            os.environ["APT_DEBS"] = debs
            failed = self.fetch()
            self.assertEqual(len(failed), 1, (debs, failed))
            self.assertIn("SWH plugins 0.4.17-3build1: download failed", failed[0])
            self.assertEqual(self.verified(), [RNNOISE])

    def test_glob_characters_in_the_data_path_are_literal(self):
        self.folder = self.dir / "a[1]" / "plugins"
        self.assertEqual(self.fetch(), [])
        self.assertEqual(self.verified(), sorted(FAKE))

    def test_a_symlink_in_the_deb_is_never_placed(self):
        src = Path(self.env["STUB_DIR"])
        (src / "elsewhere.so").write_bytes(FAKE[SC4M])  # the pinned bytes, but reached through a link
        (src / "ladspa" / SC4M).unlink()
        (src / "ladspa" / SC4M).symlink_to(src / "elsewhere.so")
        failed = self.fetch()
        self.assertEqual(len(failed), 1)
        self.assertIn(SC4M, failed[0])
        self.assertFalse(os.path.lexists(self.folder / SC4M))
        self.assertEqual(self.verified(), sorted([RNNOISE, LIMITER]))

    def test_with_no_network_nothing_is_written(self):
        os.environ.update(CURL_RC="6", APT_RC="100")
        self.assertEqual(len(self.fetch()), 2)
        self.assertFalse(self.folder.parent.exists() and os.listdir(self.folder.parent))

    def test_the_command_prints_each_verdict_and_fails_when_one_is_refused(self):
        env = {**os.environ, "CURL_RC": "6"}
        code = ("import sys; from booth import plugins; plugins.MANIFEST = %r; sys.argv[1:] = [%r]; "
                "sys.exit(plugins.main())" % (FAKE_MANIFEST, str(self.folder)))
        r = subprocess.run(["python3", "-c", code], env=env, capture_output=True, text=True,
                           cwd=Path(__file__).parent.parent)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{SC4M}: verified", r.stdout)
        self.assertIn(f"{RNNOISE}: refused (missing)", r.stdout)
        self.assertIn("RNNoise", r.stderr)


C_PLUGIN = r"""
#include <stddef.h>
typedef struct { int hint; float lower, upper; } Hint;
typedef struct {
  unsigned long id; const char *label; int props; const char *name, *maker, *copyright;
  unsigned long count; const int *descs; const char * const *names; const Hint *hints;
  void *data, *f[8];
} Desc;
static const int descs[] = {0x5, 0x9, 0xA, 0x6, 0x5};
static const char * const names[] = {"Threshold (dB)", "Input", "Output", "latency", "Mix"};
static const Hint hints[] = {{0x3, -30.0f, 0.0f}, {0, 0, 0}, {0, 0, 0}, {0, 0, 0}, {0x2, 0, 1.0f}};
static const Desc d = {4242, "fake_mono", 0, "Fake mono", "m", "c", 5, descs, names, hints, NULL, {0}};
static const Desc d2 = {4243, "fake_stereo", 0, "Fake stereo", "m", "c", 1, descs + 1, names + 1, hints + 1,
                        NULL, {0}};  /* a second descriptor, like RNNoise's mono + stereo */
const Desc *ladspa_descriptor(unsigned long i) { return i == 0 ? &d : i == 1 ? &d2 : NULL; }
"""


@unittest.skipUnless(shutil.which("gcc"), "needs gcc to build a stand-in LADSPA plugin")
class Ports(unittest.TestCase):
    def test_reads_labels_and_exact_port_names_through_ladspa_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, so = Path(tmp) / "fake.c", Path(tmp) / "fake.so"
            src.write_text(C_PLUGIN)
            subprocess.run(["gcc", "-shared", "-fPIC", "-o", so, src], check=True)
            so.chmod(0o755)
            manifest = {"fake.so": hashlib.sha256(so.read_bytes()).hexdigest()}
            self.assertEqual(plugins.ports("fake.so", Path(tmp), manifest), [{
                "label": "fake_mono", "id": 4242, "name": "Fake mono", "ports": [
                    {"index": 0, "name": "Threshold (dB)", "type": "control", "direction": "input",
                     "lower": -30.0, "upper": 0.0},
                    {"index": 1, "name": "Input", "type": "audio", "direction": "input"},
                    {"index": 2, "name": "Output", "type": "audio", "direction": "output"},
                    {"index": 3, "name": "latency", "type": "control", "direction": "output"},
                    {"index": 4, "name": "Mix", "type": "control", "direction": "input", "upper": 1.0}]},
                {"label": "fake_stereo", "id": 4243, "name": "Fake stereo", "ports": [
                    {"index": 0, "name": "Input", "type": "audio", "direction": "input"}]}])
            data = so.read_bytes()
            so.unlink()  # a new file: rewriting the loaded one in place would crash this process
            so.write_bytes(data + b"\0")  # never loaded unless it verifies
            so.chmod(0o755)
            with self.assertRaises(plugins.Refused):
                plugins.ports("fake.so", Path(tmp), manifest)


class PortsFixture(unittest.TestCase):
    """The captured labels and port names #16 builds the voice chain from."""

    def test_the_fixture_covers_the_pinned_files_and_the_three_plugins(self):
        captured = json.loads((FIXTURES / "ladspa" / "ports.json").read_text())
        self.assertEqual(set(captured), set(plugins.MANIFEST))
        labels = {d["label"] for descs in captured.values() for d in descs}
        self.assertTrue({"sc4m", "fastLookaheadLimiter"} <= labels, labels)
        self.assertTrue(any("mono" in d["label"] for d in captured[RNNOISE]), captured[RNNOISE])


if __name__ == "__main__":
    unittest.main()
