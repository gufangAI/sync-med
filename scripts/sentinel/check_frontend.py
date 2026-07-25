#!/usr/bin/env python3
"""
check_frontend.py - frontend real-experience sentinel for gufangai.com.

Founder's own words (2026-07-25): "I don't want to be yelling at people every
day, I want us discussing how the project is going every day. Having me yell
at you is a waste of time." The thing he caught today should have been caught
by us automatically: /overseas had a large share of covers degraded into text
placeholders, and the platform team's own self-check used the wrong criterion
and counted that degradation as a success.

This script hits gufangai.com exactly like a real anonymous visitor would
(public GET/POST endpoints only, no admin token) and reports five signals
whose pass/fail criteria are deliberately picky about "fake success":
  1. cover_real_ratio  - Content-Type of /api/reader/{book_id}/cover, not just HTTP 200
  2. page_availability - median latency over 3 tries + non-empty `items`, not just HTTP 200
  3. ai_search         - has real evidence/formula content, not just HTTP 200
  4. graph             - node/edge counts, alert if they dropped since last run
  5. d1_freshness       - MAX(created_at) age, catches a stalled ingest pipeline

Read-only: never writes D1 directly (only SELECT via the D1 HTTP API), never
touches guyaofang-web production code. The only repo writes are its own
reports/sentinel/*.json trend files. Calling /api/ai/search and
/api/reader/*/cover as a normal client does cause the *production app itself*
to log a row / warm its own cache, same as any real visitor would - that is
the point of "real user perspective" and is not a write this script performs.

Env vars:
  CF_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN - D1 HTTP API, read-only SELECT
  GITHUB_OUTPUT / GITHUB_STEP_SUMMARY - optional, set by GitHub Actions
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

BASE_URL = "https://gufangai.com"
BJ_TZ = timezone(timedelta(hours=8))

COVER_SAMPLE_N = 30
COVER_REAL_RATIO_ALERT_BELOW = 0.70

PAGE_LATENCY_ALERT_SEC = 3.0
PAGE_TARGETS = [
    {"key": "home", "path": "/", "is_json": False, "label": "\u9996\u9875 /"},
    {"key": "overseas", "path": "/api/library/overseas?agg=1", "is_json": True,
     "label": "\u6d77\u5916\u533b\u4e66\u5217\u8868 /api/library/overseas?agg=1"},
    {"key": "overseas_guji", "path": "/api/library/overseas-guji?agg=1", "is_json": True,
     "label": "\u6d77\u5916\u53e4\u7c4d\u5217\u8868 /api/library/overseas-guji?agg=1"},
    {"key": "daodu_list", "path": "/api/daodu/list?limit=20", "is_json": True,
     "label": "AI\u5bfc\u8bfb\u5217\u8868 /api/daodu/list?limit=20"},
]

# \u56fa\u5b9a\u7684\u4e2d\u533b\u7ecf\u5178\u95ee\u9898 -- \u6842\u679d\u6c64\u662f\u4f24\u5bd2\u8bba\u7b2c\u4e00\u65b9,\u7406\u5e94\u7a33\u5b9a\u547d\u4e2d\u8bc1\u636e/\u65b9\u5242
AI_SEARCH_QUESTION = "\u6842\u679d\u6c64\u7684\u529f\u6548\u4e0e\u4e3b\u6cbb"
AI_SEARCH_TIMEOUT_SEC = 20

D1_FRESHNESS_ALERT_DAYS = 7

STATE_PATH = "reports/sentinel/_state.json"


# ───────────────────────── low-level helpers ─────────────────────────

def bj_now():
    return datetime.now(BJ_TZ)


def d1_query(sql):
    """Read-only D1 SELECT via Cloudflare's HTTP API. Raises on any failure."""
    acc = os.environ["CF_ACCOUNT_ID"]
    db = os.environ["D1_DATABASE_ID"]
    tok = os.environ["D1_API_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        json={"sql": sql},
        timeout=30,
    )
    resp.raise_for_status()
    j = resp.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 query failed: {str(j.get('errors'))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []


def safe_get(url, timeout=15):
    t0 = time.time()
    try:
        resp = requests.get(url, timeout=timeout)
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            j = resp.json()
        except Exception:
            j = None
        return {"ok": True, "status": resp.status_code, "headers": resp.headers,
                 "text": resp.text[:2000], "json": j, "elapsed_ms": elapsed_ms, "error": None}
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "status": None, "headers": {}, "text": "", "json": None,
                 "elapsed_ms": elapsed_ms, "error": str(e)}


