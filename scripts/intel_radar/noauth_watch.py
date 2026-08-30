#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly watch on OmniRoute's no-auth provider registry.

Why this exists (founder, 2026-08-31): free model sources should come from
public no-auth endpoints -- other people's accounts, not our own keys. The
best-maintained list of such endpoints is OmniRoute's noauth.ts (58k-star
repo, the very project our nova-gateway was modelled on). Instead of
re-scanning the internet ourselves, we diff their registry weekly and only
speak up when it changes.

Cadence is weekly by founder's explicit correction ("weekly, not monthly", upgrading
my original monthly suggestion), wired into the Eagle Weekly Hunter run.

Output contract:
  exit 0  -> no change (stay silent; the weekly issue stays clean)
  exit 10 -> registry changed; noauth_watch.md written with the diff, and
             noauth_snapshot.json updated (workflow commits it back)
  exit 1  -> fetch/parse failure (the workflow surfaces it -- a watch that
             dies silently is a watch nobody has)
"""
import base64
import io
import json
import os
import re
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "noauth_snapshot.json")
SRC = ("https://api.github.com/repos/diegosouzapw/OmniRoute/contents/"
       "src/shared/constants/providers/noauth.ts")


def fetch_registry():
    req = urllib.request.Request(SRC, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "IntelRadar/3.0 (SueAI)",
        **({"Authorization": "Bearer " + os.environ["GH_TOKEN"]}
           if os.environ.get("GH_TOKEN") else {}),
    })
    blob = json.loads(urllib.request.urlopen(req, timeout=45).read())
    return base64.b64decode(blob["content"]).decode("utf-8", "replace")


def parse(ts):
    """id -> {website, noAuth, hasFree, note}. Regex over TS object literals is
    fine here: the file is a hand-written constant, and if its shape drifts
    enough to break this, exit 1 makes that loud instead of silently empty."""
    out = {}
    for m in re.finditer(r'id:\s*"([^"]+)"', ts):
        pid = m.group(1)
        seg = ts[m.start():m.start() + 1200]
        w = re.search(r'website:\s*"([^"]+)"', seg)
        f = re.search(r'freeNote:\s*\n?\s*"([^"]+)"', seg)
        a = re.search(r'authHint:\s*\n?\s*"([^"]+)"', seg)
        out[pid] = {
            "website": w.group(1) if w else "",
            "noAuth": '"noAuth": true' in seg or "noAuth: true" in seg,
            "hasFree": "hasFree: true" in seg,
            "note": (f.group(1) if f else (a.group(1) if a else ""))[:160],
        }
    return out


def main():
    try:
        cur = parse(fetch_registry())
    except Exception as exc:                                     # noqa: BLE001
        print("noauth_watch: fetch/parse FAILED: %s" % str(exc)[:120])
        return 1
    if not cur:
        print("noauth_watch: parsed 0 providers -- upstream shape changed, treat as failure")
        return 1

    old = {}
    if os.path.isfile(SNAP):
        old = json.load(io.open(SNAP, encoding="utf-8"))

    added = sorted(set(cur) - set(old))
    removed = sorted(set(old) - set(cur))
    changed = sorted(k for k in set(cur) & set(old) if cur[k] != old[k])

    print("noauth_watch: %d providers upstream, %d in snapshot" % (len(cur), len(old)))
    if not (added or removed or changed):
        print("noauth_watch: no change -- staying silent")
        return 0

    lines = ["## OmniRoute no-auth registry changed this week", ""]
    for pid in added:
        lines.append("- **NEW** `%s` %s — %s" % (pid, cur[pid]["website"], cur[pid]["note"]))
    for pid in removed:
        lines.append("- **GONE** `%s` (was: %s)" % (pid, old[pid].get("website", "")))
    for pid in changed:
        lines.append("- **CHANGED** `%s` — %s" % (pid, cur[pid]["note"]))
    lines += ["",
              "Free-source doctrine: use their public endpoints, never our own account keys.",
              "New entries go through intel-triage before touching any lane or gateway seed."]
    io.open(os.path.join(os.getcwd(), "noauth_watch.md"), "w",
            encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    io.open(SNAP, "w", encoding="utf-8", newline="\n").write(
        json.dumps(cur, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print("\n".join(lines))
    return 10


if __name__ == "__main__":
    sys.exit(main())
