#!/usr/bin/env python3
"""
cost_report.py - real-dollar Claude spend for a time window, from session transcripts.

Used by /run-implement for the Cost line of its report (copied from the shared
run-workflow skill). Token counts are ground truth
(read from the transcripts); pricing is the editable table below (list prices,
fetched 2026-09-23 from platform.claude.com/docs/en/about-claude/pricing).

WHY a transcript scan: a Workflow script cannot meter dollars, and subagent /
workflow-agent spend is NOT in the main session file - it lands in nested
<project>/<session>/subagents/ and .../subagents/workflows/wf_*/ dirs, plus
worktree agents get their own *--claude-worktrees-* project dir. Scanning the
whole projects tree by TIME WINDOW (pruned by file mtime) captures all of it.

Usage:
  cost_report.py --since <ISO|epoch> [--until <ISO|epoch>]
                 [--root ~/.claude/projects] [--match <substr>] [--fast] [--json]

  --match <substr>  only count transcript files whose path contains <substr>
                    (pass your repo's project-folder slug to scope to one repo,
                    incl. its --claude-worktrees-* dirs; omit = all projects).
  --fast            apply Opus fast-mode rates (2x) - pass if /fast was on.

CAVEATS (print-and-accept, don't hide): the window attributes ALL Claude spend in
the interval to this run - don't run other Claude work concurrently, or use
--match to scope. The Anthropic Console is the billing ground truth; this is a
close, consistent estimate ideal for A/B comparison.
"""
import argparse, json, os, sys
from datetime import datetime, timezone

# (base_input, output) $/MTok. Cache derived via multipliers below.
# Substring match, first hit wins: keep "opus-5-5" above "opus-5".
TIERS = [
    ("fable-5-2", (8.0, 40.0)),
    ("opus-5-5", (4.0, 20.0)),
    ("opus-5", (5.0, 25.0)),
    ("opus-4-8", (5.0, 25.0)), ("opus-4-7", (5.0, 25.0)),
    ("opus-4-6", (5.0, 25.0)), ("opus-4-5", (5.0, 25.0)),
    ("opus-4-1", (15.0, 75.0)), ("opus-4", (15.0, 75.0)),
    ("sonnet-5", (2.0, 10.0)),
    ("sonnet-4-6", (3.0, 15.0)), ("sonnet-4-5", (3.0, 15.0)), ("sonnet-4", (3.0, 15.0)),
    ("haiku-4-5", (1.0, 5.0)), ("haiku-3-5", (0.8, 4.0)),
]
FAST_OPUS = {"opus-5-5", "opus-5", "opus-4-8"}  # 2x base; the only models billed at fast rates
CW5, CW1H, CR = 1.25, 2.0, 0.1  # cache write 5m / write 1h / read, x base input
CR_OVERRIDE = {"opus-5-5": 0.05, "fable-5-2": 0.025}  # cache-read multiplier where it isn't CR
M = 1_000_000


def rates(model, fast):
    for key, (bi, bo) in TIERS:
        if key in (model or ""):
            if fast and key in FAST_OPUS:
                bi, bo = bi * 2, bo * 2
            return bi, bo, CR_OVERRIDE.get(key, CR)
    return None


def _writes(u):
    cc = u.get("cache_creation") or {}
    cw5 = cc.get("ephemeral_5m_input_tokens", 0)
    cw1h = cc.get("ephemeral_1h_input_tokens", 0)
    if not cw5 and not cw1h:  # no breakdown: treat lump as 1h (Claude Code default)
        cw1h = u.get("cache_creation_input_tokens", 0)
    return cw5, cw1h


def line_cost(u, model, fast):
    """One usage line's list-price dollars, or None for a model not in TIERS. wf.py cost imports it: one price rule."""
    r = rates(model, fast)
    if r is None:
        return None
    bi, bo, cr = r
    cw5, cw1h = _writes(u)
    ui = u.get("input_tokens", 0)
    uo = u.get("output_tokens", 0)
    ucr = u.get("cache_read_input_tokens", 0)
    geo = 1.1 if u.get("inference_geo") == "us" else 1.0  # US data-residency premium
    return geo * (ui * bi + uo * bo + ucr * bi * cr
            + cw5 * bi * CW5 + cw1h * bi * CW1H) / M