def safe_post(url, body, timeout=25):
    t0 = time.time()
    try:
        resp = requests.post(url, json=body, timeout=timeout)
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            j = resp.json()
        except Exception:
            j = None
        return {"ok": True, "status": resp.status_code, "headers": resp.headers,
                 "text": resp.text[:2000], "json": j, "elapsed_ms": elapsed_ms, "error": None}
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "status": None, "headers": {}, "text": "", "json": None,
                 "elapsed_ms": elapsed_ms, "error": str(e)}


def median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def fmt_ms(ms):
    if ms is None:
        return "N/A"
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.2f}s"


def parse_ts_to_dt(val):
    """Best-effort parse of a D1 created_at value into an aware UTC datetime.
    Handles unix seconds, unix ms, or an ISO-8601 string -- the exact column
    format wasn't nailed down from docs alone, so this stays defensive rather
    than assuming one shape."""
    if val is None:
        return None
    try:
        num = float(val)
        if num > 1e12:
            num = num / 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def classify_source(x_source):
    xs = (x_source or "")
    low = xs.lower()
    if not xs:
        return "(no-header,\u5373generated\u6587\u5b57\u5c01\u9762)"
    if "dlink" in low:
        return "dlink"
    if "pan123" in low:
        return "pan123"
    if low == "edge-cache":
        return "edge-cache"
    if low == "r2thumb":
        return "r2thumb"
    if low == "fallback-svg":
        return "fallback-svg"
    return f"other:{xs}"


# ───────────────────────── the five checks ─────────────────────────

def check_cover():
    result = {"name": "cover_real_ratio", "ok": False, "alert": False}
    try:
        rows = d1_query(
            "SELECT book_id FROM books_assets_v2 "
            "WHERE frontend_visible=1 AND upload_status='done' "
            f"ORDER BY RANDOM() LIMIT {COVER_SAMPLE_N}"
        )
    except Exception as e:
        result["alert"] = True
        result["reason"] = f"D1\u62bd\u6837\u5931\u8d25: {e}"
        return result

    book_ids = [r["book_id"] for r in rows if r.get("book_id")]
    total = len(book_ids)
    real = svg_generated = svg_placeholder = svg_other = http_fail = 0
    source_counter = {}
    samples = []

    for bid in book_ids:
        url = f"{BASE_URL}/api/reader/{bid}/cover"
        r = safe_get(url, timeout=15)
        ct = (r["headers"].get("Content-Type") or "").split(";")[0].strip().lower()
        xsrc = r["headers"].get("X-Source", "")
        xkind = r["headers"].get("X-Cover-Kind", "")
        bucket = classify_source(xsrc)
        source_counter[bucket] = source_counter.get(bucket, 0) + 1

        if not r["ok"] or r["status"] != 200:
            http_fail += 1
            kind = "http_fail"
        elif ct == "image/webp":
            real += 1
            kind = "real_webp"
        elif ct == "image/svg+xml":
            if xkind == "generated":
                svg_generated += 1
                kind = "svg_generated(\u8bbe\u8ba1\u6b3e\u6587\u5b57\u5c01\u9762·\u975e\u6545\u969c)"
            elif xkind == "placeholder":
                svg_placeholder += 1
                kind = "svg_placeholder(\u771f\u964d\u7ea7)"
            else:
                svg_other += 1
                kind = "svg_other"
        else:
            http_fail += 1
            kind = f"unexpected_ct:{ct}"
        samples.append({"book_id": bid, "status": r["status"], "content_type": ct,
                          "x_source": xsrc, "x_cover_kind": xkind,
                          "elapsed_ms": r["elapsed_ms"], "kind": kind})

    real_ratio = (real / total) if total else 0.0
    result.update({
        "ok": True,
        "total_sampled": total,
        "real_webp": real,
        "svg_generated": svg_generated,
        "svg_placeholder": svg_placeholder,
        "svg_other": svg_other,
        "http_fail": http_fail,
        "real_ratio": round(real_ratio, 4),
        "source_dist": source_counter,
        "samples": samples,
        "alert": real_ratio < COVER_REAL_RATIO_ALERT_BELOW,
    })
    return result


