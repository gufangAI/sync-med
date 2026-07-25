# coding: utf-8
"""
platform_report.py -- automated platform data-snapshot reporter (daily / weekly / monthly).

Why this exists: the hand-written daily report (skill `daily-report`) depends on a
human remembering to run it. It went missing 2026-07-17..07-22 (6 days) and the
weekly / monthly cadence never existed at all. This script is the machine half of
that job: it never forgets, and it only reports *numbers* -- the human report keeps
its monopoly on decisions, narrative and judgement.

Division of labour
  auto  (this script) : objective counters straight out of D1 + GitHub Actions,
                        period-over-period deltas, and an attendance roll-call that
                        names the days whose manual report is missing.
  human (skill daily-report) : what was decided, what broke, what is next.

Safety
  * D1 access is SELECT-only, enforced in code (`d1_query` refuses anything else).
  * Zero R2 access -- no bucket listing, no object reads (repo-wide iron rule,
    CI-enforced by .github/workflows/guard_no_list.yml).
  * Nothing is written to any production table. Only outputs: a markdown file under
    reports/platform/auto/, a JSON snapshot under reports/_snapshots/, and a GitHub
    Issue.
  * All credentials come from env / Actions secrets. Nothing is hardcoded.

Env
  CF_ACCOUNT_ID, D1_API_TOKEN, D1_DATABASE_ID   -- main guyaofang-db
  GUJI_DATABASE_ID                              -- guji-db (secret, optional; the
                                                   guji metric is skipped if absent)
  GH_TOKEN, GITHUB_REPOSITORY                   -- Issue creation + Actions stats

Usage
  python scripts/platform_report.py --period daily
  python scripts/platform_report.py --period weekly  --date 2026-07-19
  python scripts/platform_report.py --period monthly --date 2026-06-15
  (--date empty = cron semantics: daily -> today, weekly -> ISO week of today,
   monthly -> the month *before* today. --date given = the period *containing*
   that date, which is what back-filling a missed period wants.)
"""
import os
import sys
import json
import time
import glob
import argparse
import datetime
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BJ = datetime.timezone(datetime.timedelta(hours=8))

CF_ACC = os.environ.get("CF_ACCOUNT_ID", "")
D1_TOK = os.environ.get("D1_API_TOKEN", "")
D1_MAIN = os.environ.get("D1_DATABASE_ID", "")
D1_GUJI = os.environ.get("GUJI_DATABASE_ID", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")

REPORT_DIR = os.path.join("reports", "platform", "auto")
SNAP_DIR = os.path.join("reports", "_snapshots")
MANUAL_DIR = os.path.join("reports", "platform")

PERIOD_CN = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
PERIOD_ICON = {"daily": "\U0001F4CA", "weekly": "\U0001F4C8", "monthly": "\U0001F5D3️"}


# ---------------------------------------------------------------------------
# D1 (read-only)
# ---------------------------------------------------------------------------
def d1_query(db_id, sql, params=None):
    """POST one SELECT to the D1 HTTP API. Refuses non-SELECT by construction."""
    head = sql.strip().lstrip("(").upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise RuntimeError("read-only guard: only SELECT/WITH allowed, got: " + sql[:40])
    if not (CF_ACC and D1_TOK and db_id):
        raise RuntimeError("missing CF_ACCOUNT_ID / D1_API_TOKEN / database id")
    url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (CF_ACC, db_id)
    body = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Authorization": "Bearer " + D1_TOK,
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read().decode("utf-8"))
            if not j.get("success"):
                raise RuntimeError("d1 error: " + json.dumps(j.get("errors"), ensure_ascii=False)[:240])
            return j["result"][0].get("results", [])
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:240]
            except Exception:
                pass
            last = RuntimeError("HTTP %s %s" % (e.code, detail))
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise last


def scalar(db_id, sql):
    rows = d1_query(db_id, sql)
    if not rows:
        return 0
    return list(rows[0].values())[0]