def to_epoch(s):
    try:
        v = float(s)
        if v > 1_000_000_000:
            return v
    except (ValueError, TypeError):
        pass
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def ts_epoch(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--match", default=None)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    since = to_epoch(a.since)
    until = to_epoch(a.until) if a.until else datetime.now(timezone.utc).timestamp()

    agg = {}      # model -> {in,out,cr,cw5,cw1h,cost,msgs}
    unknown = {}  # model -> count
    seen = set()
    files = scanned = 0
    for dirpath, _dirs, fnames in os.walk(a.root):
        for fn in fnames:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, fn)
            if a.match and a.match not in p:
                continue
            try:
                if os.path.getmtime(p) < since - 1:  # file untouched before window => skip
                    continue
            except OSError:
                continue
            files += 1
            try:
                fh = open(p, encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    msg = o.get("message") or {}
                    u = msg.get("usage")
                    if not u:
                        continue
                    te = ts_epoch(o.get("timestamp"))
                    if te is None or te < since or te > until:
                        continue
                    key = o.get("requestId") or o.get("uuid")
                    if key:
                        if key in seen:
                            continue
                        seen.add(key)
                    model = msg.get("model") or "?"
                    cost = line_cost(u, model, a.fast)
                    if cost is None:
                        unknown[model] = unknown.get(model, 0) + 1
                        continue
                    cw5, cw1h = _writes(u)
                    ui = u.get("input_tokens", 0)
                    uo = u.get("output_tokens", 0)
                    ucr = u.get("cache_read_input_tokens", 0)
                    d = agg.setdefault(model, dict(in_=0, out=0, cr=0, cw5=0, cw1h=0, cost=0.0, msgs=0))
                    d["in_"] += ui; d["out"] += uo; d["cr"] += ucr
                    d["cw5"] += cw5; d["cw1h"] += cw1h; d["cost"] += cost; d["msgs"] += 1
                    scanned += 1

    total = sum(d["cost"] for d in agg.values())
    win = f"{datetime.fromtimestamp(since, timezone.utc).isoformat()} .. {datetime.fromtimestamp(until, timezone.utc).isoformat()}"

    if a.json:
        print(json.dumps({"window": win, "fast": a.fast, "total_usd": round(total, 4),
                          "by_model": {m: {**{k: d[k] for k in ("in_", "out", "cr", "cw5", "cw1h", "msgs")},
                                           "cost_usd": round(d["cost"], 4)} for m, d in agg.items()},
                          "files_scanned": files, "messages": scanned, "unknown_models": unknown}))
        return

    print(f"\nClaude spend  |  window {win}  |  fast={a.fast}")
    print(f"files scanned: {files}   billed messages: {scanned}")
    print("-" * 72)
    print(f"{'model':22} {'msgs':>5} {'in':>9} {'out':>9} {'cacheRd':>9} {'cacheWr':>9} {'$':>9}")
    for m, d in sorted(agg.items(), key=lambda x: -x[1]["cost"]):
        print(f"{m:22} {d['msgs']:>5} {d['in_']:>9} {d['out']:>9} {d['cr']:>9} "
              f"{d['cw5']+d['cw1h']:>9} {d['cost']:>9.4f}")
    print("-" * 72)
    print(f"{'TOTAL':22} {scanned:>5} {'':>9} {'':>9} {'':>9} {'':>9} {total:>9.4f}  USD")
    if unknown:
        print(f"\n! unknown models (not priced, skipped): {unknown}  -> add to TIERS")
    print("\nNote: estimate from transcripts at list prices. Ground truth = Anthropic Console.")


if __name__ == "__main__":
    main()