def check_pages():
    out = {"name": "page_availability", "targets": {}}
    for t in PAGE_TARGETS:
        url = BASE_URL + t["path"]
        tries = [safe_get(url, timeout=15) for _ in range(3)]
        elapsed_ok = [r["elapsed_ms"] for r in tries if r["ok"]]
        status_list = [r["status"] for r in tries]
        all200 = all(s == 200 for s in status_list)
        median_ms = median(elapsed_ok) if elapsed_ok else None

        item_count = None
        if t["is_json"]:
            counts = [len(r["json"]["items"]) for r in tries
                      if r["ok"] and isinstance(r["json"], dict) and isinstance(r["json"].get("items"), list)]
            item_count = counts[-1] if counts else 0

        alert = (not all200) or (median_ms is None) or (median_ms > PAGE_LATENCY_ALERT_SEC * 1000) \
            or (t["is_json"] and (item_count is None or item_count == 0))

        out["targets"][t["key"]] = {
            "label": t["label"], "url": url, "status_list": status_list,
            "median_ms": median_ms, "item_count": item_count, "alert": alert,
        }
    out["alert"] = any(v["alert"] for v in out["targets"].values())
    return out


def check_ai_search():
    url = f"{BASE_URL}/api/ai/search"
    r = safe_post(url, {"query": AI_SEARCH_QUESTION}, timeout=25)
    out = {"name": "ai_search", "url": url, "status": r["status"],
            "elapsed_ms": r["elapsed_ms"], "alert": False}

    if not r["ok"]:
        out["alert"] = True
        out["reason"] = f"\u8bf7\u6c42\u5f02\u5e38: {r['error']}"
        return out

    body = r["json"] or {}
    if r["status"] == 403 and isinstance(body.get("error"), dict) \
            and body["error"].get("code") == "GUEST_LIMIT_REACHED":
        # runner IP \u5f53\u5929\u8bbf\u5ba2\u914d\u989d\u7528\u5c3d,\u662f\u914d\u989d\u95ee\u9898\u4e0d\u662f\u53ef\u7528\u6027\u6545\u969c -- \u89c1\u8bef\u62a5\u9632\u8303\u8bbe\u8ba1
        out["skipped"] = True
        out["reason"] = "\u672c\u8f6e runner IP \u8bbf\u5ba2\u914d\u989d\u5df2\u7528\u5c3d(\u975e\u6545\u969c,guest_gate \u6309 IP \u8ba1\u6570)"
        out["alert"] = False
        return out

    evidence = body.get("public_evidence") or []
    formulas = body.get("formula_candidates") or []
    out["evidence_count"] = len(evidence)
    out["formula_count"] = len(formulas)
    out["ok_field"] = body.get("ok")

    is_slow = r["elapsed_ms"] is not None and r["elapsed_ms"] > AI_SEARCH_TIMEOUT_SEC * 1000
    has_content = (r["status"] == 200 and body.get("ok") is True
                    and (len(evidence) > 0 or len(formulas) > 0))
    out["alert"] = bool(is_slow or not has_content)
    if is_slow:
        out["reason"] = f"\u8017\u65f6 {fmt_ms(r['elapsed_ms'])} \u8d85\u8fc7 {AI_SEARCH_TIMEOUT_SEC}s \u9608\u503c"
    elif not has_content:
        out["reason"] = (f"\u54cd\u5e94\u65e0\u8bc1\u636e/\u65b9\u5242\u5185\u5bb9: status={r['status']} ok={body.get('ok')} "
                          f"evidence={len(evidence)} formulas={len(formulas)}")
    return out


