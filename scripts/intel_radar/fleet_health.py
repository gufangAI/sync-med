# -*- coding: utf-8 -*-
# Fleet health sentinel: probe gateway providers + end-to-end pengzhuang, write GitHub Issue.
# Runs on GitHub Actions (overseas runner, direct network). Zero provider secrets needed:
# it calls the production gateway health endpoint which holds keys server-side.
import json, os, subprocess, sys, time, urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SITE = "https://www.gufangai.com"
UA = {"User-Agent": "FleetHealth/1.0", "Content-Type": "application/json"}
REPO = os.environ.get("GITHUB_REPOSITORY", "hosonzuo8848/sync-med")
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

# ── 质量探针(2026-08-08 整合队列#6·拆 promptfoo「声明式断言测质量」机制自写)──
#   连通性(probe_health)只答「网关通不通」,答不了「这个时段派来的模型会不会好好干活」。
#   今晚星网三次点不着火,根因全是网关派了占位符模型:judge 该输出真判定,却吐回
#   {"verdict":"...","reason":"..."} 的模板 —— 连通、但产出是垃圾。
#   本探针发几道有确定断言的判定题,用**确定性规则**(非 LLM 评委,零成本零主观)判:
#   空 / 过短 / 占位符 / 非 JSON = 质量不合格。合格率低 = 现在不宜跑判定类任务。
_PLACEHOLD = ("...", "…", "..", "todo", "xxx", "placeholder", "n/a", "待填", "填写")
_QUALITY_CASES = [
    ("你是技术选型判定器。只输出 JSON:{\"verdict\":\"yes|no\",\"reason\":\"20字以内真实理由\"}",
     "项目:一个把 PDF 转 Markdown 的纯 Python 库。判它对古籍OCR产线有没有用。"),
    ("你是技术选型判定器。只输出 JSON:{\"verdict\":\"yes|no\",\"reason\":\"20字以内真实理由\"}",
     "项目:Redis 内存数据库。判它对我们零常驻主机的架构有没有用。"),
    ("只输出 JSON:{\"answer\":\"一句话实质回答\"}", "用一句话说明中医「君臣佐使」是什么。"),
]


def _quality_bad(txt):
    """确定性断言:返回不合格原因,合格返回空串。"""
    t = (txt or "").strip()
    if len(t) < 15:
        return "过短/空"
    lo = t.lower()
    # 抠出 JSON 值里的字段内容判占位(reason/answer 是模型该真写的地方)
    import re as _re
    vals = _re.findall(r'"(?:reason|answer|verdict)"\s*:\s*"([^"]*)"', t)
    for v in vals:
        vs = v.strip().lower()
        if not vs or vs in _PLACEHOLD or all(c in ".·…" for c in vs):
            return f"占位符字段值「{v[:12]}」"
    if "{" not in t:
        return "非 JSON(该输出结构化却没有)"
    return ""


def probe_quality():
    key = os.environ.get("GW_KEY", "")
    hdr = dict(UA)
    if key:
        hdr["X-Gateway-Key"] = key
    results = []
    for sysmsg, usermsg in _QUALITY_CASES:
        try:
            req = urllib.request.Request(
                f"{SITE}/api/gateway/chat",
                data=json.dumps({"messages": [{"role": "system", "content": sysmsg},
                                              {"role": "user", "content": usermsg}],
                                 "max_tokens": 200, "json": True, "temperature": 0,
                                 "source": "fleet_quality_probe"}).encode(),
                headers=hdr)
            j = json.loads(urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "replace"))
            txt = (j.get("data") or j).get("text", "")
            model = (j.get("data") or j).get("supplier", "?")
            bad = _quality_bad(txt)
            results.append((model, "bad" if bad else "ok", bad))
        except Exception as e:
            results.append(("?", "ERR", str(e)[:40]))
    n_ok = sum(1 for _, s, _ in results if s == "ok")
    return n_ok, len(results), results


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
    try:
        q_ok, q_total, q_rows = probe_quality()
    except Exception as e:
        q_ok, q_total, q_rows = 0, 0, [("?", "ERR", str(e)[:40])]

    down = [r for r in rows if r[1] == "DOWN"]
    core_down = [r for r in down if r[0] in ("modelscope", "sensenova", "cerebras")]
    # 质量降级也进 alert:半数以上判定题返回占位符/空 = 现在不宜跑判定类任务
    quality_bad = q_total > 0 and q_ok * 2 < q_total
    alert = bool(core_down) or len(down) >= 3 or e2e_status != "ok" or quality_bad

    lines = ["| provider | status | ms | note |", "|---|---|---|---|"]
    for name, st, ms, note in rows:
        icon = {"ok": "✅", "ok*": "✅", "no-key": "\U0001F511", "DOWN": "❌"}.get(st, "?")
        lines.append(f"| {name} | {icon} {st} | {ms} | {note} |")
    lines.append("")
    lines.append(f"**e2e pengzhuang**: {'✅' if e2e_status=='ok' else '❌'} {e2e_status} {e2e_s}s {e2e_info}")
    lines.append("")
    q_icon = "✅" if not quality_bad else "\U0001F534"
    lines.append(f"**产出质量探针**: {q_icon} {q_ok}/{q_total} 道判定题合格"
                 + ("(半数以上返回占位符/空 → 现在不宜跑判定类任务)" if quality_bad else ""))
    for model, st, note in q_rows:
        if st != "ok":
            lines.append(f"  - {model}: {st} {note}")
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
