#!/usr/bin/env python3
"""issues.py — issue edits a /run-implement orchestrator makes, without reading the bodies into its context.

  issues.py amend <issue> <adj tag> <substring> [...]   strike every body line containing a substring and tag it
                                                       "Amended by Orchestrator adjudication <tag>" (Phase 2 step 2)
  issues.py hygiene [--fix] <issue> ...                 state + labels; --fix removes ready-for-agent from closed ones

Post the adjudication itself with `gh issue comment <issue> -F "$R/adj/<issue>.md"`.
"""
import datetime, json, subprocess, sys, tempfile


def gh_json(n, fields):
    return json.loads(subprocess.check_output(["gh", "issue", "view", str(n), "--json", fields]))


def amend(n, tag, subs):
    out, hits = [], 0
    for line in gh_json(n, "body")["body"].split("\n"):
        if any(s in line for s in subs) and "Orchestrator adjudication" not in line:
            lead, rest, prefix = line[: len(line) - len(line.lstrip())], line.lstrip(), ""
            for p in ("- [ ] ", "- [x] ", "- "):
                if rest.startswith(p):
                    prefix, rest = p, rest[len(p):]
                    break
            line = f"{lead}{prefix}~~{rest}~~ **Amended by Orchestrator adjudication {tag} ({datetime.date.today()}): see that comment.**"
            hits += 1
        out.append(line)
    if not hits:
        sys.exit(f"#{n}: no line matched {subs}")
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as f:
        f.write("\n".join(out))
    subprocess.check_call(["gh", "issue", "edit", str(n), "--body-file", f.name], stdout=subprocess.DEVNULL)
    print(f"#{n}: amended {hits} line(s) for {tag}")


def hygiene(nums, fix):
    for n in nums:
        j = gh_json(n, "number,state,labels")
        labels = [l["name"] for l in j["labels"]]
        print(n, j["state"], labels)
        if fix and j["state"] == "CLOSED" and "ready-for-agent" in labels:
            subprocess.check_call(["gh", "issue", "edit", str(n), "--remove-label", "ready-for-agent"], stdout=subprocess.DEVNULL)
            print("   removed ready-for-agent")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "amend":
        amend(a[1], a[2], a[3:])
    elif a and a[0] == "hygiene":
        hygiene([x for x in a[1:] if x != "--fix"], "--fix" in a)
    else:
        sys.exit(__doc__)
