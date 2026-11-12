#!/bin/bash
# wt.sh — worktree hygiene for a /run-implement orchestrator. Run it from the orchestrator's worktree.
#   wt.sh clean <grep -E pattern>     remove every worktree whose path matches (never this one), prune, show disk
#   wt.sh unpushed <grep -E pattern>  per matching worktree: branch, dirty count, commits ahead of origin
#   wt.sh dirty <grep -E pattern>     per matching worktree: the modified tracked files (look before you clean)
# Prints one line per worktree, so the orchestrator never reads `git worktree list` raw.
cmd=$1 pat=$2
[ -n "$pat" ] || { echo "usage: wt.sh clean|unpushed|dirty <pattern>"; exit 2; }
self=$(git rev-parse --show-toplevel) || exit 1
cd "$self" || exit 1
git worktree list --porcelain | sed -n 's/^worktree //p' | grep -E -- "$pat" | while IFS= read -r p; do
  [ "$p" = "$self" ] && continue
  case $cmd in
    clean)
      git worktree unlock "$p" 2>/dev/null
      git worktree remove --force "$p" && echo "removed $p" || echo "FAILED $p" ;;
    unpushed)
      ( cd "$p" || exit
        b=$(git branch --show-current)
        d=$(git status --porcelain --untracked-files=no | wc -l)
        a=-; [ -n "$b" ] && a=$(git rev-list --count "origin/$b..HEAD" 2>/dev/null || echo "?")
        echo "$(basename "$p") branch=${b:-detached} dirty=$d ahead=$a head=$(git rev-parse --short=8 HEAD)" ) ;;
    dirty)
      ( cd "$p" || exit; echo "== $(basename "$p")"; git status --porcelain --untracked-files=no; git diff --stat | tail -1 ) ;;
    *) echo "usage: wt.sh clean|unpushed|dirty <pattern>"; exit 2 ;;
  esac
done
[ "$cmd" = clean ] && { git worktree prune; df -h / | tail -1; }
exit 0
