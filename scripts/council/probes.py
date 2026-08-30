# -*- coding: utf-8 -*-
"""Council stage-1 inputs: live D1 metrics + the latest eagle-eye (intel-radar)
Issue's self-inspection section.

Two hard rails live in this file, because everything downstream trusts them:

  1. `d1_query` refuses any statement that is not a single SELECT. The council
     is advisory-only -- it must be structurally impossible for a prompt-shaped
     accident (or a model-suggested "just run this UPDATE") to reach production
     data. The guard is on the transport, not on the caller's good intentions.
  2. Every metric carries its own SQL, its measured value AND its target, so a
     topic can always be phrased as "current number -> target number". A metric
     that fails to query degrades to skipped (never to a fabricated 0) -- a
     silent 0 would look like a catastrophic gap and would hijack the triage.

Metric targets are deliberately plain constants rather than model-chosen: the
triage ranking has to be recomputable by hand from the report, otherwise "why
these 3 topics" becomes another thing only an LLM knows.
"""
import json
import os
import re
import urllib.error
import urllib.request

from zh import t

D1_API = "https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"

# one leading SELECT, and no statement separator that could smuggle a second
# statement in behind it (D1's /query does accept multi-statement bodies)
_SELECT_ONLY = re.compile(r"^\s*SELECT\s", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|PRAGMA|VACUUM|BEGIN|COMMIT)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(RuntimeError):
    pass


def d1_query(sql):
    """Run one read-only SELECT against guyaofang-db. Raises on anything else."""
    if not _SELECT_ONLY.match(sql) or _FORBIDDEN.search(sql) or ";" in sql.strip().rstrip(";"):
        raise ReadOnlyViolation("council is advisory-only: refused non-SELECT sql: %s" % sql[:80])
    acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    db = os.environ.get("D1_DATABASE_ID", "").strip()
    tok = os.environ.get("D1_API_TOKEN", "").strip()
    if not (acc and db and tok):
        raise RuntimeError("D1 env triple missing (CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN)")
    req = urllib.request.Request(
        D1_API.format(acc=acc, db=db),
        data=json.dumps({"sql": sql}).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
    )
    d = json.load(urllib.request.urlopen(req, timeout=45))
    if not d.get("success"):
        raise RuntimeError(str(d.get("errors"))[:160])
    return d["result"][0]["results"]


def _scalar(sql, default=0):
    r = d1_query(sql)
    if not r:
        return default
    v = list(r[0].values())[0]
    return default if v is None else v


# ---------------------------------------------------------------------------
# metric definitions
# ---------------------------------------------------------------------------
# organ weights: the founder's 2026-07-25 strategic turn is "must start earning"
# (marketing/monetize pivot), so conversion outranks corpus breadth. Weights are
# printed in the report so the ranking stays arguable rather than magic.
ORGAN_WEIGHT = {
    "convert": 1.30,
    "xunmai": 1.15,
    "daodu": 1.00,
    "graph": 0.90,
    "corpus": 0.70,
}
ORGAN_LABEL = {
    "convert": "org_convert",
    "xunmai": "org_xunmai",
    "daodu": "org_daodu",
    "graph": "org_graph",
    "corpus": "org_corpus",
}


def _m(key, organ, label_key, value, target, display, unit, higher_is_better=True, sql=""):
    return {
        "key": key,
        "organ": organ,
        "organ_label": t(ORGAN_LABEL[organ]),
        "label": t(label_key),
        "value": value,
        "target": target,
        "display": display,
        "unit": unit,
        "higher_is_better": higher_is_better,
        "sql": sql,
    }