# ---------------------------------------------------------------------------
# period maths
# ---------------------------------------------------------------------------
def resolve_period(period, date_str):
    """-> (key, start_date, end_date, title_suffix)"""
    today = datetime.datetime.now(BJ).date()
    if period == "daily":
        d = datetime.date.fromisoformat(date_str) if date_str else today
        return d.isoformat(), d, d, d.isoformat()
    if period == "weekly":
        d = datetime.date.fromisoformat(date_str) if date_str else today
        iso_y, iso_w, _ = d.isocalendar()
        start = datetime.date.fromisocalendar(iso_y, iso_w, 1)
        end = datetime.date.fromisocalendar(iso_y, iso_w, 7)
        key = "%04d-W%02d" % (iso_y, iso_w)
        return key, start, end, key
    if period == "monthly":
        if date_str:
            d = datetime.date.fromisoformat(date_str)
            y, m = d.year, d.month
        else:
            first = today.replace(day=1)
            prev = first - datetime.timedelta(days=1)
            y, m = prev.year, prev.month
        start = datetime.date(y, m, 1)
        end = (datetime.date(y + (m == 12), (m % 12) + 1, 1) - datetime.timedelta(days=1))
        key = "%04d-%02d" % (y, m)
        return key, start, end, key
    raise SystemExit("unknown period: " + period)


# ---------------------------------------------------------------------------
# metric collection
# ---------------------------------------------------------------------------
SCALARS = [
    # (key, label, which-db, sql)
    ("med_visible", "医书可见数 (books_assets_v2)", "main",
     "SELECT COUNT(*) AS n FROM books_assets_v2 WHERE frontend_visible=1 AND upload_status='done'"),
    ("guji_visible", "古籍可见数 (guji-db guji_assets)", "guji",
     "SELECT COUNT(*) AS n FROM guji_assets WHERE frontend_visible=1"),
    ("daodu_total", "导读总条数 (book_daodu_ai)", "main",
     "SELECT COUNT(*) AS n FROM book_daodu_ai"),
    ("graph_nodes", "星图节点 (sue_graph_nodes)", "main",
     "SELECT COUNT(*) AS n FROM sue_graph_nodes"),
    ("graph_edges", "星图边 (sue_graph_edges)", "main",
     "SELECT COUNT(*) AS n FROM sue_graph_edges"),
    ("graph_edges_tier1", "其中「原文直证」边", "main",
     "SELECT COUNT(*) AS n FROM sue_graph_edges WHERE provenance_tier='原文直证'"),
    ("formulas_total", "药方库总条数 (sue_formulas)", "main",
     "SELECT COUNT(*) AS n FROM sue_formulas"),
    ("formulas_names", "药方库去重方名数 (name_norm)", "main",
     "SELECT COUNT(DISTINCT name_norm) AS n FROM sue_formulas"),
    ("formulas_books", "药方库覆盖书数 (text_id)", "main",
     "SELECT COUNT(DISTINCT text_id) AS n FROM sue_formulas"),
    ("cand_pending", "候选关系待审积压 (pending)", "main",
     "SELECT COUNT(*) AS n FROM sue_graph_candidates WHERE review_status='pending'"),
]

TABLES = [
    ("daodu_by_ver", "导读按 prompt_ver 分组", "main",
     "SELECT COALESCE(NULLIF(prompt_ver,''),'(空)') AS k, COUNT(*) AS n "
     "FROM book_daodu_ai GROUP BY 1 ORDER BY n DESC"),
    ("med_by_collection", "医书可见数按 collection 分组", "main",
     "SELECT COALESCE(NULLIF(collection,''),'(空=古方典籍库)') AS k, COUNT(*) AS n "
     "FROM books_assets_v2 WHERE frontend_visible=1 AND upload_status='done' GROUP BY 1 ORDER BY n DESC"),
]


