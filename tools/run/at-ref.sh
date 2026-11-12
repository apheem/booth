#!/bin/bash
# at-ref.sh — look at a file at a git ref without reading it whole (the orchestrator's context pays for every line).
#   at-ref.sh grep <ref> <file> <grep -E pattern> [max=12]   numbered matching lines
#   at-ref.sh has <ref> <file>:<pattern> ...                  silent-drop guard: every pattern must occur; exit 1 on a MISS
case $1 in
  grep)
    git show "$2:$3" | grep -nE -- "$4" | head -"${5:-12}" | cut -c1-220 ;;
  has)
    ref=$2; shift 2; miss=0
    for fp in "$@"; do
      f=${fp%%:*}; p=${fp#*:}
      n=$(git show "$ref:$f" 2>/dev/null | grep -c -- "$p")
      if [ "$n" -gt 0 ]; then echo "ok   $n  $f :: $p"; else echo "MISS    $f :: $p"; miss=1; fi
    done
    exit $miss ;;
  *) echo "usage: at-ref.sh grep <ref> <file> <pattern> [max] | has <ref> <file>:<pattern> ..."; exit 2 ;;
esac
