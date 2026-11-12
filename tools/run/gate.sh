#!/bin/bash
# gate.sh — Booth's one gate. Takes seconds, so it runs in the foreground (no queue, no start/wait).
#   bash tools/run/gate.sh [<tree>]    the tree defaults to the cwd: your worktree root
# Stages, each through sandbox.sh (no display, no session bus, no PipeWire, throwaway HOME/XDG):
#   compile   every tracked or new *.py byte-compiles
#   unit      python3 -m unittest discover -s tests -t .   (the whole suite; zero tests ran = FAIL)
#   mutate    tools/run/mutate.py --self-check              (the mutation runner still tells KILLED from SURVIVED)
# Prints `<stage> PASS|FAIL`, the failing test headers under a FAIL, `GATE EXIT 0|1`, and the full log's path.
# The log lives in ~/.cache/booth-gate/, never in the tree.
cd "${1:-.}" || { echo "GATE EXIT 2"; exit 2; }
[ -f tools/run/sandbox.sh ] || { echo "gate: no tools/run/sandbox.sh in $PWD"; echo "GATE EXIT 2"; exit 2; }
logs=$HOME/.cache/booth-gate; mkdir -p "$logs"
log=$(mktemp "$logs/$(date +%m%d-%H%M%S)-XXXX.log")
t0=$SECONDS rc=0
stage() {   # stage <label> <command...>
  local label=$1; shift
  echo "=== $label: $*" >> "$log"
  if bash tools/run/sandbox.sh "$@" >> "$log" 2>&1; then echo "$label PASS"; return; fi
  echo "$label FAIL"; rc=1
  sed -n "/^=== $label:/,\$p" "$log" | grep -aE '^(FAIL|ERROR):|^Ran [0-9]+ test|^(FAILED|NO TESTS RAN)|Error|^  File "|^SELF-CHECK' \
    | head -20 | sed 's/^/            /' | cut -c1-200
}
mapfile -t py < <(git ls-files -co --exclude-standard -- '*.py')
if [ ${#py[@]} -gt 0 ]; then stage compile python3 -m py_compile "${py[@]}"; else echo "compile PASS (no python files)"; fi
if [ -d tests ]; then stage unit python3 -m unittest discover -s tests -t .
else echo "unit FAIL"; echo "            no tests/ folder (issue #3 creates it)"; rc=1; fi
stage mutate python3 tools/run/mutate.py --self-check
echo "GATE EXIT $rc  ($((SECONDS - t0))s, head $(git rev-parse --short=8 HEAD 2>/dev/null))"
echo "full log: $log"
exit $rc
