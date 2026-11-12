---
name: run-implement
description: Booth's own issue executor. Implements ONE ready-for-agent GitHub issue end to end, or a short chain of 2–4 that must go in order, strictly one at a time - recon, a worktree-isolated implementer Agent, one combined audit with real mutations, one patch + confirm, a skeptic on hardware / privacy / trust issues, then an authorized merge to main, a reinstall into the home folder and a hardware checklist for the user. Plain Agent subagents, no Workflow script, no waves. Triggers on "/run-implement", "implement issue N", "implement this issue", "implement these few issues in order", "run the agent executor on this issue", "implement the next ready issue". Use after /grill-me, /to-prd, /to-issues and /triage.
---

# run-implement (Booth)

Take one triaged issue (or a short chain), build it, prove it, merge it, install it. **One issue at a time**: one
implementer, one auditor, at most one patch round, and a skeptic only where hardware, the user's voice or loaded code
is at stake. Booth is a GNOME desktop app on the user's real desk (BRIO, Scarlett Solo, PRO X): there is no server and
no deploy, so "live" means installed into the home folder and checked on the devices by the user.

The shared tools are in `tools/run/`, and the fixed agent rules in `tools/run/blocks.md`: **point agents at a block,
never retype one.** The repo's CLAUDE.md rules (issue #3 writes them) sit on top; the stricter wins.

More than 4 issues → do them as successive runs of at most 4, in dependency order. Never two implementers at once:
they would share the same live devices.

## Keep your own context lean

Every call re-reads your whole conversation. The rules that save the most:

- **Never `cat` / `sed -n` / `git show` a source file to check one thing.** `git grep -n` (there is no `rg` on this
  box) or `tools/run/at-ref.sh grep <ref> <file> <pattern>`.
- **`gh issue view` once per issue, into a file; never `gh pr diff`.** Agents read diffs in their own context.
- **A result saved to `tool-results/*.txt` because it was too big is never `Read` whole.** Grep it.
- **Agents return ≤15 lines**; anything longer goes to a file they name.
- **Batch independent reads** into one turn of parallel calls.
- **Every Agent gets a role-prefixed `description`**: `recon:`, `impl:`, `audit:`, `hand-patch:`, `confirm:`,
  `skeptic:`, `fix-skeptic:`, `final:`.
- **Wait for the Agent's completion notice.** No heartbeat files, no `sleep`, no polling.
- **Compress your own narration, never an agent's brief.** A confused agent flails and costs more than you saved.

## Phase 1: Bootstrap

```bash
date -u +%FT%TZ > "${TMPDIR:-/tmp}/run-implement-<tag>-start.txt"    # <tag> = the issue #, or a chain's first
git fetch origin
R="${TMPDIR:-/tmp}/run-implement-<tag>"; mkdir -p "$R"/{bodies,recon,adj}
for n in <issue#>…; do gh issue view $n --json body -q .body > "$R/bodies/$n.md"; done
grep -nE 'gate\.sh|unittest discover|run the (whole|full) suite' "$R"/bodies/*.md    # must print nothing
grep -nA3 '^## Blocked by' "$R"/bodies/*.md
```