def collect():
    dbs = {"main": D1_MAIN, "guji": D1_GUJI}
    metrics, tables = {}, {}
    for key, label, which, sql in SCALARS:
        db = dbs.get(which) or ""
        if not db:
            metrics[key] = {"label": label, "value": None,
                            "err": "缺少库 id 环境变量 (%s)" % which}
            print("  ! %-20s SKIP (no db id for %s)" % (key, which), flush=True)
            continue
        try:
            v = scalar(db, sql)
            metrics[key] = {"label": label, "value": int(v), "err": None}
            print("  + %-20s %s" % (key, v), flush=True)
        except Exception as e:  # noqa: BLE001
            metrics[key] = {"label": label, "value": None, "err": str(e)[:200]}
            print("  ! %-20s ERR %s" % (key, str(e)[:160]), flush=True)
    for key, label, which, sql in TABLES:
        db = dbs.get(which) or ""
        try:
            rows = d1_query(db, sql)
            tables[key] = {"label": label,
                           "rows": [{"k": str(r.get("k")), "n": int(r.get("n") or 0)} for r in rows],
                           "err": None}
            print("  + %-20s %d group(s)" % (key, len(rows)), flush=True)
        except Exception as e:  # noqa: BLE001
            tables[key] = {"label": label, "rows": [], "err": str(e)[:200]}
            print("  ! %-20s ERR %s" % (key, str(e)[:160]), flush=True)
    return metrics, tables


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------
def snap_path(period, key):
    return os.path.join(SNAP_DIR, period, key + ".json")


def load_prev(period, key):
    files = sorted(glob.glob(os.path.join(SNAP_DIR, period, "*.json")))
    prev = None
    for fp in files:
        k = os.path.splitext(os.path.basename(fp))[0]
        if k < key:
            prev = fp
    if not prev:
        return None, None
    try:
        with open(prev, encoding="utf-8") as f:
            return json.load(f), os.path.splitext(os.path.basename(prev))[0]
    except Exception:  # noqa: BLE001
        return None, None