def check_graph(prev_state):
    url = f"{BASE_URL}/api/ai/graph"
    r = safe_get(url, timeout=20)
    out = {"name": "graph", "url": url, "elapsed_ms": r["elapsed_ms"], "alert": False}

    if not r["ok"] or r["status"] != 200 or not isinstance(r["json"], dict):
        out["alert"] = True
        out["reason"] = f"\u8bf7\u6c42\u5931\u8d25: status={r['status']} err={r['error']}"
        return out

    body = r["json"]
    stats = body.get("stats") or {}
    nodes = stats.get("nodes", len(body.get("nodes") or []))
    links = stats.get("links", len(body.get("links") or []))
    out["nodes"] = nodes
    out["links"] = links

    reasons = []
    if not body.get("ok") or not nodes or not links:
        out["alert"] = True
        reasons.append("\u56fe\u8c31\u4e3a\u7a7a\u6216 ok=false")

    prev_nodes = (prev_state or {}).get("graph_nodes")
    prev_links = (prev_state or {}).get("graph_links")
    out["prev_nodes"] = prev_nodes
    out["prev_links"] = prev_links
    if prev_nodes is not None and nodes < prev_nodes:
        out["alert"] = True
        reasons.append(f"\u8282\u70b9\u6570\u4e0b\u964d {prev_nodes}→{nodes}")
    if prev_links is not None and links < prev_links:
        out["alert"] = True
        reasons.append(f"\u8fb9\u6570\u4e0b\u964d {prev_links}→{links}")
    if reasons:
        out["reason"] = "; ".join(reasons)
    return out


def check_freshness():
    out = {"name": "d1_freshness", "alert": False}
    try:
        rows = d1_query("SELECT MAX(created_at) AS latest FROM books_assets_v2")
    except Exception as e:
        out["alert"] = True
        out["reason"] = f"D1\u67e5\u8be2\u5931\u8d25: {e}"
        return out

    raw = rows[0].get("latest") if rows else None
    out["raw_value"] = raw
    dt = parse_ts_to_dt(raw)
    if dt is None:
        out["alert"] = True
        out["reason"] = f"created_at \u65e0\u6cd5\u89e3\u6790: {raw!r}"
        return out

    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    out["latest_created_at"] = dt.isoformat()
    out["age_days"] = round(age_days, 2)
    out["alert"] = age_days > D1_FRESHNESS_ALERT_DAYS
    if out["alert"]:
        out["reason"] = f"\u6700\u65b0\u5165\u5e93\u8ddd\u4eca {age_days:.1f} \u5929,\u8d85\u8fc7 {D1_FRESHNESS_ALERT_DAYS} \u5929\u9608\u503c,\u7591\u4f3c\u5165\u5e93\u7ba1\u7ebf\u505c\u6446"
    return out


# ───────────────────────── state + trend files ─────────────────────────

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state):
    dirname = os.path.dirname(STATE_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=True, indent=2)


def daily_path(date_str):
    return f"reports/sentinel/{date_str}.json"


def append_daily(date_str, record):
    path = daily_path(date_str)
    data = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = [data]
        except Exception:
            data = []
    data.append(record)
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)


# ───────────────────────── report rendering ─────────────────────────

