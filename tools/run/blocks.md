The fixed text every /run-implement agent gets. Agents are pointed at a `## ` section by name and follow it verbatim;
the orchestrator never retypes one into a brief. Edit a block here and every later run gets it. `{MAIN}` is the shared
checkout's path, `{INTEG}` the run's base branch (`main`, or a chain's `integration-<tag>`).
The repo's CLAUDE.md agent rules (issue #3 writes them) apply on top of these; where the two differ, the stricter wins.

## TDD
Work test-first (the `tdd` skill's red-green-refactor loop): write the failing test, watch it fail for the
RIGHT reason, make it pass, then tidy. A test written after the code passes on the first run and proves
nothing about the code.

RED-FIRST EVIDENCE IS THE DELIVERABLE, not the claim. For every behaviour this slice adds, paste into the
PR body: the test name, the production line you broke (or the pre-fix state), and the actual failing
assertion text. "Tests written first" with no red output is not evidence, and the audit will mutate your
tests anyway: a test that survives its own mutation comes back as a must-fix.

INNER LOOP — run only the test modules your change touches, always through the sandbox:
  bash tools/run/sandbox.sh python3 -m unittest tests.test_<module>
The whole suite is the gate's job. Tests read FIXTURES (captured device output with fake serials), never a live
device, the real PipeWire graph, gsettings or the user's real settings file; the sandbox cuts those off, so a test
that only passes outside it is environment-green.

## GATE
THE GATE — the one gate rule. PUSH FIRST, then gate the commit you pushed, from your worktree root:
  bash tools/run/gate.sh
It takes seconds and prints `compile`, `unit` and `mutate` PASS|FAIL lines, then `GATE EXIT 0` (green) or `1` (red).
ONE GATE PER PUSHED COMMIT: after a fix, commit, push and gate that commit once.
QUOTE the gate's summary lines (each stage line and the GATE EXIT line) in a PR comment, never raw test output.

## HEADLESS
HEADLESS: never halt to ask for authorization, never finish with zero files changed; if confinement blocks the wiring,
make the minimal edit and list it.

## FORBIDDEN
FORBIDDEN: `git stash` (the stash stack is shared across worktrees), force-push, any git command in the shared checkout
{MAIN} or another agent's worktree, `cd <abs path> && git …` chains and `git -C` (run git from YOUR worktree root with
relative paths), `rm` of absolute paths, sudo, committing a scratch file (write a PR body or comment outside your
worktree: `--body-file "${TMPDIR:-/tmp}/pr-<n>.md"`).
THE USER'S DESK IS LIVE — they may be on a call right now. Never: open the BRIO's IR video node (face login uses it);
restart, stop or reconfigure PipeWire or WirePlumber; start, stop or restart a Booth systemd user service; run the
install script against the real home folder; touch system config; open a window on the user's display; turn 48V
phantom power on. A READ-ONLY probe of a live device (`v4l2-ctl --list-ctrls-menus`, `amixer -c <card> contents`,
`pw-dump`, `wpctl status`) to capture a fixture or check a fact is allowed; any live WRITE (a camera control, a mixer
value, the default sink or source, a volume, a HID++ request) is allowed only to the implementer or a hand-patch,
only when the AC can't be checked otherwise, and every value you change is read first and put back before you
finish: say which in your return. Auditors, confirms and skeptics never write to a live device.
PUBLIC REPO: never commit a device serial (fixtures use the one shared fake serial), a real camera frame, a screenshot
showing one, or a third-party binary.

## STANDING
STANDING SCOPE RULES (verbatim):
Only genuine correctness / acceptance-criterion failures are BLOCKERs.
Do NOT flag: forward seams owned by later slices of the PRD, widget polish, test naming, style.
The look of a page, live audio quality and on-device ACs are the user's hardware checklist (#23), not yours — do not
flag them; DO flag logic that would make a hardware check fail (a wrong control name, channel or node).
A permanent by-design fact that cannot be removed in this slice is a plain MINOR with latent_risk:false.
Never re-flag a value an AC or an "Orchestrator adjudication" comment explicitly specifies; such a comment
SUPERSEDES the AC line it names — never flag adjudicated behaviour, never report adjudicated-away behaviour as missing.
latent_risk:true for a MINOR that would plausibly cause a real bug, a stuck device state, a privacy leak or
expensive rework later.
hollow_test:true for any test that stays green under the mutation you name (a runner `SURVIVED` line).
Read via git fetch + git show/diff; do NOT modify code, do NOT checkout or stash in any shared checkout.
Record audited_head = git rev-parse --short=8 origin/<branch> FIRST.
An empty findings array is a valid, useful answer.

## SKEPTIC
You are an INDEPENDENT SKEPTIC. This PR already passed its audit (and any patch round + confirm). Your job is to
BREAK it: construct the device events, settings files or inputs that leave hardware in a wrong state, put the user's
voice somewhere it must not go, or let unverified code run. Treat each suspicion as real until you disprove it with
the code or a test.

SETUP: your OWN isolated worktree on a detached `origin/<branch>`. Prove each finding with a throwaway repro test in
that worktree (`bash tools/run/sandbox.sh python3 -m unittest tests.test_<x>`), driven by fixtures. Repro tests are
NEVER committed or pushed; edit nothing outside your worktree; never write to a live device.
The contract is the issue(s) as amended by their "Orchestrator adjudication" comments (an adjudication SUPERSEDES the
body; never re-flag adjudicated behaviour). Only real defects: no style, naming or forward seams owned by a later
slice. A missing abstraction is never a finding; a simplification that drops validation at a trust boundary, error
handling that prevents losing the user's settings, or an AC is.

YOUR KIND (your prompt names it):
- HARDWARE: every write the PR makes to a device or the audio graph. The right device every time (resolved by model
  or name on each use, never a cached ALSA card number or PipeWire node id that changes on replug; never the BRIO IR
  node); the right value (control ranges from the device, not hardcoded; extension-unit bytes and HID++ payloads match
  the spec); 48V never switched on without the user's confirmation; nothing left changed after a failure, a timeout or
  an unplug mid-write; flapping (replug ten times fast) never ends in a stuck or wrong state.
- PRIVACY (who hears or sees the user): mute holds through every retarget and fallback and is never cleared by one;
  a call never gets the user's voice on one side only (Booth Mic stays mono whatever Booth Stereo says); processed
  monitoring and Direct Monitor never route the voice to speakers on the Scarlett's rear outputs; the preview never
  opens the IR node and stops when the page is hidden; a notification or sidebar label always says when the mic changed.
- TRUST (what code and files Booth loads or writes): a plugin loads only from the verified folder and only after its
  sha256 matched the one in the repo, every time, with a mismatch refused (including a file swapped after the check);
  the install script needs no sudo, stays inside the home folder, copies (never symlinks into a repo or worktree) and
  is safe to re-run; a corrupt or half-written settings file is backed up, never silently lost; no serial or real
  frame reaches the repo.

OUTPUT: verdict SHIP or NO-SHIP (any BLOCKER is a NO-SHIP), audited_head, and at most 12 findings, each one line of
evidence: severity, location file:line, and a description naming the input that breaks it and how you proved it (the
repro test's name, or the code path).

A FIX-SKEPTIC (your prompt says so) reviews ONLY the commits since the named SHA (`git diff <sha>..origin/<branch>`):
did each fix close its finding for EVERY input, not just the repro's, and did it break anything that worked before?
Report each finding still open and each regression; SHIP only when every finding is closed and nothing broke. Same output.

## MUTATION
MUTATION RUNNER: every mutation goes through tools/run/mutate.py, one run for the whole list.
Never write your own mutation script; never edit your worktree to mutate; a gap in the runner goes in your hand-back
(it becomes an issue). From your worktree root:
  python3 tools/run/mutate.py "${TMPDIR:-/tmp}/mutants-<n>.jsonl" --tree "$PWD"
The mutants file lives OUTSIDE your worktree: a JSON array, or one JSON object per line:
  {"id":"m01_ir","file":"booth/devices.py","find":"if not is_ir:","replace":"if True:","tests":["tests/test_devices.py"],"t":"test_never_ir"}
`id`: letters, digits, `_`, `.`, `-`, unique. `find` must match EXACTLY ONCE in `file` (or `"edits": [{file, find,
replace}, …]`, applied in order). `tests`: test files or dotted ids; `"t"`: an optional name filter (unittest -k).
It runs in the foreground (seconds per mutant) and prints, in input order:
  KILLED <id> — <first failed test>        the test bites; its failing assertion is in the log folder it names
  SURVIVED <id> — <tests> stayed green     a hollow test: hollow_test:true, quote this line
  ERROR <id> — <why>                       no verdict (find matched n times, no test ran, import failed, timed out,
                                           baseline red): fix that mutant and run it again
  <n> mutants: <k> KILLED, <s> SURVIVED, <e> ERROR — <secs>s
A test the runner can't run (lanes have no .git) gets ONE MINOR finding, category `tool-gap`, latent_risk:false,
naming the test, the ERROR line and the mutation it would have run: never hollow_test, never a hand mutation.

## REPO FACTS
REPO FACTS:
- Booth: Python 3.12, PyGObject, GTK 4.14, libadwaita 1.5, GStreamer — all from the system. Nothing to compile, no
  pip installs, no new dependency, no sudo. The PRD is issue #1; its module list is the map.
- Tests: standard-library `unittest` only. `tests/` is a package (`tests/__init__.py`), files are
  `tests/test_*.py`, fixtures under `tests/fixtures/`. The gate runs `python3 -m unittest discover -s tests -t .`
  from the repo root inside sandbox.sh. The UI, preview and meters are not unit-tested: keep logic in pure modules
  (discovery, parsers, the voice-chain builder, the routing policy `next(state, event) → actions`, the state store)
  and test those; widgets stay thin.
- Devices are found by model or name on every use (BRIO `046d:085e` color node = the one offering MJPEG; the
  Scarlett's mic is capture channel 2 for the XLR input, read from its routing; the PRO X is "off" when its battery
  query fails, never by PipeWire presence). ALSA card numbers and PipeWire node ids change on replug.
- Fixtures are real captured output with every serial replaced by the one shared fake serial; a test fails on any
  other serial.
- CI does not exist: the gate-summary comment on the PR is the check.
- `rg` is not installed: use `git grep -n`.
- READS: independent reads, searches and file views go in ONE turn as parallel tool calls; read a long file once, in
  the range you need. A request, not a gate: never skip a read you need.