def save_snap(period, key, payload):
    p = snap_path(period, key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
    return p


def delta_str(cur, prev):
    if cur is None or prev is None:
        return "—"
    d = cur - prev
    if d > 0:
        return "+%d" % d
    if d < 0:
        return "%d" % d
    return "0"


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
def gh_api(path, method="GET", payload=None):
    url = "https://api.github.com" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + GH_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "platform-report",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def actions_stats(start, end):
    """Runs created inside [start, end] grouped by workflow name."""
    per = {}
    total = ok = 0
    page = 1
    while page <= 10:
        q = "/repos/%s/actions/runs?created=%s..%s&per_page=100&page=%d" % (
            REPO, start.isoformat(), end.isoformat(), page)
        j = gh_api(q)
        runs = j.get("workflow_runs") or []
        if not runs:
            break
        for r in runs:
            name = r.get("name") or "(unnamed)"
            concl = r.get("conclusion") or r.get("status") or "?"
            e = per.setdefault(name, {"n": 0, "success": 0, "failure": 0, "cancelled": 0, "other": 0})
            e["n"] += 1
            total += 1
            if concl == "success":
                e["success"] += 1
                ok += 1
            elif concl == "failure":
                e["failure"] += 1
            elif concl == "cancelled":
                e["cancelled"] += 1
            else:
                e["other"] += 1
        if len(runs) < 100:
            break
        page += 1
    return {"total": total, "success": ok, "per_workflow": per}


def attendance(start, end):
    """Which manual daily reports (reports/platform/YYYY-MM-DD.md) exist in the period."""
    today = datetime.datetime.now(BJ).date()
    last = min(end, today)
    days, missing, present = [], [], []
    d = start
    while d <= last:
        days.append(d)
        if os.path.exists(os.path.join(MANUAL_DIR, d.isoformat() + ".md")):
            present.append(d.isoformat())
        else:
            missing.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return {"expected": len(days), "present": present, "missing": missing}


def create_issue(title, body):
    j = gh_api("/repos/%s/issues" % REPO, "POST", {"title": title, "body": body})
    return j.get("number"), j.get("html_url")


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------
def build_md(period, key, start, end, metrics, tables, prev, prev_key, extra):
    cn = PERIOD_CN[period]
    now = datetime.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    pm = (prev or {}).get("metrics", {})
    L = []
    L.append("# %s 平台数据%s · %s" % (PERIOD_ICON[period], cn, key))
    L.append("")
    L.append("> 自动生成 · 北京时间 %s · run `%s`" % (now, RUN_ID))
    L.append("> 统计区间 %s ~ %s · 对比基准 %s" % (
        start.isoformat(), end.isoformat(), prev_key or "无（首期）"))
    L.append("")
    L.append("## 一、核心资产快照")
    L.append("")
    L.append("| 指标 | 当前 | 上期 | Δ |")
    L.append("|---|---:|---:|---:|")
    for k, label, _which, _sql in SCALARS:
        m = metrics.get(k, {})
        cur = m.get("value")
        old = (pm.get(k) or {}).get("value")
        if cur is None:
            shown = "⚠ " + (m.get("err") or "n/a")[:60]
            L.append("| %s | %s | %s | — |" % (label, shown,
                                                    "—" if old is None else "{:,}".format(old)))
        else:
            L.append("| %s | %s | %s | %s |" % (
                label, "{:,}".format(cur),
                "—" if old is None else "{:,}".format(old),
                delta_str(cur, old)))
    L.append("")
    for k, _label, _which, _sql in TABLES:
        t = tables.get(k) or {}
        L.append("### " + (t.get("label") or k))
        L.append("")
        if t.get("err"):
            L.append("⚠ 取数失败：`%s`" % t["err"])
            L.append("")
            continue
        rows = t.get("rows") or []
        if not rows:
            L.append("（无数据）")
            L.append("")
            continue
        prev_rows = {r["k"]: r["n"] for r in ((prev or {}).get("tables", {}).get(k, {}) or {}).get("rows", [])}
        L.append("| 分组 | 条数 | Δ |")
        L.append("|---|---:|---:|")
        for r in rows[:30]:
            L.append("| %s | %s | %s |" % (r["k"], "{:,}".format(r["n"]),
                                           delta_str(r["n"], prev_rows.get(r["k"]))))
        L.append("")

    if extra.get("attendance"):
        a = extra["attendance"]
        L.append("## 二、人工日报出勤（reports/platform/）")
        L.append("")
        L.append("**本%s应 %d 份，实到 %d 份。**" % (
            cn, a["expected"], len(a["present"])))
        L.append("")
        if a["missing"]:
            L.append("❌ 缺勤点名：" + "、".join(a["missing"]))
        else:
            L.append("✅ 无缺勤。")
        L.append("")

    if extra.get("actions"):
        s = extra["actions"]
        rate = (100.0 * s["success"] / s["total"]) if s["total"] else 0.0
        L.append("## 三、本%s GitHub Actions 运行局面" % cn)
        L.append("")
        L.append("总 run %d 次，成功 %d 次，成功率 **%.1f%%**。"
                 % (s["total"], s["success"], rate))
        L.append("")
        if s["per_workflow"]:
            L.append("| workflow | run | 成功 | 失败 | 取消 | 成功率 |")
            L.append("|---|---:|---:|---:|---:|---:|")
            for name, e in sorted(s["per_workflow"].items(), key=lambda kv: -kv[1]["n"])[:40]:
                r = (100.0 * e["success"] / e["n"]) if e["n"] else 0.0
                L.append("| %s | %d | %d | %d | %d | %.0f%% |" % (
                    name, e["n"], e["success"], e["failure"], e["cancelled"], r))
            L.append("")

    errs = [m for m in metrics.values() if m.get("err")] + [t for t in tables.values() if t.get("err")]
    L.append("## 四、取数健康")
    L.append("")
    if errs:
        L.append("以下指标本期没取到（据实报，不编数）：")
        L.append("")
        for e in errs:
            L.append("- **%s** — `%s`" % (e.get("label"), (e.get("err") or "")[:180]))
    else:
        L.append("全部指标取数成功。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 本报告只管**客观数字**（D1 只读 SELECT，零 R2 访问）。"
             "决策/叙事/风险由**人工日报**（skill `daily-report` → "
             "`reports/platform/YYYY-MM-DD.md`）负责，两者不互相替代。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, choices=["daily", "weekly", "monthly"])
    ap.add_argument("--date", default="")
    ap.add_argument("--no-issue", action="store_true")
    args = ap.parse_args()

    date_str = (args.date or os.environ.get("REPORT_DATE") or "").strip()
    period = args.period
    key, start, end, _sfx = resolve_period(period, date_str)
    print("[%s] period=%s key=%s range=%s..%s repo=%s" % (
        RUN_ID, period, key, start, end, REPO), flush=True)

    print("-- collecting D1 metrics --", flush=True)
    metrics, tables = collect()

    prev, prev_key = load_prev(period, key)
    print("-- previous snapshot: %s --" % (prev_key or "none"), flush=True)

    extra = {}
    if period in ("weekly", "monthly"):
        try:
            extra["actions"] = actions_stats(start, end)
            print("-- actions runs in period: %d --" % extra["actions"]["total"], flush=True)
        except Exception as e:  # noqa: BLE001
            print("!! actions stats failed: %s" % str(e)[:200], flush=True)
        extra["attendance"] = attendance(start, end)
        print("-- manual reports: %d/%d, missing %s --" % (
            len(extra["attendance"]["present"]), extra["attendance"]["expected"],
            ",".join(extra["attendance"]["missing"]) or "-"), flush=True)

    md = build_md(period, key, start, end, metrics, tables, prev, prev_key, extra)

    os.makedirs(REPORT_DIR, exist_ok=True)
    fname = "%s_%s.md" % (PERIOD_CN[period], key)
    fpath = os.path.join(REPORT_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print("-- wrote %s (%d bytes) --" % (fpath, len(md)), flush=True)

    payload = {
        "period": period, "key": key,
        "range": [start.isoformat(), end.isoformat()],
        "generated_at": datetime.datetime.now(BJ).isoformat(),
        "run_id": RUN_ID,
        "metrics": {k: {"value": v.get("value"), "err": v.get("err")} for k, v in metrics.items()},
        "tables": {k: {"rows": v.get("rows"), "err": v.get("err")} for k, v in tables.items()},
        "extra": extra,
    }
    spath = save_snap(period, key, payload)
    print("-- wrote %s --" % spath, flush=True)

    issue_no, issue_url = None, None
    if not args.no_issue and GH_TOKEN and REPO:
        title = "%s 平台%s%s %s" % (
            PERIOD_ICON[period],
            "数据" if period == "daily" else "",
            PERIOD_CN[period], key)
        try:
            issue_no, issue_url = create_issue(title, md)
            print("-- issue #%s %s --" % (issue_no, issue_url), flush=True)
        except Exception as e:  # noqa: BLE001
            print("!! issue creation failed: %s" % str(e)[:300], flush=True)

    with open("platform_report_result.json", "w", encoding="utf-8") as f:
        json.dump({"period": period, "key": key, "file": fpath, "snapshot": spath,
                   "issue": issue_no, "issue_url": issue_url,
                   "metrics": payload["metrics"]}, f, ensure_ascii=False, indent=1)

    print("\n===== REPORT PREVIEW =====\n" + md[:4000], flush=True)


if __name__ == "__main__":
    main()