def collect_metrics():
    """Probe every organ. Each probe is isolated: one failure never blanks the
    board and never fabricates a 0 (it is dropped, and reported as skipped)."""
    out, skipped = [], []

    def guard(fn):
        try:
            m = fn()
            if m:
                out.append(m)
        except Exception as e:
            skipped.append("%s: %s" % (fn.__name__, str(e)[:70]))

    def xunmai():
        sql = ("SELECT COUNT(*) total, "
               "SUM(CASE WHEN public_evidence_count>0 THEN 1 ELSE 0 END) with_ev "
               "FROM sue_call_logs WHERE created_at >= strftime('%s','now','-7 days')")
        r = d1_query(sql)[0]
        total, with_ev = r.get("total") or 0, r.get("with_ev") or 0
        pct = (with_ev * 100.0 / total) if total else 0.0
        return _m("xunmai_recall", "xunmai", "m_xunmai_recall", round(pct, 1), 90.0,
                  "%s/%s(%.1f%%)" % (with_ev, total, pct), "%", sql=sql)

    def xunmai_vol():
        sql = ("SELECT COUNT(*) c FROM sue_call_logs "
               "WHERE created_at >= strftime('%s','now','-7 days')")
        n = _scalar(sql)
        return _m("xunmai_vol", "xunmai", "m_xunmai_vol", float(n), 100.0, str(n), t("u_times"), sql=sql)

    def graph_approved():
        sql = "SELECT COUNT(*) c FROM sue_graph_candidates WHERE review_status='approved'"
        n = _scalar(sql)
        return _m("graph_approved", "graph", "m_graph_approved", float(n), 3000.0, str(n), t("u_ge"), sql=sql)

    def graph_pending():
        # real backlog only: llm-rejected rows stay in the table for audit and
        # once inflated this number to 365 -- excluded here on purpose.
        sql = ("SELECT COUNT(*) c FROM sue_graph_candidates WHERE review_status='pending' "
               "AND (llm_verdict IS NULL OR llm_verdict='accept')")
        n = _scalar(sql)
        return _m("graph_pending", "graph", "m_graph_pending", float(n), 200.0, str(n), t("u_ge"),
                  higher_is_better=False, sql=sql)

    def graph_edges():
        sql = "SELECT COUNT(*) c FROM sue_graph_edges"
        n = _scalar(sql)
        return _m("graph_edges", "graph", "m_graph_edges", float(n), 8000.0, str(n), t("u_tiao"), sql=sql)

    def daodu_cov():
        sql_a = "SELECT COUNT(DISTINCT book_id) c FROM book_daodu_ai WHERE status='visible'"
        sql_b = "SELECT COUNT(*) c FROM books_assets_v2 WHERE frontend_visible=1"
        have, total = _scalar(sql_a), _scalar(sql_b)
        pct = (have * 100.0 / total) if total else 0.0
        return _m("daodu_cov", "daodu", "m_daodu_cov", round(pct, 2), 8.0,
                  "%s/%s(%.2f%%)" % (have, total, pct), "%", sql=sql_a + " | " + sql_b)

    def funnel():
        sql = "SELECT COUNT(DISTINCT event_name) c FROM events"
        n = _scalar(sql)
        detail = d1_query("SELECT event_name k, COUNT(*) c FROM events "
                          "GROUP BY event_name ORDER BY c DESC LIMIT 8")
        txt = ", ".join("%s=%s" % (x["k"], x["c"]) for x in detail) or "-"
        return _m("funnel", "convert", "m_funnel", float(n), 5.0, "%s (%s)" % (n, txt), t("u_zhong"), sql=sql)

    def paid():
        sql = "SELECT COUNT(*) c FROM payments"
        n = _scalar(sql)
        subs = _scalar("SELECT COUNT(*) c FROM subscriptions")
        users = _scalar("SELECT COUNT(*) c FROM users")
        return _m("paid", "convert", "m_paid", float(n), 10.0,
                  t("paid_display", a=n, b=subs, c=users), t("u_dan"), sql=sql)

    def formulas():
        # Target set by the founder on 2026-07-25: one million structured
        # formulas, "at least". The previous 10000 was not a goal, it was the
        # size of the pilot dressed up as one -- and a target the pipeline can
        # clear by accident tells the council nothing. Expect this metric to
        # read as a deep shortfall until the mining corpus itself grows; that
        # is the point.
        sql = "SELECT COUNT(*) c FROM sue_formulas"
        n = _scalar(sql)
        return _m("formula", "corpus", "m_formula", float(n), 1000000.0, str(n), t("u_shou"), sql=sql)

    def cases():
        sql = "SELECT COUNT(*) c FROM case_extractions"
        n = _scalar(sql)
        return _m("case", "corpus", "m_case", float(n), 2000.0, str(n), t("u_li"), sql=sql)

    for fn in (xunmai, xunmai_vol, graph_approved, graph_pending, graph_edges,
               daodu_cov, funnel, paid, formulas, cases):
        guard(fn)
    return out, skipped


