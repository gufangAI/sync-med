#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-source guard: turn "one implementation only" from a code comment into
a machine gate.

Why this exists (2026-08-31): _ai.py's header has said for months "parsing and
gateway logic must live in one place; new scripts import it." It stayed a
comment, so ask()/d1()/parse_json() got copy-pasted into herb_factory, reviewer,
tcm_judge... and one copy dropped the X-Gateway-Key header, which is what cost
the content line 22 days of zero output. A comment cannot stop the next paste;
CI can.

Design: this does NOT try to force the existing sprawl to zero in one shot --
that would red the whole repo. It freezes today's known copies as a baseline
allowlist and fails only when a NEW copy appears (a file not on the list grows a
`def ask(`) or a NEW hardcoded internal endpoint shows up outside the config
files. The baseline is meant to shrink over time (delete a copy -> remove its
allowlist entry), never grow.

Exit 0 = clean; exit 1 = a new duplicate or new hardcoded endpoint (prints the
offending file:line). Runs on push/PR via guard-single-source.yml, never on cron.
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = "scripts"

# --- baseline: files ALLOWED to define these today (the sprawl we inherited) ---
# _ai.py is the single source of truth; every other entry is a known copy that
# should eventually be deleted and removed from this list. Do not add to it.
ALLOW = {
    "ask": {"_ai.py", "herb_factory.py", "tcm_judge.py", "fangji_ai_refine.py"},
    "d1": {"_ai.py", "herb_factory.py", "book_health_check.py", "evolve_controller.py",
           "fangji_ai_refine.py", "intake_sentry.py", "nvidia_probe.py"},
    "parse_json": {"_ai.py", "herb_factory.py"},
    "parse_json_array": {"_ai.py", "herb_factory.py"},
}
# Files allowed to hold hardcoded endpoints (config-ish or transport layers).
# Everything else must read the URL from env / import it.
ENDPOINT_ALLOW = {
    "_ai.py",
    "_diag_r2_manifests.py",
    "book_health_check.py",
    "check_frontend.py",
    "command_center.py",
    "daily_report_cloud.py",
    "daily_report_v3.py",
    "direct_fleet.py",
    "evolve_controller.py",
    "fangji_ai_refine.py",
    "fleet_watch.py",
    "herb_factory.py",
    "intake_sentry.py",
    "llm_roster.py",
    "local_github_consistency_audit.py",
    "nvidia_probe.py",
    "platform_report.py",
    "probes.py",
    "self_audit.py",
    "tcm_judge.py",
    "team_report_audit.py",
    "workflow_sentry.py",
}
ENDPOINT_PAT = re.compile(
    r'[\"\']https?://[^\"\']*(gufangai\.com/api/gateway|maas-api|'
    r'api\.cloudflare\.com|opencode\.ai|xf-yun\.com|integrate\.api\.nvidia|bigmodel\.cn)')


def scan():
    dup_viol, ep_viol = [], []
    for dp, dns, fns in os.walk(ROOT):
        if "__pycache__" in dp:
            continue
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dp, fn)
            base = fn
            try:
                lines = io.open(path, encoding="utf-8", errors="replace").read().split("\n")
            except Exception:                                    # noqa: BLE001
                continue
            for i, line in enumerate(lines, 1):
                for name, allowed in ALLOW.items():
                    if re.match(r"^def %s\(" % name, line) and base not in allowed:
                        dup_viol.append((path, i, "def %s(" % name))
                if base not in ENDPOINT_ALLOW and ENDPOINT_PAT.search(line):
                    ep_viol.append((path, i, line.strip()[:70]))
    return dup_viol, ep_viol


def main():
    dup, ep = scan()
    if not dup and not ep:
        print("single-source guard: clean (no new duplicate impls, no new hardcoded endpoints)")
        return 0
    if dup:
        print("NEW duplicate implementation(s) -- import from _ai instead of copying:")
        for p, ln, what in dup:
            print("  %s:%d  %s" % (p.replace("\\", "/"), ln, what))
    if ep:
        print("NEW hardcoded internal endpoint(s) -- read from env / import the base URL:")
        for p, ln, what in ep:
            print("  %s:%d  %s" % (p.replace("\\", "/"), ln, what))
    print("\nThis gate freezes today's known copies; it fails only on NEW ones.")
    print("Fix a real copy by importing _ai and removing its allowlist entry in guard_single_source.py.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