def build_report_markdown(cover, pages, ai_search, graph, freshness, prev_state, now, alert_count):
    lines = []
    lines.append("# \u524d\u53f0\u4f53\u9a8c\u54e8\u5175\u5de1\u68c0\u62a5\u544a")
    lines.append(f"\u5de1\u68c0\u65f6\u95f4(\u5317\u4eac\u65f6\u95f4): {now.strftime('%Y-%m-%d %H:%M:%S')}")
    conclusion = "✅ \u6b63\u5e38" if alert_count == 0 else f"🚨 \u5f02\u5e38({alert_count} \u9879)"
    lines.append(f"\u603b\u4f53\u7ed3\u8bba: {conclusion}")
    lines.append("")
    lines.append("## \u6307\u6807\u8868")
    lines.append("")
    lines.append("| # | \u68c0\u67e5\u9879 | \u7ed3\u679c | \u5173\u952e\u6570\u5b57 | \u5224\u636e |")
    lines.append("|---|---|---|---|---|")

    c_mark = "🚨" if cover.get("alert") else "✅"
    if cover.get("ok"):
        lines.append(
            f"| 1 | \u5c01\u9762\u771f\u4e66\u5f71\u7387 | {c_mark} | "
            f"\u771f\u4e66\u5f71 {cover['real_webp']}/{cover['total_sampled']} = {cover['real_ratio'] * 100:.1f}% "
            f"(\u8bbe\u8ba1\u6b3e\u6587\u5b57\u5c01\u9762{cover['svg_generated']}·\u964d\u7ea7\u5360\u4f4d{cover['svg_placeholder']}·"
            f"\u5176\u5b83SVG{cover['svg_other']}·\u8bf7\u6c42\u5931\u8d25{cover['http_fail']}) | <70% \u62a5\u8b66 |"
        )
    else:
        reason = cover.get("reason", "\u672a\u77e5\u9519\u8bef")
        lines.append(f"| 1 | \u5c01\u9762\u771f\u4e66\u5f71\u7387 | {c_mark} | {reason} | <70% \u62a5\u8b66 |")

    idx = 2
    for t in PAGE_TARGETS:
        d = pages["targets"][t["key"]]
        mark = "🚨" if d["alert"] else "✅"
        items_str = f", items={d['item_count']}" if d["item_count"] is not None else ""
        lines.append(
            f"| {idx} | {d['label']} | {mark} | "
            f"\u4e2d\u4f4d {fmt_ms(d['median_ms'])}, status={d['status_list']}{items_str} | >3s \u6216 items=0 \u62a5\u8b66 |"
        )
        idx += 1

    if ai_search.get("skipped"):
        a_mark = "⏭️"
    elif ai_search.get("alert"):
        a_mark = "🚨"
    else:
        a_mark = "✅"
    lines.append(
        f"| {idx} | AI\u5bfb\u8109\u53ef\u7528\u6027 | {a_mark} | "
        f"status={ai_search.get('status')}, \u8bc1\u636e{ai_search.get('evidence_count', '?')}\u6761/"
        f"\u65b9\u5242{ai_search.get('formula_count', '?')}\u6761, \u8017\u65f6{fmt_ms(ai_search.get('elapsed_ms'))} | "
        f"\u5931\u8d25\u6216>20s\u62a5\u8b66(\u914d\u989d\u53d7\u9650\u4e0d\u8ba1) |"
    )
    idx += 1

    g_mark = "🚨" if graph.get("alert") else "✅"
    prev_nodes_disp = graph.get("prev_nodes") if graph.get("prev_nodes") is not None else "—"
    prev_links_disp = graph.get("prev_links") if graph.get("prev_links") is not None else "—"
    lines.append(
        f"| {idx} | \u661f\u56fe\u53ef\u7528\u6027 | {g_mark} | "
        f"nodes={graph.get('nodes', '?')} links={graph.get('links', '?')} "
        f"(\u4e0a\u6b21: nodes={prev_nodes_disp} links={prev_links_disp}) | "
        f"\u6570\u91cf\u4e0b\u964d\u62a5\u8b66 |"
    )
    idx += 1

    f_mark = "🚨" if freshness.get("alert") else "✅"
    lines.append(
        f"| {idx} | D1\u5165\u5e93\u65b0\u9c9c\u5ea6 | {f_mark} | "
        f"\u6700\u65b0\u5165\u5e93 {freshness.get('latest_created_at', '?')}, \u8ddd\u4eca {freshness.get('age_days', '?')} \u5929 | "
        f">7\u5929\u62a5\u8b66 |"
    )

    lines.append("")
    lines.append("## \u5f02\u5e38\u9879\u8be6\u60c5")
    any_alert = False
    if cover.get("alert"):
        any_alert = True
        ratio_pct = cover.get("real_ratio", 0) * 100
        lines.append(f"- **\u5c01\u9762\u771f\u4e66\u5f71\u7387** {ratio_pct:.1f}% \u4f4e\u4e8e 70% \u9608\u503c\u3002"
                      f"X-Source \u5206\u5e03: {cover.get('source_dist')}")
    for t in PAGE_TARGETS:
        d = pages["targets"][t["key"]]
        if d["alert"]:
            any_alert = True
            lines.append(f"- **{d['label']}** \u5f02\u5e38: status={d['status_list']}, "
                          f"\u4e2d\u4f4d\u8017\u65f6={fmt_ms(d['median_ms'])}, items={d['item_count']}")
    if ai_search.get("alert"):
        any_alert = True
        reason = ai_search.get("reason", "\u672a\u77e5\u539f\u56e0")
        lines.append(f"- **AI\u5bfb\u8109** \u5f02\u5e38: {reason}")
    if graph.get("alert"):
        any_alert = True
        reason = graph.get("reason", "\u672a\u77e5\u539f\u56e0")
        lines.append(f"- **\u661f\u56fe** \u5f02\u5e38: {reason}")
    if freshness.get("alert"):
        any_alert = True
        reason = freshness.get("reason", "\u672a\u77e5\u539f\u56e0")
        lines.append(f"- **D1\u65b0\u9c9c\u5ea6** \u5f02\u5e38: {reason}")
    if not any_alert:
        lines.append("- \u65e0\u5f02\u5e38\u9879\u3002")

    lines.append("")
    lines.append("## \u4e0e\u4e0a\u6b21\u5de1\u68c0\u5bf9\u6bd4")
    if not prev_state:
        lines.append("- \u9996\u6b21\u5de1\u68c0,\u65e0\u5bf9\u6bd4\u57fa\u7ebf\u3002")
    else:
        lines.append(f"- \u4e0a\u6b21\u5de1\u68c0\u65f6\u95f4: {prev_state.get('checked_at_bj', '—')}")
        prev_ratio = prev_state.get("cover_real_ratio")
        if prev_ratio is not None and cover.get("ok"):
            delta = (cover["real_ratio"] - prev_ratio) * 100
            sign = "+" if delta >= 0 else ""
            lines.append(f"- \u5c01\u9762\u771f\u4e66\u5f71\u7387: {prev_ratio * 100:.1f}% → {cover['real_ratio'] * 100:.1f}% "
                          f"({sign}{delta:.1f} \u4e2a\u767e\u5206\u70b9)")
        if prev_state.get("graph_nodes") is not None:
            lines.append(f"- \u661f\u56fe\u8282\u70b9/\u8fb9: {prev_state.get('graph_nodes')}/{prev_state.get('graph_links')} "
                          f"→ {graph.get('nodes')}/{graph.get('links')}")
        if prev_state.get("d1_age_days") is not None:
            lines.append(f"- D1\u65b0\u9c9c\u5ea6: {prev_state.get('d1_age_days')} \u5929\u524d → {freshness.get('age_days')} \u5929\u524d")

    if alert_count > 0:
        lines.append("")
        lines.append("## \u5efa\u8bae\u6392\u67e5\u65b9\u5411")
        if cover.get("alert"):
            lines.append("- \u5c01\u9762\u964d\u7ea7: \u68c0\u67e5 123 \u76f4\u94fe token(PAN_DIRECT_KEY)\u662f\u5426\u8fc7\u671f\u3001pan_dir_id \u7f3a\u5931\u6bd4\u4f8b\u3001"
                          "R2 \u7f29\u7565\u56fe\u70ed\u5c42(guyaofang-assets/thumbs/)\u662f\u5426\u88ab\u6e05\u7a7a;\u5148 wrangler tail \u770b\u771f\u56e0,"
                          "\u4e0d\u8981\u5bf9\u9519\u8bef\u7801\u786c\u91cd\u8bd5\u3002")
        if pages.get("alert"):
            lines.append("- \u5217\u8868/\u9996\u9875\u5f02\u5e38: \u68c0\u67e5 D1 \u662f\u5426\u53ef\u8fbe\u3001Worker \u662f\u5426\u62a5\u9519; wrangler tail \u770b\u771f\u56e0\u3002")
        if ai_search.get("alert"):
            lines.append("- AI\u5bfb\u8109\u5f02\u5e38: \u68c0\u67e5\u4e91\u7aef\u63a8\u7406\u5f15\u64ce/\u6a21\u578b\u6c60\u662f\u5426\u53ef\u7528, guest_gate \u7684 KV \u7ed1\u5b9a\u662f\u5426\u6b63\u5e38\u3002")
        if graph.get("alert"):
            lines.append("- \u661f\u56fe\u5f02\u5e38: \u68c0\u67e5 sue_graph_nodes/sue_graph_edges \u662f\u5426\u88ab\u8bef\u5220\u6216 quality_flag \u88ab\u8bef\u6807\u3002")
        if freshness.get("alert"):
            lines.append("- \u5165\u5e93\u7ba1\u7ebf\u53ef\u80fd\u505c\u6446: \u68c0\u67e5\u4e0b\u8f7d\u7ebf sync.yml/ocr.yml \u7b49 workflow \u7684 cron \u662f\u5426\u88ab\u53d6\u6d88\u6216\u8fde\u7eed\u5931\u8d25,"
                          "\u5bf9\u7167 fleet-watch.yml \u7684\u300c\u8230\u961f\u5de1\u67e5\u65e5\u62a5\u300dIssue \u4ea4\u53c9\u6838\u5b9e\u3002")

    lines.append("")
    lines.append("*by frontend_sentinel · \u53ea\u8bfb gufangai.com \u516c\u5f00\u7aef\u70b9 · \u4e0d\u5199 D1 · \u4e0d\u6539\u751f\u4ea7\u4ee3\u7801*")
    return "\n".join(lines)