def severity(m):
    """0..1 shortfall against target, times the organ weight. Deterministic and
    hand-recomputable from the numbers printed in the report."""
    v, tgt = float(m["value"]), float(m["target"])
    if m["higher_is_better"]:
        raw = 0.0 if tgt <= 0 else max(0.0, (tgt - v) / tgt)
    else:
        # over-target is the bad direction; saturate at 3x target
        raw = 0.0 if v <= tgt else min(1.0, (v - tgt) / max(tgt * 2.0, 1.0))
    return round(raw * ORGAN_WEIGHT.get(m["organ"], 1.0), 4)


def is_gap(m):
    return severity(m) > 0.15


def fmt_target(m):
    if m["unit"] == "%":
        return "%.4g%%" % m["target"]
    return "%.4g%s" % (m["target"], m["unit"])


def fmt_value(m):
    if m["unit"] == "%":
        return "%.4g%%" % m["value"]
    return "%.4g%s" % (m["value"], m["unit"])


# ---------------------------------------------------------------------------
# eagle-eye issue
# ---------------------------------------------------------------------------
_RADAR_PREFIX = t("radar_prefix")     # "[intel radar" issue title prefix
_SELF_HEAD = t("radar_selfhead")      # "self-inspection" section head


def _gh(path):
    tok = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "hosonzuo8848/sync-med")
    req = urllib.request.Request(
        "https://api.github.com/repos/%s%s" % (repo, path),
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "sueai-council"},
    )
    return json.load(urllib.request.urlopen(req, timeout=30))


def fetch_radar_selfcheck(max_scan=30):
    """Return (issue_number, date, section_markdown) of the newest intel-radar
    Issue that actually carries a self-inspection section, else (None,None,'').
    Scans a window rather than only the newest issue because the radar
    occasionally posts a degraded run without the section."""
    try:
        items = _gh("/issues?state=all&per_page=%d&sort=created&direction=desc" % max_scan)
    except Exception:
        return None, None, ""
    for it in items:
        if "pull_request" in it:
            continue
        if not (it.get("title") or "").startswith(_RADAR_PREFIX):
            continue
        body = it.get("body") or ""
        if _SELF_HEAD not in body:
            continue
        lines, keep, buf = body.splitlines(), False, []
        for ln in lines:
            if ln.startswith("## ") and _SELF_HEAD in ln:
                keep = True
                buf.append(ln)
                continue
            if keep and ln.startswith("## "):
                break
            if keep:
                buf.append(ln)
        sect = "\n".join(buf).strip()
        if sect:
            return it["number"], (it.get("created_at") or "")[:10], sect[:4000]
    return None, None, ""


_ARSENAL_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "reports", "arsenal", "candidates.json")


def fetch_arsenal_candidates(limit=12):
    """Read the radar's machine file -- the OUTSIDE world, as opposed to
    collect_metrics() which reads our own D1.

    Why this exists (founder, 2026-07-26): "what they should be doing is
    discovering new technology, new trends, things happening in the industry --
    not monetisation." Until now triage ranked topics purely off internal D1
    shortfalls, so four models spent every session arguing about our own order
    count while 1132 external findings sat unread one directory away. A council
    that only ever looks inward is an expensive mirror.

    Only distilled rows are returned: those carry capability / transfer_forms /
    line_candidates, which is what makes a repo arguable. An undistilled row is
    just a name and a star count -- nothing to hold a debate about.

    Returns [] on any failure. An unreadable intel file must degrade the council
    back to its old D1-only behaviour, never take the run down.
    """
    try:
        with open(_ARSENAL_JSON, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        print("  [arsenal] intel file unreadable (%s: %s) -- falling back to D1 only"
              % (type(e).__name__, str(e)[:80]), flush=True)
        return []
    rows = [r for r in (doc.get("candidates") or [])
            if r.get("distilled") and r.get("capability")]
    # Stamp each row with the scan date so a topic can cite when its intel was
    # gathered. Without it a stale candidates.json would silently present
    # week-old findings as today's.
    gen = str(doc.get("generated") or "")[:10]
    for r in rows:
        r["_generated"] = gen
    # new_to_us first, then by size. Anything already in the arsenal ledger has
    # been seen before and makes a poor headline topic.
    rows.sort(key=lambda r: (0 if r.get("new_to_us") else 1, -(r.get("stars") or 0)))
    return rows[:limit]
