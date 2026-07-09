# -*- coding: utf-8 -*-
# Fleet health sentinel: probe gateway providers + end-to-end pengzhuang, write GitHub Issue.
# Runs on GitHub Actions (overseas runner, direct network). Zero provider secrets needed:
# it calls the production gateway health endpoint which holds keys server-side.
import json, os, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SITE = "https://www.gufangai.com"
UA = {"User-Agent": "FleetHealth/1.0", "Content-Type": "application/json"}
REPO = os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med")
TITLE = "\U0001F6A2 fleet-health"  # ship emoji

def fetch(url, body=None, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def probe_health():
    j = fetch(f"{SITE}/api/gateway/health", timeout=90)
    d = j.get("data", j)
    rows = []
    for p in d.get("providers", []):
        name = p.get("name")
        if p.get("ok"):
            rows.append((name, "ok", p.get("cost_ms", 0), ""))
        elif p.get("missing_secret"):
            rows.append((name, "no-key", 0, "missing secret"))
        else:
            err = str(p.get("error", ""))[:60]
            # reasoning-family models return empty content on 1-token probes -> false negative
            if "empty choices/content" in err:
                rows.append((name, "ok*", p.get("cost_ms", 0), "reasoning-family probe artifact"))
            else:
                rows.append((name, "DOWN", p.get("cost_ms", 0), err))
    return rows

def probe_usage():
    try:
        j = fetch(f"{SITE}/api/gateway/usage", timeout=40)
        return j.get("providers", []), j.get("cooling", [])
    except Exception:
        return [], []

def probe_e2e():
    t0 = time.time()
    try:
        j = fetch(f"{SITE}/api/ai/huizhen",
                  {"q": "e2e probe case: chronic fatigue, pale tongue, deep pulse, cold limbs, recurring."},
                  timeout=140)
        el = round(time.time() - t0, 1)
        return ("ok" if j.get("ok") else "FAIL", el, j.get("pair", ""))
    except Exception as e:
        return ("FAIL", round(time.time() - t0, 1), str(e)[:60])

def gh(*args, inp=None):
    return subprocess.run(["gh"] + list(args), capture_output=True,
                          encoding="utf-8", errors="replace", input=inp)

def upsert_issue(body_md, alert):
    q = gh("issue", "list", "-R", REPO, "--search", TITLE, "--state", "open",
           "--json", "number,title", "--limit", "10")
    num = None
    try:
        for it in json.loads(q.stdout or "[]"):
            if TITLE in it.get("title", ""):
                num = it["number"]; break
    except Exception:
        pass
    red, green = "\U0001F534 ALERT", "\U0001F7E2"
    badge = red if alert else green
    title = f"{TITLE} {badge} {time.strftime('%m-%d %H:%M UTC', time.gmtime())}"
    if num:
        gh("issue", "edit", str(num), "-R", REPO, "--title", title, "--body", body_md)
        print(f"issue #{num} updated")
    else:
        r = gh("issue", "create", "-R", REPO, "--title", title, "--body", body_md)
        print("issue created:", (r.stdout or r.stderr)[:120])
        try:
            num = int((r.stdout or "").strip().rsplit("/", 1)[-1])
        except Exception:
            num = None
    # Issue edits do NOT push notifications; a new comment DOES. Comment only on ALERT.
    if alert and num:
        gh("issue", "comment", str(num), "-R", REPO,
           "--body", f"\U0001F534 ALERT {time.strftime('%m-%d %H:%M UTC', time.gmtime())} — check table above.")
        print("alert comment posted (push notification)")

def main():
    rows = []
    try:
        rows = probe_health()
    except Exception as e:
        rows = [("gateway/health", "DOWN", 0, str(e)[:60])]
    e2e_status, e2e_s, e2e_info = probe_e2e()
    usage, cooling = probe_usage()

    down = [r for r in rows if r[1] == "DOWN"]
    core_down = [r for r in down if r[0] in ("modelscope", "sensenova", "cerebras")]
    alert = bool(core_down) or len(down) >= 3 or e2e_status != "ok"

    lines = ["| provider | status | ms | note |", "|---|---|---|---|"]
    for name, st, ms, note in rows:
        icon = {"ok": "✅", "ok*": "✅", "no-key": "\U0001F511", "DOWN": "❌"}.get(st, "?")
        lines.append(f"| {name} | {icon} {st} | {ms} | {note} |")
    lines.append("")
    lines.append(f"**e2e pengzhuang**: {'✅' if e2e_status=='ok' else '❌'} {e2e_status} {e2e_s}s {e2e_info}")
    lines.append("")
    if usage:
        lines.append("**today's load (rotation)** | provider | calls | ok | tokens |")
        lines.append("|---|---|---|---|")
        for u in usage[:12]:
            lines.append(f"| {u.get('provider')} | {u.get('calls')} | {u.get('ok')} | {u.get('tokens')} |")
        lines.append("")
    lines.append(f"- cooling now: {', '.join(c.get('provider') for c in cooling) if cooling else 'none (all active)'}")
    lines.append(f"- providers down: {len(down)} (core down: {len(core_down)})")
    lines.append(f"- rule: ALERT if any core (modelscope/sensenova/cerebras) down, >=3 down, or e2e fail")
    lines.append(f"- ts: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    body = "\n".join(lines)
    print(body)
    upsert_issue(body, alert)
    print(f"ALERT={alert}")

if __name__ == "__main__":
    main()
