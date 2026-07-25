# -*- coding: utf-8 -*-
"""SueAI council -- eagle-eye -> race -> mutual critique -> verdict -> founder.

The problem this exists to fix: intel-radar already sees the platform's own
gaps (its self-inspection section measures xunmai recall, star-graph nodes,
daodu coverage, funnel events straight off D1) -- but it can only WRITE AN
ISSUE. Everything after that needed a human to read it and hand out work, so
the moment nobody was reading, self-improvement stopped. This pipeline is the
missing middle: it takes those same numbers and drives many vendors' models
through a structured argument, ending in something the founder can tick.

Five stages, each auditable on its own:

  1 triage    -- NO LLM. Ranks live D1 metrics by (shortfall x organ weight)
                 and takes the top 3. Deterministic on purpose: "why these
                 three" must be recomputable by hand from the printed table,
                 not a thing only a model knows.
  2 race      -- 4 seats from 4 different model houses answer the SAME prompt
                 IN PARALLEL AND BLIND. No seat ever sees another's text at
                 this stage; that is the whole anti-convergence mechanism.
                 A plan with no acceptance-testable number is scrapped, and
                 the report says which and why.
  3 critique  -- every seat attacks the OTHER seats' plans (never its own),
                 under a prompt that mandates suspicion-by-default and a
                 concrete failure scenario. Plans are anonymised to letters so
                 critics grade the argument, not the brand.
  4 verdict   -- an independent referee (a house that is NOT racing) picks a
                 winner. Its narrative is NOT trusted to carry the dissent:
                 the veto table is regex-counted from the raw critiques by
                 this script, printed as hard fact, and if the referee's text
                 skips a veto the script appends it with a visible note. That
                 is the structural answer to "AI committees congratulate each
                 other into consensus".
  5 report    -- one GitHub Issue + reports/council/<date>.md, ending in a
                 checkbox list. Nothing is executed.

RED LINE: advisory only. D1 access is SELECT-guarded at the transport layer
(probes.d1_query), and there is no code path here that writes a table, edits a
file outside reports/council/, or deploys anything.
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm_roster as roster          # noqa: E402
import probes                        # noqa: E402
from zh import t                     # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LETTERS = "ABCD EFGH".replace(" ", "")

# CJK punctuation the parsers must match, written as escapes so this source
# stays ASCII (public-repo convention). Models decorate headings
# inconsistently, so every parser accepts halfwidth and fullwidth forms.
CJK_LB = "\u3010"        # lenticular bracket -- models wrap section names in it
CJK_RB = "\u3011"
CJK_COLON = "\uff1a"     # fullwidth colon
ARROW_CHARS = "\u2192\u21d2"   # fullwidth -> and =>
CJK_TO = "\u81f3"        # "to", as in "2% to 8%"
T0 = time.time()


def log(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


def hhmmss(sec):
    m, s = divmod(int(sec), 60)
    return "%dm%02ds" % (m, s) if m else "%ds" % s


# ---------------------------------------------------------------------------
# stage 1 -- triage
# ---------------------------------------------------------------------------
def triage(metrics, n_topics, manual_topic=""):
    scored = sorted(
        ({"m": m, "sev": probes.severity(m)} for m in metrics),
        key=lambda x: -x["sev"],
    )
    topics = []
    if manual_topic.strip():
        topics.append({
            "organ": t("lbl_manual_topic"),
            "title": t("topic_manual_tpl", txt=manual_topic.strip()),
            "current": (scored[0]["m"]["display"] if scored else "-"),
            "target": "-",
            "basis": t("lbl_manual_topic"),
            "sev": 9.99,
            "sql": "",
        })
    for row in scored:
        if len(topics) >= n_topics:
            break
        m, sev = row["m"], row["sev"]
        if sev <= 0.15:            # inside threshold: not a real gap, don't pad
            continue
        topics.append({
            "organ": m["organ_label"],
            "title": t("topic_tpl", label=m["label"],
                       cur=probes.fmt_value(m), tgt=probes.fmt_target(m)),
            "current": m["display"],
            "target": probes.fmt_target(m),
            "basis": t("basis_tpl", tgt=probes.fmt_target(m), sev="%.3f" % sev),
            "sev": sev,
            "sql": m["sql"],
        })
    return topics


def metrics_block(metrics):
    rows = [t("rpt_metrics_tbl_head")]
    for m in metrics:
        gap = t("lbl_gap") if probes.is_gap(m) else t("lbl_healthy")
        rows.append("| %s | %s | %s | %s | %s |" % (
            m["organ_label"], m["label"], m["display"], probes.fmt_target(m), gap))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# stage 2 -- race
# ---------------------------------------------------------------------------
_SEC_RE_CACHE = {}


def section(text, name_key, nxt_keys):
    """Pull one '### <heading>' block out of a model's answer. Models drift on
    the exact markdown level (###, ##, **bold**, or bare), so the match is on
    the heading WORD with optional decoration, and the block ends at the next
    known heading."""
    name = t(name_key)
    stops = "|".join(re.escape(t(k)) for k in nxt_keys)
    key = (name, stops)
    if key not in _SEC_RE_CACHE:
        # start = decorated heading + optional colon; stop = the mere
        # appearance of the next known heading word, decoration optional.
        # Requiring a newline after the stop word used to swallow whole
        # answers written as '**heading**: text on the same line'.
        deco = r"[#*\s" + CJK_LB + r"\[]*"
        tail = r"[" + CJK_RB + r"\]\s*:" + CJK_COLON + r"*#]*"
        _SEC_RE_CACHE[key] = re.compile(
            deco + re.escape(name) + tail + r"\n?(.*?)(?="
            + deco + r"(?:" + stops + r")|$)", re.DOTALL)
    m = _SEC_RE_CACHE[key].search(text or "")
    return (m.group(1).strip() if m else "").strip()


# an acceptance-testable number = an explicit before->after transition.
_ARROW_NUM = re.compile(
    r"[0-9][0-9,\.]*\s*%?\s*(?:->|=>|[" + ARROW_CHARS + CJK_TO + r"])\s*[0-9]")
_TWO_NUMS = re.compile(r"[0-9][0-9,\.]*\s*%")


def has_acceptance_number(numbers_section, whole):
    hay = numbers_section or whole or ""
    if _ARROW_NUM.search(hay):
        return True
    # a plan that states two percentages in the numbers section (e.g. "from
    # 2.07% to 8%" written without an arrow) still counts; anything vaguer
    # does not.
    return len(_TWO_NUMS.findall(numbers_section or "")) >= 2


ALL_SECS = ["sec_where", "sec_numbers", "sec_risk", "sec_effort"]


def race(topic, racers, metrics_md, radar_md):
    user = t("race_user_tpl", title=topic["title"], current=topic["current"],
             target=topic["target"], basis=topic["basis"],
             metrics_block=metrics_md, radar_block=radar_md,
             platform_ctx=t("platform_ctx"))
    sysmsg = t("race_sys")

    def one(seat):
        t1 = time.time()
        try:
            txt = roster.call(seat, sysmsg, user, max_tokens=1400, temperature=0.65,
                              stage="race", tag=topic["organ"])
            return {"seat": seat, "text": txt, "sec": round(time.time() - t1, 1), "err": ""}
        except Exception as e:
            return {"seat": seat, "text": "", "sec": round(time.time() - t1, 1),
                    "err": "%s:%s" % (type(e).__name__, str(e)[:100])}

    with ThreadPoolExecutor(max_workers=max(1, len(racers))) as ex:
        plans = list(ex.map(one, racers))     # blind + parallel: the anti-groupthink core

    for i, p in enumerate(plans):
        p["letter"] = LETTERS[i]
        p["where"] = section(p["text"], "sec_where", ["sec_numbers", "sec_risk", "sec_effort"])
        p["numbers"] = section(p["text"], "sec_numbers", ["sec_risk", "sec_effort"])
        p["risk"] = section(p["text"], "sec_risk", ["sec_effort"])
        p["effort"] = section(p["text"], "sec_effort", ["sec_where"])
        p["ok"] = bool(p["text"].strip())
        p["numbered"] = p["ok"] and has_acceptance_number(p["numbers"], p["text"])
        p["scrapped"] = p["ok"] and not p["numbered"]
    return plans


# ---------------------------------------------------------------------------
# stage 3 -- critique
# ---------------------------------------------------------------------------
def _anon(plans, exclude_letter=None):
    out = []
    for p in plans:
        if not p["ok"] or p["letter"] == exclude_letter:
            continue
        body = p["text"].strip()
        out.append(t("plan_anon_tpl", letter=p["letter"], body=body[:3000]))
    return "\n\n".join(out)


_VERDICT_WORDS = None


def _verdict_re():
    global _VERDICT_WORDS
    if _VERDICT_WORDS is None:
        _VERDICT_WORDS = re.compile(
            (r"%s\s*[:" + CJK_COLON + r"]?\s*(%s)")
            % (re.escape(t("kw_conclusion")), t("verdict_words")))
    return _VERDICT_WORDS


def parse_critique(text, letters):
    """Split a critic's answer per plan letter and read off its verdict word.

    Deliberately regex, not LLM: the veto count is the one number in this
    pipeline that must not be re-narrated by a model, because 'summarise the
    critiques' is exactly the step where disagreement gets sanded off."""
    out = {}
    if not text:
        return out
    word = t("plan_word")
    marks = []
    for L in letters:
        pat = (r"[" + CJK_LB + r"\[\(*#\s]*%s\s*%s") % (re.escape(word), L)
        for m in re.finditer(pat, text):
            marks.append((m.start(), L))
    marks.sort()
    for i, (pos, L) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        chunk = text[pos:end]
        if L in out and len(chunk) < len(out[L]["chunk"]):
            continue
        vm = _verdict_re().search(chunk)
        verdict = vm.group(1) if vm else t("lbl_unknown")
        fatal = ""
        fm = re.search((r"%s\s*[:" + CJK_COLON + r"]?\s*(.+)")
                       % re.escape(t("kw_fatal")), chunk)
        if fm:
            fatal = fm.group(1).strip().split("\n")[0][:220]
        out[L] = {"verdict": verdict, "fatal": fatal, "chunk": chunk}
    return out


def parse_ranking(text, letters):
    """Read the mandatory trailing ordering line ("ranking: A > B > C").

    Verdict words alone stopped discriminating once every critic vetoed every
    plan, so this line is the part of a critique that still ranks something.
    Accepts any of > / >= / -> / spaces between the letters."""
    if not text:
        return []
    kw = t("kw_ranking")
    m = None
    for m2 in re.finditer(re.escape(kw) + r"\s*[:" + CJK_COLON + r"]?\s*([^\n]+)", text):
        m = m2                      # keep the LAST one: it is the summary line
    if not m:
        return []
    seq = re.findall(r"[A-Z]", m.group(1).upper())
    out = []
    for L in seq:
        if L in letters and L not in out:
            out.append(L)
    return out


def critique(topic, plans, racers):
    live = [p for p in plans if p["ok"]]
    letters = [p["letter"] for p in live]
    results = []

    def one(p_self):
        others = _anon(live, exclude_letter=p_self["letter"])
        if not others.strip():
            return None
        n_others = len([x for x in live if x["letter"] != p_self["letter"]])
        user = t("critique_user_tpl", title=topic["title"], current=topic["current"],
                 target=topic["target"], platform_ctx=t("platform_ctx"),
                 plans_block=others, n=n_others)
        try:
            txt = roster.call(p_self["seat"], t("critique_sys"), user,
                              max_tokens=1500, temperature=0.35,
                              stage="critique", tag=topic["organ"])
        except Exception as e:
            return {"critic": p_self["seat"], "letter": p_self["letter"], "text": "",
                    "verdicts": {}, "ranking": [], "flat": False,
                    "err": "%s:%s" % (type(e).__name__, str(e)[:90])}
        vd = parse_critique(txt, letters)
        return {"critic": p_self["seat"], "letter": p_self["letter"], "text": txt,
                "verdicts": vd, "ranking": parse_ranking(txt, letters),
                # a critic that gave every plan the same verdict ranked nothing;
                # the report says so rather than presenting it as a signal
                "flat": len({v["verdict"] for v in vd.values()}) <= 1 and len(vd) > 1,
                "err": ""}

    with ThreadPoolExecutor(max_workers=max(1, len(live))) as ex:
        for r in ex.map(one, live):
            if r:
                results.append(r)
    return results


def vote_matrix(plans, critiques):
    """{plan_letter: {critic_seat_id: verdict}} plus the veto list."""
    live = [p for p in plans if p["ok"]]
    matrix, vetoes = {}, []
    for p in live:
        matrix[p["letter"]] = {}
        for c in critiques:
            if c["letter"] == p["letter"]:
                continue
            v = c["verdicts"].get(p["letter"])
            verdict = v["verdict"] if v else t("lbl_unknown")
            matrix[p["letter"]][c["critic"]["id"]] = verdict
            if verdict == t("lbl_veto"):
                vetoes.append({"critic": c["critic"], "letter": p["letter"],
                               "reason": (v or {}).get("fatal", "") or "-"})
    return matrix, vetoes


def matrix_md(plans, critiques, matrix):
    live = [p for p in plans if p["ok"]]
    critic_ids = [c["critic"]["id"] for c in critiques]
    head = "| " + t("plan_word") + " | " + " | ".join(critic_ids) + " |"
    sep = "|---" * (len(critic_ids) + 1) + "|"
    rows = [head, sep]
    for p in live:
        cells = []
        for cid in critic_ids:
            cells.append(matrix.get(p["letter"], {}).get(cid, "-"))
        rows.append("| %s%s | %s |" % (t("plan_word"), p["letter"], " | ".join(cells)))
    rank_cells = []
    for c in critiques:
        r = " > ".join(c.get("ranking") or []) or "-"
        if c.get("flat"):
            r += " " + t("lbl_no_discrimination")
        rank_cells.append(r)
    rows.append(t("rpt_rank_row", cells=" | ".join(rank_cells)))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# stage 4 -- verdict
# ---------------------------------------------------------------------------
def verdict(topic, plans, critiques, matrix_text, judge):
    live = [p for p in plans if p["ok"]]
    crit_block = "\n\n".join(
        "[%s]\n%s" % (c["critic"]["id"], (c["text"] or "")[:2200]) for c in critiques if c["text"])
    user = t("verdict_user_tpl", title=topic["title"], current=topic["current"],
             target=topic["target"], plans_block=_anon(live),
             critiques_block=crit_block or "-", matrix_block=matrix_text)
    try:
        return roster.call(judge, t("verdict_sys"), user, max_tokens=1800,
                           temperature=0.3, stage="verdict", tag=topic["organ"]), ""
    except Exception as e:
        return "", "%s:%s" % (type(e).__name__, str(e)[:110])


def dissent_md(vetoes, verdict_text):
    """ALWAYS renders the machine-counted vetoes. If the referee's own text
    failed to carry one, that is stated in the open rather than quietly fixed."""
    if not vetoes:
        return t("rpt_dissent_none"), False
    lines = [t("rpt_dissent_intro"), ""]
    for v in vetoes:
        lines.append(t("dissent_line_tpl", critic=v["critic"]["id"],
                       letter=v["letter"], reason=v["reason"]))
    covered = t("sec_dissent") in (verdict_text or "")
    if not covered:
        lines.append(t("dissent_appended"))
    return "\n".join(lines), (not covered)


# ---------------------------------------------------------------------------
# stage 5 -- report
# ---------------------------------------------------------------------------
def seats_md(bench_report):
    rows = [t("rpt_seats_tbl_head")]
    role_lbl = {"racer": t("lbl_racer"), "judge": t("lbl_judge"), "bench": t("lbl_bench")}
    for s in bench_report:
        alive = t("lbl_alive") if s.get("alive") else (
            "-" if s.get("alive") is None else t("lbl_dead"))
        rows.append("| `%s` | %s | `%s` | %s | %s |" % (
            s["id"], s.get("vendor", "-"), s.get("model", "-"),
            role_lbl.get(s.get("role"), s.get("role", "")), alive))
    return "\n".join(rows)


def race_table(plans):
    rows = [t("rpt_race_tbl_head")]
    for p in plans:
        if not p["ok"]:
            rows.append("| %s%s | %s | %s | - | - | %ss |" % (
                t("plan_word"), p["letter"], p["seat"]["vendor"],
                t("rpt_race_fail", err=p["err"][:60]), p["sec"]))
            continue
        where = (p["where"] or p["text"])[:90].replace("\n", " ").replace("|", "/")
        nums = (p["numbers"] or "-")[:90].replace("\n", " ").replace("|", "/")
        flag = t("lbl_number_ok") if p["numbered"] else t("lbl_scrapped")
        rows.append("| %s%s | %s | %s | %s | %s | %ss |" % (
            t("plan_word"), p["letter"], p["seat"]["vendor"], where, nums, flag, p["sec"]))
    return "\n".join(rows)


def build_report(date_str, topics, results, metrics, skipped, bench_report,
                 radar, judge, judge_independent, stage_times, run_id, run_url):
    snap = roster.budget_snapshot()
    seats = ", ".join(sorted({p["seat"]["vendor"] for r in results
                              for p in r["plans"] if p["ok"]})) or "-"
    dist = ", ".join("%s=%d" % (k, v) for k, v in sorted(
        _call_dist().items(), key=lambda kv: -kv[1]))
    out = [
        t("rpt_h1", date=date_str), "",
        t("rpt_lead", seats=seats, calls=snap["calls"], cap=roster.MAX_CALLS,
          elapsed=hhmmss(time.time() - T0), run_id=run_id, run_url=run_url), "",
        t("rpt_stage_hdr", t1=hhmmss(stage_times.get("triage", 0)),
          t2=hhmmss(stage_times.get("race", 0)), t3=hhmmss(stage_times.get("critique", 0)),
          t4=hhmmss(stage_times.get("verdict", 0))), "",
        t("rpt_redline"), "",
        t("rpt_h2_triage", n=len(topics)), "",
        t("rpt_triage_intro", m=len(metrics)), "",
        t("rpt_triage_tbl_head"),
    ]
    for i, tp in enumerate(topics, 1):
        out.append("| %d | %s | %s | %s | %s | %.3f |" % (
            i, tp["organ"], tp["title"], tp["current"], tp["target"], tp["sev"]))
    out += ["", t("rpt_h3_metrics"), "", metrics_block(metrics), ""]
    if skipped:
        out += [t("rpt_skipped", items="; ".join(skipped)), ""]
    if radar[0]:
        out += [t("rpt_radar_h"), "", t("lbl_radar_from", n=radar[0], date=radar[1]), "",
                "```\n%s\n```" % radar[2][:1500], ""]
    else:
        out += [t("rpt_radar_h"), "", t("lbl_no_radar"), ""]

    for i, r in enumerate(results, 1):
        tp = r["topic"]
        out += ["---", "", t("rpt_topic_h", i=i, organ=tp["organ"], title=tp["title"]), "",
                t("rpt_topic_meta", cur=tp["current"], tgt=tp["target"],
                  sev="%.3f" % tp["sev"], sql=(tp["sql"] or "-")[:150]), "",
                t("rpt_h2_race", n=len([p for p in r["plans"] if p["ok"]])), "",
                t("rpt_race_intro"), "", race_table(r["plans"]), ""]
        for p in r["plans"]:
            if p["ok"]:
                out += [t("rpt_plan_full_h", letter=p["letter"], vendor=p["seat"]["vendor"],
                          body=p["text"][:4000]), ""]
        out += [t("rpt_h2_critique"), "",
                t("rpt_critique_intro", n=max(0, len([p for p in r["plans"] if p["ok"]]) - 1)), "",
                t("rpt_matrix_head"), "", r["matrix_md"], "",
                t("rpt_matrix_note"), "", t("rpt_rank_note"), ""]
        for c in r["critiques"]:
            if c["text"]:
                out += [t("rpt_critique_full_h", critic=c["critic"]["id"],
                          body=c["text"][:3500]), ""]
        out += [t("rpt_h2_verdict"), "",
                (t("rpt_verdict_by", judge=judge["id"]) if judge_independent
                 else t("rpt_verdict_by_fallback", judge=judge["id"])), "",
                (r["verdict"] or ("`%s`" % r["verdict_err"])), "",
                t("rpt_h2_dissent"), "", r["dissent"], ""]

    out += ["---", "", t("rpt_h2_checklist"), "", t("rpt_checklist_intro"), ""]
    for i, r in enumerate(results, 1):
        first = section(r["verdict"], "sec_firststep", ["sec_recommend"]) or t("act_default")
        acc = section(r["verdict"], "sec_accept", ["sec_firststep"]) or t("acc_default")
        out.append(t("checklist_tpl", i=i, organ=r["topic"]["organ"],
                     action=first.replace("\n", " ")[:300],
                     accept=acc.replace("\n", " ")[:300]))
    out += ["", t("rpt_h2_seats"), "", seats_md(bench_report), "",
            t("rpt_h2_usage"), "",
            t("usage_tpl", calls=snap["calls"], cap=roster.MAX_CALLS, fails=snap["fails"],
              dist=dist or "-", neurons=snap["neurons"], nbudget=int(roster.CF_NEURON_BUDGET),
              elapsed=hhmmss(time.time() - T0)), "",
            t("rpt_footer")]
    return "\n".join(out)


def _call_dist():
    d = {}
    for c in roster.CALL_LOG:
        if c["stage"] == "probe":
            continue
        d[c["seat"]] = d.get(c["seat"], 0) + 1
    return d


def create_issue(title, body, date_str):
    tok = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med")
    if not tok:
        log("no GH token -- skipping issue creation")
        return None
    if len(body) > 60000:
        body = body[:60000] + t("lbl_truncated", date=date_str)
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/issues" % repo,
        data=json.dumps({"title": title, "body": body}).encode("utf-8"),
        headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "sueai-council"})
    d = json.load(urllib.request.urlopen(req, timeout=60))
    return d.get("number")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topics", type=int, default=int(os.environ.get("N_TOPICS", "3")))
    ap.add_argument("--racers", type=int, default=int(os.environ.get("N_RACERS", "4")))
    ap.add_argument("--topic", default=os.environ.get("MANUAL_TOPIC", ""))
    ap.add_argument("--dry-run", action="store_true", help="run everything, create no Issue")
    args = ap.parse_args()

    date_str = time.strftime("%Y-%m-%d")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_url = "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                         os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med"),
                                         run_id)
    st = {}

    # --- stage 1 -----------------------------------------------------------
    t1 = time.time()
    log("stage1 triage: probing D1 ...")
    metrics, skipped = probes.collect_metrics()
    log("  metrics=%d skipped=%d" % (len(metrics), len(skipped)))
    for s in skipped:
        log("  SKIPPED %s" % s)
    radar = probes.fetch_radar_selfcheck()
    log("  eagle-eye issue: %s" % (("#%s (%s)" % (radar[0], radar[1])) if radar[0] else "none"))
    topics = triage(metrics, args.topics, args.topic)
    st["triage"] = time.time() - t1
    if not topics:
        log("no topic above threshold -- nothing to convene about")
        print(t("rpt_no_topic"))
        return 0
    for i, tp in enumerate(topics, 1):
        log("  topic%d [%.3f] %s" % (i, tp["sev"], tp["title"]))

    # --- seats -------------------------------------------------------------
    log("assembling seats ...")
    racers, judge, bench_report = roster.assemble(args.racers)
    if len(racers) < 2 or judge is None:
        log("FATAL: only %d live racer(s), judge=%s -- a council needs a real argument"
            % (len(racers), judge and judge["id"]))
        return 2
    judge_independent = judge["house"] not in {r["house"] for r in racers}
    log("  racers=%s judge=%s independent=%s"
        % ([r["id"] for r in racers], judge["id"], judge_independent))

    md_metrics = metrics_block(metrics)
    md_radar = (t("lbl_radar_from", n=radar[0], date=radar[1]) + "\n" + radar[2]) if radar[0] \
        else t("lbl_no_radar")

    results = []
    st["race"] = st["critique"] = st["verdict"] = 0.0
    for i, tp in enumerate(topics, 1):
        log("topic%d/%d: %s" % (i, len(topics), tp["title"]))
        try:
            t2 = time.time()
            plans = race(tp, racers, md_metrics, md_radar)
            st["race"] += time.time() - t2
            log("  race done: ok=%d scrapped=%d"
                % (len([p for p in plans if p["ok"]]), len([p for p in plans if p["scrapped"]])))

            t3 = time.time()
            crits = critique(tp, plans, racers)
            st["critique"] += time.time() - t3
            matrix, vetoes = vote_matrix(plans, crits)
            mmd = matrix_md(plans, crits, matrix)
            log("  critique done: critics=%d vetoes=%d" % (len(crits), len(vetoes)))

            t4 = time.time()
            vtext, verr = verdict(tp, plans, crits, mmd, judge)
            st["verdict"] += time.time() - t4
            dmd, appended = dissent_md(vetoes, vtext)
            if appended and vetoes:
                log("  NOTE: referee text omitted the dissent section -- machine-appended")
        except roster.BudgetExhausted as e:
            log("  BUDGET STOP: %s -- reporting what is already in hand" % e)
            break
        except Exception as e:
            # one topic blowing up must not cost the founder the other two
            log("  topic FAILED, skipping: %s: %s" % (type(e).__name__, str(e)[:140]))
            continue
        results.append({"topic": tp, "plans": plans, "critiques": crits,
                        "matrix_md": mmd, "verdict": vtext, "verdict_err": verr,
                        "dissent": dmd, "vetoes": vetoes})

    if not results:
        log("FATAL: no topic completed")
        return 3

    # --- stage 5 -----------------------------------------------------------
    body = build_report(date_str, topics, results, metrics, skipped, bench_report,
                        radar, judge, judge_independent, st, run_id, run_url)
    outdir = os.path.join("reports", "council")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "%s.md" % date_str)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    log("wrote %s (%d bytes)" % (path, len(body.encode("utf-8"))))

    title = t("issue_title_prefix") + date_str
    if args.dry_run:
        log("dry-run: no Issue created")
    else:
        try:
            num = create_issue(title, body, date_str)
            log("issue #%s created" % num)
        except Exception as e:
            log("issue creation FAILED: %s: %s" % (type(e).__name__, str(e)[:200]))

    snap = roster.budget_snapshot()
    log("DONE calls=%d fails=%d neurons=%.0f elapsed=%s"
        % (snap["calls"], snap["fails"], snap["neurons"], hhmmss(time.time() - T0)))
    with io.open("council_usage.json", "w", encoding="utf-8") as f:
        json.dump({"calls": snap["calls"], "fails": snap["fails"],
                   "neurons": round(snap["neurons"], 2),
                   "elapsed_sec": round(time.time() - T0, 1),
                   "log": roster.CALL_LOG}, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