# ───────────────────────── main ─────────────────────────

def main():
    # defensive: force UTF-8 stdout/stderr so emoji markers never crash the run
    # on a runner whose default locale isn't UTF-8 (GH Actions ubuntu-latest is
    # UTF-8 by default, this is just a cheap safety net).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    now = bj_now()
    prev_state = load_state()

    print("checking cover real-image ratio ...", file=sys.stderr)
    cover = check_cover()
    print(f"  real_ratio={cover.get('real_ratio')} alert={cover.get('alert')}", file=sys.stderr)

    print("checking page availability ...", file=sys.stderr)
    pages = check_pages()
    print(f"  alert={pages.get('alert')}", file=sys.stderr)

    print("checking ai search ...", file=sys.stderr)
    ai_search = check_ai_search()
    print(f"  status={ai_search.get('status')} alert={ai_search.get('alert')}", file=sys.stderr)

    print("checking graph ...", file=sys.stderr)
    graph = check_graph(prev_state)
    print(f"  nodes={graph.get('nodes')} links={graph.get('links')} alert={graph.get('alert')}", file=sys.stderr)

    print("checking d1 freshness ...", file=sys.stderr)
    freshness = check_freshness()
    print(f"  age_days={freshness.get('age_days')} alert={freshness.get('alert')}", file=sys.stderr)

    alerts = []
    if cover.get("alert"):
        alerts.append("cover")
    for k, d in pages["targets"].items():
        if d["alert"]:
            alerts.append(f"page:{k}")
    if ai_search.get("alert"):
        alerts.append("ai_search")
    if graph.get("alert"):
        alerts.append("graph")
    if freshness.get("alert"):
        alerts.append("freshness")
    alert_count = len(alerts)

    report = build_report_markdown(cover, pages, ai_search, graph, freshness, prev_state, now, alert_count)
    print(report)

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")
    record = {
        "checked_at_bj": now.strftime("%Y-%m-%d %H:%M:%S"),
        "alert_count": alert_count,
        "alerts": alerts,
        "cover": cover,
        "pages": pages,
        "ai_search": ai_search,
        "graph": graph,
        "freshness": freshness,
    }
    try:
        append_daily(date_str, record)
    except Exception as e:
        print(f"WARN append_daily failed: {e}", file=sys.stderr)

    new_state = {
        "checked_at_bj": now.strftime("%Y-%m-%d %H:%M:%S"),
        "cover_real_ratio": cover.get("real_ratio"),
        "graph_nodes": graph.get("nodes"),
        "graph_links": graph.get("links"),
        "d1_age_days": freshness.get("age_days"),
    }
    try:
        save_state(new_state)
    except Exception as e:
        print(f"WARN save_state failed: {e}", file=sys.stderr)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(report + "\n")
        except Exception as e:
            print(f"WARN write GITHUB_STEP_SUMMARY failed: {e}", file=sys.stderr)

    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"alert_count={alert_count}\n")
            f.write(f"today_bj={date_str}\n")
            f.write(f"time_bj={time_str}\n")
            f.write(f"report_body<<SENTINEL_REPORT_EOF\n{report}\nSENTINEL_REPORT_EOF\n")

    print(f"\nDONE alert_count={alert_count} alerts={alerts}", file=sys.stderr)


if __name__ == "__main__":
    main()
