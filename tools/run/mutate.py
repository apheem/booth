#!/usr/bin/env python3
"""mutate.py — Booth's mutation runner: hand it a LIST of mutants, get one verdict line per mutant.

  python3 tools/run/mutate.py <mutants file> [--tree <dir>]   run every mutant, print the verdicts and the summary
  python3 tools/run/mutate.py --self-check                    prove the runner on a scratch repo (the gate runs it)

THE TREE is --tree or the cwd: your worktree root. Every file `git ls-files -co --exclude-standard` lists (uncommitted
work included) is copied into a fresh LANE in $TMPDIR for each mutant; the tree itself is never written.
THE MUTANTS FILE lives outside the tree ($TMPDIR): a JSON array, or one JSON object per line:
  {"id": "m01_ir", "file": "booth/devices.py", "find": "if not is_ir:", "replace": "if True:",
   "tests": ["tests/test_devices.py"], "t": "test_never_ir"}
  - id: letters, digits, _ . - and unique. file + find + replace, or "edits": [{file, find, replace}, ...] applied in
    order; each find must match EXACTLY ONCE in its file as the earlier edits left it.
  - tests: test files (tests/test_x.py) or dotted ids (tests.test_x.Class.test_y). "t": optional -k name filter.
Each (tests, t) set first runs unmutated: a red baseline makes its mutants ERROR. Tests run through sandbox.sh.
One line per mutant, then the summary:
  KILLED <id> — <first failed test>        a test failed: the test bites
  SURVIVED <id> — <tests> stayed green     a hollow test
  ERROR <id> — <why>                       find matched n times · no such file · baseline red · no test ran ·
                                           import failed (the mutant broke the import, not a test) · timed out
  <n> mutants: <k> KILLED, <s> SURVIVED, <e> ERROR — <secs>s
Logs: ~/.cache/booth-mutate/<run>/<id>.log holds each mutant's test output (the failing assertion of a KILLED).
Exit 0 when every mutant has a verdict line (a SURVIVED is data), 2 on bad usage or input.
shortcut: lanes run one at a time and lanes have no .git; parallelize if the suite ever takes minutes.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT_S = int(os.environ.get("MUTATE_TIMEOUT_S", "300"))


def load(path):
    text = Path(path).read_text()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def check(mutants):
    seen = set()
    for m in mutants:
        mid = m.get("id", "")
        if not re.fullmatch(r"[\w.-]+", mid) or mid in seen:
            sys.exit(f"mutate: bad or duplicate id {mid!r}")
        seen.add(mid)
        if not m.get("tests"):
            sys.exit(f"mutate: {mid} lists no tests")
        if "edits" not in m and not all(k in m for k in ("file", "find", "replace")):
            sys.exit(f"mutate: {mid} needs file + find + replace, or edits")


def targets(tests):
    # tests/test_x.py -> tests.test_x, so the run matches the gate's `discover -s tests -t .` import names
    return [t[:-3].replace("/", ".") if t.endswith(".py") else t for t in tests]


def run_tests(lane, tests, t, log):
    cmd = ["bash", str(HERE / "sandbox.sh"), sys.executable, "-m", "unittest", *targets(tests)]
    if t:
        cmd += ["-k", t]
    try:
        p = subprocess.run(cmd, cwd=lane, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        log.write_text(f"timed out after {TIMEOUT_S}s\n")
        return "timeout", ""
    out = p.stdout + p.stderr
    log.write_text(" ".join(cmd) + "\n" + out)
    if p.returncode == 0:
        return "green", ""
    if p.returncode == 5 or "NO TESTS RAN" in out:
        return "none", ""
    m = re.search(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", out, re.M)
    # a crash before any test ran (a named module that won't import) prints no FAIL/ERROR line: never a kill
    if not m or "_FailedTest" in out:
        return "import", ""
    return "red", m.group(2)


def make_lane(tree, files):
    lane = Path(tempfile.mkdtemp(prefix="booth-lane-"))
    for f in files:
        dst = lane / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        if (tree / f).is_file():
            shutil.copy2(tree / f, dst)
    return lane


def apply(lane, m):
    for e in m.get("edits") or [m]:
        p = lane / e["file"]
        if not p.is_file():
            return f"no such file {e['file']}"
        src = p.read_text()
        n = src.count(e["find"])
        if n != 1:
            return f"find matched {n} times in {e['file']}"
        p.write_text(src.replace(e["find"], e["replace"]))
    return None


def run(mutants_file, tree):
    tree = Path(tree).resolve()
    mutants = load(mutants_file)
    check(mutants)
    files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], cwd=tree, text=True).split("\n")
    files = [f for f in files if f]
    rundir = Path.home() / ".cache/booth-mutate" / time.strftime("%m%d-%H%M%S")
    rundir.mkdir(parents=True, exist_ok=True)
    t0, baselines, lines, counts = time.time(), {}, [], {"KILLED": 0, "SURVIVED": 0, "ERROR": 0}
    for m in mutants:
        key = (tuple(m["tests"]), m.get("t"))
        if key not in baselines:
            lane = make_lane(tree, files)
            baselines[key], _ = run_tests(lane, m["tests"], m.get("t"), rundir / f"baseline-{len(baselines)}.log")
            shutil.rmtree(lane)
        base = baselines[key]
        if base != "green":
            why = {"none": "no test ran (a typo in tests or t?)", "timeout": f"baseline timed out after {TIMEOUT_S}s"}
            line = f"ERROR {m['id']} — " + why.get(base, f"baseline red: {', '.join(m['tests'])}")
        else:
            lane = make_lane(tree, files)
            err = apply(lane, m)
            if err:
                line = f"ERROR {m['id']} — {err}"
            else:
                state, first = run_tests(lane, m["tests"], m.get("t"), rundir / f"{m['id']}.log")
                line = {
                    "red": f"KILLED {m['id']} — {first}",
                    "green": f"SURVIVED {m['id']} — {', '.join(m['tests'])} stayed green",
                    "import": f"ERROR {m['id']} — import failed (the mutant broke the import, not a test)",
                    "none": f"ERROR {m['id']} — no test ran",
                    "timeout": f"ERROR {m['id']} — timed out after {TIMEOUT_S}s",
                }[state]
            shutil.rmtree(lane)
        counts[line.split()[0]] += 1
        lines.append(line)
        print(line, flush=True)
    print(f"{len(mutants)} mutants: {counts['KILLED']} KILLED, {counts['SURVIVED']} SURVIVED, "
          f"{counts['ERROR']} ERROR — {time.time() - t0:.0f}s (logs {rundir})")
    return lines


def self_check():
    with tempfile.TemporaryDirectory() as d:
        tree = Path(d) / "tree"
        (tree / "tests").mkdir(parents=True)
        (tree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (tree / "tests/__init__.py").write_text("")
        (tree / "tests/test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
            "    def test_hollow(self):\n        self.assertTrue(callable(add))\n")
        subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
        mf = Path(d) / "mutants.jsonl"
        add = {"file": "calc.py", "tests": ["tests/test_calc.py"]}
        mf.write_text("\n".join(json.dumps(m) for m in [
            {**add, "id": "kill", "find": "a + b", "replace": "a - b", "t": "test_add"},
            {**add, "id": "hollow", "find": "a + b", "replace": "a * b", "t": "test_hollow"},
            {**add, "id": "twice", "find": "a", "replace": "x"},
            {**add, "id": "syntax", "find": "return a + b", "replace": "return a +"},
            {**add, "id": "notest", "find": "a + b", "replace": "a - b", "t": "no_such_test"},
        ]))
        got = [l.split(" — ")[0] for l in run(mf, tree)]
        want = ["KILLED kill", "SURVIVED hollow", "ERROR twice", "ERROR syntax", "ERROR notest"]
        assert got == want, f"SELF-CHECK FAILED: {got} != {want}"
        assert (tree / "calc.py").read_text().count("a + b") == 1, "SELF-CHECK FAILED: the tree was written"
        print("SELF-CHECK OK")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a == ["--self-check"]:
        self_check()
    elif len(a) in (1, 3) and (len(a) == 1 or a[1] == "--tree"):
        run(a[0], a[2] if len(a) == 3 else ".")
    else:
        sys.exit(__doc__)