- **Blocked by an open issue** (including a `[Human]` prerequisite like #2 with unchecked boxes): stop and tell the
  user which one. Never build on top of a missing slice.
- **A body with a gate line** (the first grep printed something): strip it with `gh issue edit <n> --body-file …` and
  re-save `bodies/<n>.md`. An AC says what to prove, never how to gate; `blocks.md` `## GATE` is the only gate
  instruction an agent gets.
- Read the PRD (#1) section for the modules the issue names, and the issue's parent if any — never the whole PRD.
- **Before #3 lands** the repo has no app, no `tests/` and no CLAUDE.md: the gate is red by design (`unit FAIL — no
  tests/ folder`) until #3's PR makes it green, and Phase 5's install step is skipped until the install script exists.
- **A chain** gets its own base: `git push origin origin/main:refs/heads/integration-<tag>`. Each slice merges there
  before the next is briefed, and `main` gets one merge at the end. A single issue's base is `main`.
- **Ask once, in plain text** (never the pop-up): *"Once the gate is green and the review ships, may I merge to main
  and reinstall Booth into your home folder? Reinstalling restarts Booth's background service, so Booth Mic may drop
  for a moment — not during a call."* A yes is this run's authorization. No answer → you stop and ask at Phase 5.

You stay in the shared checkout, on `main`, and never edit code there. A one-line fix goes into the slice's own
worktree (`EnterWorktree({path: <its worktree>})`), committed, pushed and re-gated like any other.

## Phase 2: Recon, adjudicate, brief

**Recon against HEAD before briefing — never brief from the issue body alone.** For a small issue, your own few
`git grep -n` give every brief confirmed `file:line` anchors. For a bigger one (more than ~3 files, or a body you
doubt) spawn one read-only `recon:` agent: it writes the full report (exact symbols with `file:line` and signatures,
the real file set, the fixtures that exist and the ones the slice must capture, risks, every false claim in the body)
to `$R/recon/<n>.md` and returns ≤15 lines: `VERDICT: body OK | body has false claims`, one line per false claim,
one per AC that needs an adjudication.

**Adjudicate in the issue, never only in code.** When the slice must deviate from a written AC (a control the BRIO
doesn't expose, a HID++ reply that differs from the PRD): write `$R/adj/<n>.md` whose first line is exactly
`Orchestrator adjudication` (the AC it overrides, the ruling, one line why), post it with
`gh issue comment <n> -F "$R/adj/<n>.md"`, then strike the AC line with
`tools/run/issues.py amend <n> <tag> "<substring of the AC line>"`. Every agent reads the issue with `--comments` and
treats that comment as superseding. An adjudication that lives only in a patch gets reverted by the next audit.

**Disown stale sources by name.** If the PRD or a linked issue is wrong about the code or a device, the brief says so:
"#X is stale about Y — do not follow it."

**Mark the skeptic kinds** (none for a pure-UI or parser-only issue; several can apply):
- `hardware` — writes to a device or the audio graph: camera controls and extension units, Scarlett mixer values
  (48V!), HID++ requests, hardware volume, default sink or source.
- `privacy` — decides who hears or sees the user: mute, Booth Mic / Booth Stereo routing, fallback, monitoring,
  the camera preview.
- `trust` — loads or writes code or files: plugin fetch and checksum, the install script, systemd units, the
  settings store.

### The implementer brief

`Agent({ description: "impl: #<N>", isolation: "worktree", prompt: … })`. Keep it FULL:

```
Implement GitHub issue #<N> of Booth. Read it with `gh issue view <N> --comments`: a comment whose first line is
exactly "Orchestrator adjudication" SUPERSEDES the acceptance criterion it names.

YOUR RULES are in tools/run/blocks.md. Read these sections first and follow them verbatim:
## TDD, ## GATE, ## HEADLESS, ## FORBIDDEN (with {MAIN} = <shared checkout path>), ## REPO FACTS.

EXACT SYMBOLS (recon against HEAD — don't re-discover them): <file:line + signatures, or "read <$R/recon/<N>.md>">
PRD: issue #1, section(s) <the module names this issue implements> — read only those.
STALE SOURCES: <"#X is wrong about Y — do not follow it", or none>
CONFINE: <only what a chain sibling owns; never a file this issue needs to work>
LIVE DEVICES: <the read-only probes this slice may run to capture fixtures (e.g. `v4l2-ctl -d <color node>
--list-ctrls-menus`), and any live write it may make with read-first-and-restore, or "none: fixtures only">

WORKTREE: you are in a fresh isolated worktree. Branch from the base:
  git fetch origin && git switch -c implement/issue-<N>-<slug> origin/<base>
<chain only: then verify <a symbol the previous slice added> is present; if it is missing STOP and return failed —
never rebuild it.>

DONE WHEN: red-first evidence in the PR body (## TDD); the gate green on your last pushed commit, its summary lines in
a PR comment (## GATE); a PR against <base> whose body says "Closes #<N>" (body via --body-file outside your worktree)
and lists the hardware checks the user must run for this slice.
RETURN ≤15 lines: success|failed, PR number, branch, pushed head sha, the GATE EXIT line, every live write made and
restored, every file touched outside the issue's region with why, and the error if failed.
```

`failed` → read the error, fix the root cause (a wrong symbol in the brief, a missing adjudication, a fixture that
can't be captured), re-brief once.

## Phase 3: Review — one auditor, then one patch round

### The audit

One `audit:` agent, `isolation: "worktree"`, read-only on the code and the devices:

```
Audit PR #<P> (branch <branch>, base <base>) for Booth issue #<N>. Read the issue with `gh issue view <N> --comments`
(an "Orchestrator adjudication" comment supersedes the AC it names).
YOUR RULES: tools/run/blocks.md ## STANDING, ## FORBIDDEN, ## MUTATION and ## REPO FACTS, verbatim.
SETUP: in your own worktree: git fetch origin && git checkout --detach origin/<branch>. Record audited_head first.
Read the change with `git diff origin/<base>...HEAD` (never gh pr diff).
THREE JOBS:
1. CORRECTNESS & BINDING — trace each AC's real flow end to end, on every branch it names: the right device resolved
   by model on every use (never the IR node, never a cached card number or node id), parsed values vs the fixture,
   unwired code (zero non-test callers), a GLib main-loop callback that blocks or never fires, a subprocess whose
   failure is swallowed, state that isn't saved or re-applied, hotplug and unplug paths.
2. TEST TEETH — for each behaviour AC, find the test that claims it and mutate the production line through
   tools/run/mutate.py, one run for every mutant. Each SURVIVED line is hollow_test:true; an AC with no test is a
   finding. Fixtures: real captured shape, fake serial only.
3. COMPLETENESS & SCOPE — every AC (as adjudicated) → its landing file:line, or missing/partial. Then
   `gh pr view <P> --json files`: out-of-region edits, a new dependency, a committed serial / frame / binary,
   machinery no AC asked for.
<add when it applies — HOTPLUG RACE: what happens if the device is unplugged or replugged while a read or write is in
flight, or flaps ten times fast; what does the page say, what state is left? ROUTING: walk the routing policy's
state machine for every event order the issue names, and check mute after each.>
RETURN ≤15 lines: audited_head, verdict clean|needs_fixes, then at most 12 findings, one line each: severity,
latent_risk, hollow_test, file:line, the input that breaks it and the proof (the mutant line or the code path).
```

**Must-fix** = every BLOCKER, every `latent_risk:true` MINOR, and **every hollow test** (a test that stays green
under its mutation is a defect, never a nit). The rest are deferred MINORs, and **deferred is not safe-to-ship**:
read each one. One pointing at a file this slice refactored is a likely regression; fix it now or file it
`needs-triage`, never drop it silently.

### Patch and confirm (only if there is a must-fix)

One `hand-patch:` agent, `isolation: "worktree"`:

```
Fix these audit findings on PR #<P> (branch <branch>, base <base>) for Booth issue #<N>.
YOUR RULES: tools/run/blocks.md ## GATE, ## HEADLESS, ## FORBIDDEN ({MAIN} = <path>), ## MUTATION, ## REPO FACTS,
verbatim.
SETUP: git fetch origin && git checkout --detach origin/<branch>; commit there and `git push origin HEAD:<branch>`
(the implementer's worktree still holds the branch — never touch it).
Fix BLOCKERs and latent risks for real. Fix a hollow test by making it bite — its mutant re-run through the runner
until it reads KILLED — never by deleting or weakening it. Show the red for every new test. A fix exists only when
pushed: check it is in origin/<branch> before you report it. If you believe one finding should not be fixed, leave
that one and say why.
RETURN ≤15 lines: per finding fixed|not fixed (why), the pushed sha, the GATE EXIT line, every live write made and
restored.
FINDINGS: <the must-fix lines as JSON>
```

Then one `confirm:` agent (the audit's setup and rules) that asks **both halves**: *did each finding actually land
at the new HEAD, AND did these fixes introduce any regression?* It reads the changed regions, not the whole slice,
and re-runs each hollow test's mutant: `KILLED` = landed. A confirm that only asks "did they land" is how a patch
that silently disarms a guard (a mute that no longer survives a fallback) ships green.

**Stop rule: two fix attempts per finding, never a third.** If the second still fails its confirm, revert that
finding's fixes to the audited tree, file it `needs-triage` with both attempts and why each failed, and ship the
rest. A finding that is really a product or device call (the headset firmware ignores sidetone) goes to the user,
not to another patch.

### The skeptic (only for marked kinds)

Once the review is clean: one `skeptic:<kind>` agent per marked kind, in parallel, each `isolation: "worktree"`,
briefed: *Booth issue #N, PR #P, branch, base, your kind is <kind>; your rules are tools/run/blocks.md ## SKEPTIC,
## FORBIDDEN and ## MUTATION verbatim.* SHIP → go on. NO-SHIP → one `hand-patch:` with every NO-SHIP kind's findings
(the patch brief above), then one `fix-skeptic:<kind>` on `git diff <its audited_head>..origin/<branch>`. The stop rule
counts these attempts too. On a hardware slice treat a finding as real until it is disproven: a wrong write lands on
the user's real mic, camera or ears.

### The hardware checklist (every slice that touches a device, routing or a page)

Unit tests are blind to the look, the sound and the devices. Comment this slice's checks on the `[Human]` verify issue
(#23) — concrete steps in the PRD's style ("unplug the Scarlett: the sidebar says Booth Mic · from PRO X mic within
2 s") — and say plainly in the report that they have not happened yet. The implementer never grades its own look.

## Phase 4: Chain only — fold the slice in, then the next

Merge the slice's PR into `integration-<tag>` (no ask: it installs nothing) with `gh pr merge <P> --squash
--delete-branch`, then brief the next slice against what **actually landed**: `git grep -n` its symbols at
`origin/integration-<tag>` and put them in the next brief's EXACT SYMBOLS. Never brief from your guess.

After the last slice, one `final:` agent (read-only, its own worktree on `origin/integration-<tag>`) does what no
per-slice audit can see. It walks one real device journey through every slice (camera replug → settings re-applied;
Scarlett unplug → fallback → mute still on → Scarlett back), naming `file:line` at both ends of every seam. It checks
the contracts between slices (the devices record, the routing actions, the settings schema version) and maps every AC
of every issue to where it landed. Its findings go through the same patch → confirm → stop rule.

## Phase 5: Gate, merge, install

**Gate the final tree once, yourself:**

```bash
git worktree add --detach .claude/worktrees/run-implement-<tag>-gate origin/<branch | integration-<tag>>
bash tools/run/gate.sh .claude/worktrees/run-implement-<tag>-gate
```

Red → back through a patch (the stop rule still holds). After any fix, move the gate worktree to the new head
(`git -C .claude/worktrees/run-implement-<tag>-gate fetch origin && git -C … checkout --detach origin/<branch>`) and
gate again.

**Merge** on a green gate, a clean review and SHIP from every skeptic, with the bootstrap yes. Without that yes, stop
and ask now, in plain text. Single issue: `gh pr merge <P> --squash --delete-branch`. Chain: open
`integration-<tag> → main` (`Closes #…` for each issue) and merge it. Then run the silent-drop guard in one call:
`tools/run/at-ref.sh has origin/main <file>:<symbol> …` (exit 1 on a MISS). A "clean" GitHub merge can drop a
line without showing a conflict.

**Install** (Booth's deploy; skip, and say so, while the repo has no install script). Merged is not installed. From a
fresh detached worktree at `origin/main` — never the shared checkout or a slice branch, since the script copies files
and a worktree vanishes — run the install command the README names. It needs no sudo and is safe to re-run. Then
check: the launcher is on PATH, the installed app files match `origin/main` (`diff -rq` of the repo's app folder
against the install folder, both named in the README), and, once the background service exists (#7),
`systemctl --user is-active booth.service` reads `active`. A failed install: read its output and fix the root cause
through a patch, never re-run it blindly. Remove that worktree after.

**Hygiene:** close any issue the merge didn't auto-close (with the PR number);
`tools/run/issues.py hygiene --fix <issue#>…`. Then remove this run's worktrees by their ids, never every `agent-*`
(other sessions share that folder): `tools/run/wt.sh clean '<agent ids>|run-implement-<tag>-'`, and `rm -rf "$R"`.
File each real deferred MINOR as a `needs-triage` issue. Pull the shared checkout's `main` (`git pull --ff-only`).

## Report

```
## #<N> shipped (run-implement)
PR #<P> → main <sha> · installed <yes: launcher + files match | skipped: no install script yet>
Review: audit <clean | N must-fix → patch → confirm clean> · hollow tests <n> fixed · skeptic <kinds: SHIP | fixed | —>
Gate: <the GATE EXIT line + stage lines>
Live writes: <each one and that it was restored, or none>
Hardware checklist: <n checks added to #23 — not yet run by you>
Cost: $<x> (cost_report.py)
Filed: <needs-triage issues, or none> · Adjudications: <issue + one line each, or none>
```

Cost, for the line above:

```bash
python3 tools/run/cost_report.py --since "$(cat "${TMPDIR:-/tmp}/run-implement-<tag>-start.txt")" --match booth
```

## Principles

- **One at a time.** One implementer, one auditor, one patch round; the devices are shared, so never two writers.
- **Rules live in `blocks.md`.** Point agents at its sections and never retype one. When a rule changes, it changes there.
- **Recon before briefing, and adjudicate in the issue.** Guessed paths turn into reimplementation and audit churn,
  and a ruling that lives only in code gets reverted by the next audit.
- **A hollow test is a defect.** Mutation through `mutate.py` is the proof; "written first" is not.
- **The confirm asks about regressions**, not just whether the fixes landed.
- **Two fix attempts, never a third.** After that, revert, file it, ship the rest.
- **The desk is live.** Read devices freely, write them rarely and put everything back; 48V never goes on unasked.
- **Green is not "sounds right", and merged is not installed.** The install step shows what is on the machine, and
  the user's hardware checklist shows whether it works.
