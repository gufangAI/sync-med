# Team daily-report auditor (runs in GitHub Actions cron).
# Each CC line must submit a daily report at reports/<line>/<YYYY-MM-DD>.md in this repo.
# This script: (1) checks who submitted today, (2) LLM-audits each report for red flags
# (stalled progress / wrong-goods / self-built-engine drift / vague hand-waving / numbers not backed),
# (3) opens/updates a GitHub Issue with \U0001f534 when anything is missing or flagged -> founder's phone.
# Founder does NOT chase reports; the system does. Free LLM (Xunfei) does the reading.
import os, sys, json, datetime, urllib.request, urllib.parse

REPO = os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med")
GH_TOKEN = os.environ["GH_ISSUE_TOKEN"]
XF_KEYS = [k.strip() for k in (os.environ.get("XF_API_KEYS") or os.environ.get("XF_KEY") or "").split(",") if k.strip()]
XF_HOST = os.environ.get("XF_HOST", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_CHAT_MODEL", "xopqwen36v35b")

# lines that MUST report each day (add/remove as team changes)
LINES = [l.strip() for l in (os.environ.get("TEAM_LINES")
         or "download,guji,platform,ocr").split(",") if l.strip()]
LINE_LABEL = {"download": "\u4e0b\u8f7d\u7ebf", "guji": "\u53e4\u7c4d\u7ebf", "platform": "\u5e73\u53f0\u7ebf", "ocr": "OCR\u7ebf"}

TODAY = os.environ.get("AUDIT_DATE") or datetime.date.today().isoformat()
REPORT_DIR = "reports"

AUDIT_SYS = (
    "\u4f60\u662f AI \u56e2\u961f\u65e5\u62a5\u5ba1\u6838\u5b98\u3002\u7ed9\u4f60\u4e00\u6761\u5de5\u4f5c\u7ebf\u4eca\u5929\u7684\u65e5\u62a5,\u4f60\u8981\u6311\u51fa\u300c\u5371\u9669\u4fe1\u53f7\u300d\u2014\u2014"
    "\u8fd9\u4e9b\u662f\u8fc7\u53bb\u771f\u51fa\u8fc7\u4e8b\u7684\u5751:\u2460\u81ea\u7814\u5de5\u5177/\u5f15\u64ce\u8dd1\u504f(\u8be5\u7528\u6210\u719f\u5f00\u6e90\u5374\u81ea\u5df1\u9020);\u2461\u8d27\u4e0d\u5bf9\u677f"
    "(\u4e0b\u8f7d/\u4ea7\u51fa\u7684\u5185\u5bb9\u548c\u6807\u6ce8\u5bf9\u4e0d\u4e0a);\u2462\u8fdb\u5ea6\u505c\u6ede\u6216\u7a7a\u8f6c(\u8bf4\u4e86\u534a\u5929\u6ca1\u771f\u4ea7\u51fa);\u2463\u542b\u7cca\u5176\u8f9e"
    "/\u62a5\u559c\u4e0d\u62a5\u5fe7(\u53ea\u8bf4\u505a\u4e86\u4ec0\u4e48\u4e0d\u7ed9\u6570\u5b57\u3001\u4e0d\u63d0\u95ee\u9898);\u2464\u6570\u5b57\u5bf9\u4e0d\u4e0a/\u65e0\u5b9e\u8bc1\u6491\u7740;\u2465\u70e7\u94b1\u65e0\u4ea7\u51fa\u3002"
    "\u4e25\u683c\u8f93\u51fa JSON:{\"status\":\"ok|warn|alert\",\"flags\":[\"\u5177\u4f53\u95ee\u9898\"],\"one_line\":\"\u4e00\u53e5\u8bdd\u7ed3\u8bba\"}\u3002"
    "\u6ca1\u95ee\u9898=ok;\u53ef\u7591=warn;\u660e\u663e\u8e29\u5751=alert\u3002\u5b81\u53ef\u591a\u62a5\u8b66\u4e0d\u53ef\u6f0f(\u6f0f\u4e00\u6b21=\u53c8\u4e00\u4e2a\u4e0b\u8f7d\u7ebf\u5751\u56e2\u961f\u4e00\u4e2a\u6708)\u3002"
)


def gh_api(method, path, body=None):
    url = "https://api.github.com" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + GH_TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "team-report-audit",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get_report(line):
    # read reports/<line>/<TODAY>.md from repo default branch
    path = "/repos/%s/contents/%s/%s/%s.md" % (REPO, REPORT_DIR, line, TODAY)
    try:
        obj = gh_api("GET", path)
        import base64
        return base64.b64decode(obj["content"]).decode("utf-8", "replace")
    except Exception:
        return None


def xf_audit(text):
    if not XF_KEYS:
        return {"status": "warn", "flags": ["\u5ba1\u6838LLM\u65e0\u51ed\u636e\u00b7\u4ec5\u68c0\u67e5\u662f\u5426\u63d0\u4ea4"], "one_line": "\u672a\u505a\u5185\u5bb9\u5ba1\u6838"}
    body = {"model": XF_MODEL, "messages": [
        {"role": "user", "content": AUDIT_SYS + chr(10) + chr(10) + "===" + chr(10) + chr(10) + text[:6000]},
    ], "temperature": 0.2, "max_tokens": 600}
    for key in XF_KEYS:
        try:
            req = urllib.request.Request(XF_HOST + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode())
            content = j["choices"][0]["message"]["content"]
            s = content[content.find("{"): content.rfind("}") + 1]
            return json.loads(s)
        except Exception as e:
            last = str(e)
            continue
    return {"status": "warn", "flags": ["\u5ba1\u6838LLM\u8c03\u7528\u5931\u8d25:" + last[:80]], "one_line": "\u5ba1\u6838\u672a\u5b8c\u6210"}


def main():
    lines_out = []
    worst = "ok"
    rank = {"ok": 0, "warn": 1, "alert": 2, "missing": 3}
    for line in LINES:
        label = LINE_LABEL.get(line, line)
        rep = get_report(line)
        if rep is None:
            lines_out.append(("missing", label, {"one_line": "\u274c \u4eca\u65e5\u672a\u63d0\u4ea4\u65e5\u62a5", "flags": ["\u672a\u4ea4\u62a5\u544a"]}))
            if rank["missing"] > rank[worst]:
                worst = "alert"  # missing counts as alert-level red
            continue
        verdict = xf_audit(rep)
        st = verdict.get("status", "warn")
        lines_out.append((st, label, verdict))
        if rank.get(st, 1) > rank[worst]:
            worst = st

    # build issue body
    icon = {"ok": "\u2705", "warn": "\u26a0\ufe0f", "alert": "\U0001f534", "missing": "\u274c"}
    red = worst in ("alert",)
    title = "%s \u56e2\u961f\u65e5\u62a5\u5ba1\u6838 \u00b7 %s" % ("\U0001f534" if red else "\u2705", TODAY)
    lines_md = ["# \u56e2\u961f\u65e5\u62a5\u5ba1\u6838 \u00b7 %s" % TODAY, "",
                "> \u5404\u7ebf\u5fc5\u987b\u6bcf\u5929\u63d0\u4ea4 `reports/<\u7ebf>/%s.md`;\u672c\u5ba1\u6838\u81ea\u52a8\u8dd1(cron)\u3001\u521b\u59cb\u4eba\u96f6\u5e72\u9884\u3002" % TODAY, ""]
    for st, label, v in lines_out:
        lines_md.append("## %s %s \u2014 %s" % (icon.get(st, "\u26a0\ufe0f"), label, v.get("one_line", "")))
        for f in (v.get("flags") or []):
            lines_md.append("- %s" % f)
        lines_md.append("")
    submitted = sum(1 for st, _, _ in lines_out if st != "missing")
    lines_md.insert(3, "**\u63d0\u4ea4 %d/%d \u00b7 \u6700\u9ad8\u98ce\u9669: %s**\n" % (submitted, len(LINES), worst.upper()))
    body = "\n".join(lines_md)

    # find existing open issue for today, update; else create
    issues = gh_api("GET", "/repos/%s/issues?state=open&labels=team-audit&per_page=20" % REPO)
    found = None
    for it in issues:
        if TODAY in it.get("title", ""):
            found = it["number"]; break
    payload = {"title": title, "body": body, "labels": ["team-audit"]}
    if found:
        gh_api("PATCH", "/repos/%s/issues/%d" % (REPO, found), payload)
        print("updated issue #%d: %s" % (found, title), flush=True)
    else:
        r = gh_api("POST", "/repos/%s/issues" % REPO, payload)
        print("created issue #%d: %s" % (r["number"], title), flush=True)
    print("AUDIT %s worst=%s submitted=%d/%d" % (TODAY, worst, submitted, len(LINES)), flush=True)


if __name__ == "__main__":
    main()
