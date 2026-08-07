# -*- coding: utf-8 -*-
"""Deep GitHub radar + objective distiller -- the eagle-eye's feed INTO the
SueAI council. Not a human-facing report.

WHY THIS EXISTS (founder, 2026-07-26): "our eagle eye is 10000-degree
short-sighted". Four repos he had to hand us MANUALLY, none of which the radar
had ever surfaced:

    yikart/AiToEarn                        24230 stars, topics=[auto-publish,
                                           douyin, xiaohongshu, kuaishou, ...]
    tashfeenahmed/freellmapi               16978 stars, topics=[]
    tmstack/awesome-persona-skills          3413 stars, topics=[]
    alchaincyf/zhangxuefeng-skill           9991 stars, topics=[]
    YouMind-OpenLab/ai-image-prompts-skill   528 stars, topics=[]

MEASURED ROOT CAUSE (probed against the live API on 2026-07-26, not guessed):
  1. FOUR OF THE FIVE CARRY NO TOPICS AT ALL. Every query in daily_report_v3
     (fetch_github_trending, fetch_github_freebies' topic half, and the whole
     blindspot radar) is `topic:`-driven, so those repos are not "ranked low",
     they are STRUCTURALLY INVISIBLE. Full-text `in:name` / `in:name,description`
     is therefore not a nice-to-have, it is the fix.
  2. `created:>7 days` in fetch_github_trending can only ever see brand-new
     repos. AiToEarn was created 2025-02-24. It could never appear.
  3. `per_page=50` + `sort=stars` buries the mid-star half of a result set. Two
     of the five sat inside the matched set and were cut off by the page size.
     per_page=100 is the same one request for double the coverage.
  4. `sort=stars` alone cannot see a small-but-moving repo. Mixing in
     `sort=updated` recovered ai-image-prompts-skill (528 stars).

  With those four corrections the scan plan below hits 5/5. Each query that
  proved a target is tagged `# PROVEN <repo>` and MUST NOT be "tidied away".

  5. THE PLAN ONLY SPOKE ENGLISH -- added 2026-07-27, founder: "it has to search
     Chinese and English, multilingual". The four fixes above are all about HOW
     we ask; this one is about WHAT LANGUAGE we ask in, and it is the same class
     of structural blindness. Until now every technical query was English, and
     the only CJK in the plan was domain vocabulary (medicine, classics). A tool
     named, described and documented in Chinese therefore could not be returned
     by any technical query we ran, no matter how popular it was -- measured:
     harry0703/MoneyPrinterTurbo, 99373 stars and squarely on our video line,
     had never once been surfaced. Its description is Chinese. The CJK technical
     block in scan_plan() is the fix, and it is not a translation of the English
     queries: the two halves return substantially different repos, which is the
     whole point of running both. Query wording IS the radar's eyesight, not
     configuration -- the same lesson as `stars:>100000` finding a 261k-star
     repo that every topic query missed.

WHAT THIS MODULE DELIBERATELY DOES NOT DO -- founder's correction, same day:
    "push it to SueAI, not to me ... free models, race, expert mechanism, they
     hold a discussion, decide what we actually need, then it comes to the CTO"
  So the distiller emits OBJECTIVE DESCRIPTION AND CANDIDATE OPTIONS ONLY. No
  "should we adopt", no score, no cost rating, no recommendation. That judgement
  belongs to the council (scripts/council/). A radar that pre-decides leaves the
  council nothing to argue about, which is exactly the failure mode being fixed.

COST: zero. GitHub search is free; distillation runs on the council's internal
FREE model bench (scripts/council/llm_roster.py -- imported READ-ONLY, never
modified). No Cloudflare Workers AI, no GitHub Models, no xunfei MaaS chat.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Every CJK literal in this module lives here as \uXXXX escapes, so the source
# file itself stays pure ASCII -- same public-repo convention as
# scripts/council/zh.py. Prompts and display labels only; never a credential.
ZH = {
    "LINE_HINT": "video=\u89c6\u9891\u4ea7\u7ebf(\u811a\u672c/\u5408\u6210/\u53d1\u5e03\u5206\u53d1); formula=\u914d\u65b9\u5e93/\u53e4\u65b9\u7ed3\u6784\u5316; image=\u53d6\u56fe/\u56fe\u50cf\u7d20\u6750\u4e0e\u63d0\u793a\u8bcd; modelpool=\u6a21\u578b\u6c60/\u514d\u8d39\u7b97\u529b/\u7f51\u5173\u8def\u7531; ocr=\u53e4\u7c4d\u533b\u4e66 OCR \u4e0e\u6587\u5b57\u5316; rag=RAG \u68c0\u7d22/AI \u5bfb\u8109/\u77e5\u8bc6\u56fe\u8c31; unknown=\u5bf9\u4e0d\u4e0a\u4efb\u4f55\u4e00\u6761\u7ebf",
    "P1": "\u4f60\u662f\u60c5\u62a5\u5f55\u5165\u5458\uff0c\u4e0d\u662f\u51b3\u7b56\u8005\u3002\u4e0b\u9762\u662f\u4e00\u6279 GitHub \u4ed3\u5e93\u3002\u4f60\u53ea\u505a\u4e24\u4ef6\u4e8b\uff1a(1) \u7528\u4e00\u53e5\u8bdd\u5ba2\u89c2\u63cf\u8ff0\u5b83\u80fd\u529b\u4e0a\u505a\u4ec0\u4e48\uff1b(2) \u5217\u51fa\u5019\u9009\u9879\u3002\n\n",
    "P2": "\u94c1\u5f8b\uff08\u8fdd\u53cd\u5373\u4f5c\u5e9f\uff09\uff1a\n- \u7edd\u4e0d\u5199\u201c\u503c\u5f97\u7528/\u4e0d\u503c\u5f97\u7528/\u5efa\u8bae/\u4f18\u52bf/\u63a8\u8350/\u4f18\u5148\u7ea7/\u6210\u672c\u9ad8\u4f4e\u201d\u4e4b\u7c7b\u5224\u65ad\u3002\u5224\u65ad\u7531\u8bae\u4e8b\u4f1a\u505a\uff0c\u4e0d\u662f\u4f60\u3002\n- \u53ea\u5199\u4ece\u63cf\u8ff0/\u8bdd\u9898\u91cc\u80fd\u770b\u51fa\u6765\u7684\u4e8b\u5b9e\uff0c\u770b\u4e0d\u51fa\u6765\u5c31\u5199 unknown\u3002\n- \u7edd\u4e0d\u786c\u51d1\u5bf9\u5e94\u7ebf\u3002\u5bf9\u4e0d\u4e0a\u5c31\u662f unknown\u3002\n\n",
    "P3": "\u6211\u4eec\u7684\u7ebf\uff1a%s\n\u53ef\u8fc1\u79fb\u5f62\u6001\u5019\u9009\uff1amcp(\u5b83\u81ea\u5e26 MCP \u670d\u52a1)\u3001skill(\u53ef\u5f53 skill \u88c5)\u3001architecture(\u53ea\u80fd\u501f\u67b6\u6784\u601d\u8def)\u3001deploy(\u53ef\u76f4\u63a5\u90e8\u7f72\u8dd1)\u3001dataset(\u4e3b\u4f53\u662f\u6570\u636e/\u63d0\u793a\u8bcd\u5e93)\u3001library(\u5f53\u5e93\u8c03\u7528)\u3001unknown\n\n",
    "P4": "\u4ed3\u5e93\u5217\u8868\uff1a\n%s\n\n\u53ea\u8f93\u51fa JSON \u6570\u7ec4\uff0c\u65e0\u4efb\u4f55\u591a\u4f59\u6587\u5b57\uff1a\n",
    "P5": "[{\"i\": <\u7f16\u53f7>, \"capability\": \"<\u4e00\u53e5\u5ba2\u89c2\u80fd\u529b\u63cf\u8ff0\uff0c\u4e0d\u8d85\u8fc7 80 \u5b57>\", \"transfer_forms\": [\"...\"], \"line_candidates\": [\"...\"], \"evidence\": \"<\u4f60\u4ece\u54ea\u53e5\u63cf\u8ff0/\u8bdd\u9898\u770b\u51fa\u6765\u7684\uff0c\u4e0d\u8d85\u8fc7 40 \u5b57>\"}]",
    "D_stock": "\u5b58\u91cf\u9ad8\u661f",
    "D_cat": "\u54c1\u7c7b\u5173\u952e\u8bcd",
    "D_cn": "\u4e2d\u6587\u751f\u6001",
    "Q_tcm": "\u4e2d\u533b",
    "Q_guji": "\u53e4\u7c4d",
    "Q_agent": "\u667a\u80fd\u4f53",
    "Q_nvwa": "\u5973\u5a32",
    # Technical-vocabulary half of the CJK plan, added 2026-07-27. Until now
    # every technical query in scan_plan() was English-only, so a Chinese
    # developer who names the repo, writes the description and writes the README
    # in Chinese was invisible for the same structural reason topic-only queries
    # missed the original five. Glossed here in English so the file stays
    # readable without decoding: free-api / api-relay / large-model / ocr /
    # fine-tune / rag-knowledge-base / knowledge-graph / short-video /
    # auto-publish / digital-human / prompt-word / tcm-large-model.
    "Q_freeapi": "\u514d\u8d39 API",
    "Q_relay": "API \u4e2d\u8f6c",
    "Q_llm": "\u5927\u6a21\u578b",
    "Q_ocr": "OCR \u8bc6\u522b",
    "Q_finetune": "\u5fae\u8c03",
    "Q_ragkb": "RAG \u77e5\u8bc6\u5e93",
    "Q_kg": "\u77e5\u8bc6\u56fe\u8c31",
    "Q_shortvid": "\u77ed\u89c6\u9891",
    "Q_autopub": "\u81ea\u52a8 \u53d1\u5e03",
    "Q_avatar": "\u6570\u5b57\u4eba",
    "Q_prompt": "\u63d0\u793a\u8bcd",
    "Q_tcmllm": "\u4e2d\u533b \u5927\u6a21\u578b",
    "SEC_H": "## \U0001f9ed \u519b\u706b\u5019\u9009 (\u5582\u8bae\u4e8b\u4f1a\u7684\u8f93\u5165\uff0c\u975e\u7ed3\u8bba)",
    "SEC_NOTE": "> \u672c\u6bb5\u53ea\u5199\u5ba2\u89c2\u4e8b\u5b9e\u4e0e\u5019\u9009\u9879\uff0c**\u4e0d\u505a\u91c7\u4e0d\u91c7\u7528\u7684\u5224\u65ad**\u3002\u5224\u65ad\u5728 SueAI \u8bae\u4e8b\u4f1a\u3002\u673a\u5668\u53ef\u8bfb\u5168\u91cf\uff1a`reports/arsenal/candidates.json`\uff1b\u53f0\u8d26\uff1a`reports/arsenal/arsenal.json`\u3002",
    "SEC_TBL": "| \u9879\u76ee | \u661f | \u80fd\u529b(\u5ba2\u89c2) | \u53ef\u8fc1\u79fb\u5f62\u6001\u5019\u9009 | \u7591\u4f3c\u5bf9\u5e94\u7ebf | \u8bb8\u53ef\u8bc1 | \u53d1\u73b0\u7ef4\u5ea6 |",
    "SEC_FOOT": "\u65b0\u589e\u5165\u53f0\u8d26 %d \u6761\uff1b\u63d0\u70bc\u5ea7\u4f4d=%s\uff08\u5747\u4e3a\u5185\u90e8\u514d\u8d39\u6c60\uff09\u3002",
    "SEC_NOSEAT": "\u65e0(\u964d\u7ea7\u4e3a\u539f\u59cb\u63cf\u8ff0)",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
ARSENAL_DIR = os.path.join(REPO_ROOT, "reports", "arsenal")

CANDIDATES_JSON = os.path.join(ARSENAL_DIR, "candidates.json")
ARSENAL_JSON = os.path.join(ARSENAL_DIR, "arsenal.json")
STAR_HISTORY_JSON = os.path.join(ARSENAL_DIR, "star_history.json")

# Human-edited, next to arsenal_repos.txt: both are manually synced mirrors of
# the local CJK ledger, and neither is ever written by this bot. The bot-written
# files live in reports/arsenal/ (the only path the workflow may commit).
ADOPTION_TXT = os.path.join(SCRIPT_DIR, "adoption.txt")
REPOS_TXT = os.path.join(SCRIPT_DIR, "arsenal_repos.txt")

# Days without a new landing before the radar starts shouting. Derived from a
# measured failure, not picked for feel: pan-register wrote its gap number into
# a run log every day and went 12 DAYS with zero human response. An alarm that
# only fires at day 12 is an alarm that reproduces that incident. 7 days is one
# full week, so a single busy week cannot swallow it, and it fires with 5 days
# still on the clock before the known-bad threshold.
ADOPTION_STALL_DAYS = 7

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------
# GitHub SEARCH is its own bucket and it is small: 10 req/min unauthenticated,
# 30 req/min authenticated. The rest of the REST API ("core") is 60/hr anon vs
# 5000/hr with a token. Everything below therefore goes through ONE throttled,
# budgeted, cached door -- a scan plan that grows by dimension must not be able
# to take the daily report down with it.
SEARCH_MIN_INTERVAL_AUTH = 2.2     # 60/2.2 = ~27 req/min, under the 30 ceiling
SEARCH_MIN_INTERVAL_ANON = 6.5     # 60/6.5 = ~9 req/min, under the 10 ceiling
# 34 -> 44 on 2026-07-27, when the CJK technical block took the plan from 27
# queries to 39. Raised rather than paid for by pruning, and the reasoning is
# worth keeping because "budget" reads like "cost" and here it is not:
#   * This budget is a RUNAWAY GUARD, not a quota. GitHub search costs nothing;
#     the ceiling that actually binds is 30 requests/MINUTE, and that is enforced
#     by SEARCH_MIN_INTERVAL_AUTH above (~27/min), not by this number. Raising
#     the budget cannot breach the rate limit -- it only buys wall time:
#     39 calls x 2.2s = ~86s, against ~59s for the old 27-query plan.
#   * 44 not 39, i.e. five spare, ON PURPOSE. When the budget is spent gh_search
#     returns empty and the plan is consumed IN ORDER, so a plan sized exactly to
#     its budget silently truncates from the TAIL -- and the tail is the CJK
#     block. Sizing it flush would have made the Chinese queries the first thing
#     to die on any future addition, which is the failure being fixed here.
# The env override stays the escape hatch if a run ever needs to be cut short.
SEARCH_BUDGET = int(os.environ.get("ARSENAL_SEARCH_BUDGET", "44"))
CORE_MIN_INTERVAL = 0.8

_state = {"search_calls": 0, "core_calls": 0, "last_search": 0.0, "last_core": 0.0,
          "throttled": 0, "errors": 0}
_search_cache = {}


def _token():
    return os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")


def _headers(accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": "IntelRadar-Arsenal/1.0 (SueAI)",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = _token()
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _sleep_until(kind):
    now = time.time()
    if kind == "search":
        gap = SEARCH_MIN_INTERVAL_AUTH if _token() else SEARCH_MIN_INTERVAL_ANON
        wait = _state["last_search"] + gap - now
    else:
        wait = _state["last_core"] + CORE_MIN_INTERVAL - now
    if wait > 0:
        _state["throttled"] += 1
        time.sleep(wait)


def _get_json(url, accept="application/vnd.github+json", timeout=40):
    req = urllib.request.Request(url, headers=_headers(accept))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace")), resp.headers


def gh_search(q, sort="stars", per_page=100, order="desc"):
    """One throttled + budgeted + cached repo search.

    Returns (items, failed). `failed` matters: an empty result from a genuinely
    empty query and an empty result from a timeout look identical, and treating
    the second as "this query is dead, prune it" would delete a working
    dimension on the strength of one bad night. Measured 2026-07-26: the CJK
    query for classical texts timed out once and returned 0 while the other CJK
    queries returned 100, 100 and 19 -- i.e. CJK search works fine and the 0 was
    the network, not the query."""
    key = "%s|%s|%s|%s" % (q, sort, order, per_page)
    if key in _search_cache:
        return _search_cache[key]
    if _state["search_calls"] >= SEARCH_BUDGET:
        print("    [arsenal] search budget %d spent, skipping: %s"
              % (SEARCH_BUDGET, q[:60]), flush=True)
        return [], True
    url = ("https://api.github.com/search/repositories?q=%s&sort=%s&order=%s&per_page=%d"
           % (urllib.parse.quote(q), sort, order, per_page))
    items, failed = [], True
    for attempt in range(3):
        _sleep_until("search")
        try:
            data, hdrs = _get_json(url, timeout=40 + 20 * attempt)
            _state["last_search"] = time.time()
            _state["search_calls"] += 1
            items = data.get("items", []) or []
            failed = False
            # A search bucket at zero means every later query this run is a 403.
            # Stop spending the budget instead of burning it on failures.
            try:
                if int(hdrs.get("x-ratelimit-remaining", "99")) <= 1:
                    print("    [arsenal] search quota exhausted, halting scan", flush=True)
                    _state["search_calls"] = SEARCH_BUDGET
            except (TypeError, ValueError):
                pass
            break
        except urllib.error.HTTPError as e:
            _state["last_search"] = time.time()
            _state["search_calls"] += 1
            if e.code in (403, 429):
                retry = e.headers.get("Retry-After") if e.headers else None
                nap = int(retry) if (retry or "").isdigit() else 20 * (attempt + 1)
                print("    [arsenal] %s on search, backing off %ss" % (e.code, nap), flush=True)
                time.sleep(min(nap, 60))
                continue
            _state["errors"] += 1
            print("    [arsenal] HTTP %s: %s" % (e.code, q[:60]), flush=True)
            break
        except Exception as e:
            # transient network faults get one more go with a longer timeout
            # before the query is written off
            _state["last_search"] = time.time()
            _state["errors"] += 1
            print("    [arsenal] search failed (%s, attempt %d): %s"
                  % (type(e).__name__, attempt + 1, q[:60]), flush=True)
            if attempt < 2:
                time.sleep(3 + 3 * attempt)
                continue
            break
    _search_cache[key] = (items, failed)
    return items, failed


# ---------------------------------------------------------------------------
# scan plan
# ---------------------------------------------------------------------------
def _d(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def scan_plan():
    """(dimension, query, sort) triples.

    Dimension keys are ASCII; their display labels live in _DIM_LABEL. Queries
    tagged `PROVEN` were each measured to return a specific repo the old radar
    had missed -- changing those four is a regression, not a cleanup."""
    d30, d90, d180 = _d(30), _d(90), _d(180)
    return [
        # ---- stock: high-star AND still maintained. The old main scan could
        # only see `created:>7d`, i.e. nothing that already exists. ----
        ("stock", "topic:auto-publish stars:>200 pushed:>%s" % d180, "stars"),
        # PROVEN yikart/AiToEarn (24230 stars, created 2025-02-24)
        ("stock", "topic:social-media-automation stars:>300 pushed:>%s" % d90, "stars"),
        ("stock", "topic:mcp-server stars:>1000 pushed:>%s" % d30, "stars"),
        ("stock", "topic:mcp stars:>2000 pushed:>%s" % d30, "updated"),
        ("stock", "topic:ai-agent stars:>2000 pushed:>%s" % d30, "updated"),
        ("stock", "topic:content-creation stars:>1000 pushed:>%s" % d90, "stars"),
        ("stock", "topic:free-api stars:>300 pushed:>%s" % d90, "stars"),

        # ---- category: FULL TEXT, not topics. This is the block that fixes the
        # structural blindness -- 4 of the 5 missed repos carry no topics. ----
        # Pure-star catch-all: no keyword, no topic, no category guess at all.
        # Measured 2026-07-26 -- obra/superpowers (261421 stars) slipped past
        # every other query in this plan. Its name carries neither "skill" nor
        # "awesome", and its topics (ai, skills, coding) match no topic query
        # here. A repo can be among the loudest things on GitHub and still be
        # invisible to a radar that only ever asks by name or by tag, so the
        # last line of defence must ask by nothing but size. stars:>100000 +
        # pushed:>30d matched exactly 94 repos worldwide: the whole set fits in
        # one page, so this costs a single call and can never be truncated away
        # by per_page the way stars:>50000 was (that one overflowed 100 and
        # dropped superpowers again -- both were tried, this is the one that
        # holds).
        # PROVEN obra/superpowers (261421 stars, rank 59 of 94)
        ("category", "stars:>100000 pushed:>%s" % d30, "updated"),
        ("category", "skill in:name stars:>500 pushed:>%s" % d90, "stars"),
        # PROVEN alchaincyf/zhangxuefeng-skill (9991 stars, topics=[])
        ("category", "skills in:name stars:>500 pushed:>%s" % d90, "stars"),
        # PROVEN tmstack/awesome-persona-skills (3413 stars, topics=[])
        ("category", "skill in:name stars:>200 pushed:>%s" % d30, "updated"),
        # PROVEN YouMind-OpenLab/ai-image-prompts-skill (528 stars, topics=[])
        ("category", "openai-compatible proxy free in:name,description stars:>300", "stars"),
        # PROVEN tashfeenahmed/freellmapi (16978 stars, topics=[])
        ("category", "awesome in:name stars:>2000 pushed:>%s" % d30, "updated"),
        ("category", "agent skill in:name,description stars:>300 pushed:>%s" % d90, "stars"),
        ("category", "free llm api in:name,description stars:>200 pushed:>%s" % d90, "stars"),
        ("category", "api aggregator llm in:name,description stars:>200", "stars"),
        ("category", "mcp server in:name,description stars:>1000 pushed:>%s" % d30, "updated"),
        # `multi-platform publish in:name,description stars:>100` sat here and
        # measured 0 results on 2026-07-26 -- replaced, not kept as decoration.
        # topic:xiaohongshu measured 46 results and independently returns
        # AiToEarn, i.e. it covers the same "one-click multi-platform publish"
        # category that the founder pointed at, and actually returns rows.
        ("category", "topic:xiaohongshu stars:>100", "stars"),
        ("category", "auto publish douyin xiaohongshu in:name,description,readme stars:>100",
         "stars"),
        ("category", "content automation in:name,description stars:>500 pushed:>%s" % d90,
         "stars"),
        ("category", "prompts in:name stars:>300 pushed:>%s" % d30, "updated"),

        # ---- chinese ecosystem. Domain half (below) plus a TECHNICAL half that
        # did not exist before 2026-07-27: every technical query above this block
        # is English-only, so a project named, described and documented in
        # Chinese was structurally invisible for exactly the same reason the
        # original five were -- we were only ever asking in one language.
        #
        # THREE THINGS WERE MEASURED on 2026-07-27, all against the live API, and
        # they overturn the "CJK literals are best-effort" note that stood here:
        #
        #  1. `pushed:` IS THE CJK NOISE FILTER, and it is not optional. GitHub
        #     matches CJK loosely, so a handful of enormous, ABANDONED awesome
        #     lists sit at the top of nearly every CJK result set -- funNLP
        #     (82061 stars, no push in >90d) headed 11 of the 16 queries first
        #     tried. Adding `pushed:>d90` evicts them: `Q_ocr` went 93 results
        #     headed by funNLP -> 18 results that are all genuinely OCR. The
        #     filter buys precision, not freshness, which is why it is on almost
        #     every line below.
        #
        #  2. RESULT COUNT IS NOT QUALITY. The single highest-yielding CJK query
        #     tried, the classical-Chinese term for literary Chinese at
        #     `stars:>20`, returned 862 -- and was pure noise: rust-course and
        #     gpt_academic matched it. It is REJECTED despite out-yielding every
        #     query that was kept. Same verdict, same evidence standard, for the
        #     generic words for "skill" (107 results, all Android/admin/interview
        #     repos) and "workflow" (74, all Java ERP frameworks): a CJK word
        #     common in ordinary software prose cannot be a radar dimension.
        #
        #  3. `Q_tcmllm` DELIBERATELY CARRIES NO `pushed:` FILTER, and that is
        #     the one real exception to point 1. Measured both ways: without it,
        #     30 results led by ShenNong-TCM-LLM / TCMLLM / HuangDI; WITH
        #     `pushed:>d90`, 7 results whose stars are 3, 0, 0, 0, 0. The good
        #     work in this niche is finished research, not a live project, so
        #     here the freshness filter deletes precisely the rows we want.
        #     Do not "make it consistent" with the lines around it.
        #
        # Every query below was run against the live API on 2026-07-27 and is
        # tagged with what it actually returned. None was kept on taste; a query
        # that answers 0 is reported by --selftest and pruned, never left as
        # decoration. ----
        ("chinese", "topic:chinese stars:>500 pushed:>%s" % d30, "updated"),
        ("chinese", "topic:chinese-nlp stars:>200 pushed:>%s" % d90, "stars"),
        ("chinese", ZH["Q_tcm"] + " in:name,description stars:>50", "stars"),
        ("chinese", ZH["Q_guji"] + " in:name,description stars:>20", "stars"),
        ("chinese", ZH["Q_agent"] + " in:name,description stars:>200", "stars"),
        ("chinese", ZH["Q_nvwa"] + " in:name,description stars:>100", "stars"),

        # -- free / cheap model access: the pool the batch work actually runs on
        # ret=30 PROVEN chatanywhere/GPT_API_free (39052), fangzesheng/free-api (16168)
        ("chinese", ZH["Q_freeapi"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),
        # ret=12 PROVEN Wei-Shaw/sub2api (34434), qixing-jk/all-api-hub (4554),
        # zzsting88/relayAPI (3550) -- the Chinese-language relay/aggregator
        # category, which no English query in this plan returns.
        ("chinese", ZH["Q_relay"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),
        # ret=59 PROVEN harry0703/MoneyPrinterTurbo (99373), jingyaogong/minimind (53861)
        # -- a 99k-star content-production repo the radar had never once seen.
        ("chinese", ZH["Q_llm"] + " in:name,description stars:>1000 pushed:>%s" % d30,
         "stars"),

        # -- OCR / models: 650k pages of scanned classics are waiting on this
        # ret=18 PROVEN tisfeng/Easydict (13999), DayBreak-u/chineseocr_lite (12327)
        ("chinese", ZH["Q_ocr"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),
        # ret=24 PROVEN datawhalechina/self-llm (31429), AiHubCN/Awesome-Chinese-LLM (22699)
        ("chinese", ZH["Q_finetune"] + " in:name,description stars:>300 pushed:>%s" % d90,
         "stars"),

        # -- RAG / knowledge graph / document parsing
        # ret=19 PROVEN zhimaAi/chatwiki (2016), moyangzhan/langchain4j-aideepin (1341)
        ("chinese", ZH["Q_ragkb"] + " in:name,description stars:>200", "stars"),
        # ret=12 PROVEN xerrors/Yuxi (6264), honeyandme/RAGQnASystem (1370)
        ("chinese", ZH["Q_kg"] + " in:name,description stars:>200 pushed:>%s" % d90,
         "stars"),

        # -- AI content production: classics -> video / graphics
        # ret=35 PROVEN harry0703/MoneyPrinterTurbo (99373), ATH-MaaS/Pixelle-Video (26056)
        ("chinese", ZH["Q_shortvid"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),
        # ret=30 PROVEN white0dew/XiaohongshuSkills (3231), liyown/ai-trend-publish (3080)
        # -- the Chinese half of the auto-publish category that AiToEarn sits in.
        ("chinese", ZH["Q_autopub"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),
        # ret=19 PROVEN xszyou/Fay (13352), modstart-lib/aigcpanel (5354). Avatar
        # synthesis is a separate category from Q_shortvid (video assembly);
        # measured overlap between the two result sets was near zero.
        ("chinese", ZH["Q_avatar"] + " in:name,description stars:>100 pushed:>%s" % d90,
         "stars"),

        # -- agent / skill / prompt engineering
        # ret=41 PROVEN langgptai/LangGPT (12378), rockbenben/ChatGPT-Shortcut (8640)
        ("chinese", ZH["Q_prompt"] + " in:name,description stars:>200 pushed:>%s" % d90,
         "stars"),
        # ret=30 PROVEN michael-wzhu/ShenNong-TCM-LLM (499), 2020MEAI/TCMLLM (261),
        # Zlasejd/HuangDI (160). Lowest star counts in the plan and the most
        # on-mission query in it: 100% of the first page is TCM-specific LLM
        # work. See point 3 above before adding a `pushed:` filter here.
        ("chinese", ZH["Q_tcmllm"] + " in:name,description", "stars"),
    ]


_DIM_LABEL = {
    "stock": ZH["D_stock"],
    "category": ZH["D_cat"],
    "chinese": ZH["D_cn"],
}

# ---------------------------------------------------------------------------
# license: a hard fact from the API, never an LLM guess
# ---------------------------------------------------------------------------
PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC",
              "MPL-2.0", "UNLICENSE", "CC0-1.0", "0BSD", "ZLIB", "PSF-2.0"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0",
            "SSPL-1.0", "BUSL-1.1", "EUPL-1.2"}


def license_family(spdx):
    """permissive | copyleft | none | unknown. Deterministic classification of a
    field the API hands us -- the council needs the raw fact, not our opinion of
    whether the terms are acceptable."""
    s = (spdx or "").strip().upper()
    if not s or s in ("NOASSERTION", "NONE"):
        return "none" if not s else "unknown"
    if s in PERMISSIVE:
        return "permissive"
    if s in COPYLEFT:
        return "copyleft"
    return "unknown"


# ---------------------------------------------------------------------------
# star velocity
# ---------------------------------------------------------------------------
# NOT via GET /repos/{o}/{r}/stargazers. That endpoint was cross-checked three
# independent ways on 2026-07-26 -- python urllib, curl, and `gh api` -- and all
# three answer 404 WITH a valid token (the same token gets 200 on /repos and
# shows x-ratelimit-remaining 4984, so it is the endpoint, not our auth). So
# per-star timestamps are simply not obtainable and any "30-day velocity" built
# on them would be fiction.
#
# What is left is honest and costs zero extra requests: snapshot the star count
# every run into star_history.json and diff. Two consequences stated plainly
# rather than papered over:
#   * run 1 establishes a baseline and reports delta = null. It does NOT report 0.
#   * a true 30-day delta needs 30 days of runs. Until then the delta is over
#     whatever window exists, and `window_days` says how long that is.
# `stars_per_day_lifetime` (stars / age) is available immediately and separates
# "fast from birth" from "old and slow", but by construction cannot see an old
# repo that just caught fire -- that is what the snapshot diff is for.
def load_star_history():
    try:
        with open(STAR_HISTORY_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_star_history(hist, repos, today, keep_days=120):
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=keep_days)).isoformat()
    for r in repos:
        row = hist.setdefault(r["repo"], [])
        row[:] = [p for p in row if p.get("d", "") >= cutoff]
        if row and row[-1].get("d") == today:
            row[-1]["s"] = r["stars"]
        else:
            row.append({"d": today, "s": r["stars"]})
    return hist


def star_delta(hist, repo, today, days=30):
    row = hist.get(repo) or []
    if len(row) < 2:
        return None, None
    want = (datetime.date.fromisoformat(today) - datetime.timedelta(days=days)).isoformat()
    older = [p for p in row if p.get("d", "") <= want]
    base = older[-1] if older else row[0]
    span = (datetime.date.fromisoformat(today)
            - datetime.date.fromisoformat(base["d"])).days
    if span <= 0:
        return None, None
    return row[-1]["s"] - base["s"], span


def _age_days(created_at):
    try:
        c = datetime.datetime.strptime(created_at[:10], "%Y-%m-%d").date()
        return max(1, (datetime.date.today() - c).days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------
def load_known_names():
    """Repo short-names already catalogued in the local arsenal ledger, via the
    committed ASCII snapshot arsenal_repos.txt, PLUS everything already in
    arsenal.json. Known repos are not re-emitted as fresh candidates -- but they
    are still star-snapshotted, so a dormant entry that suddenly moves is
    visible in the ledger."""
    names = set()
    p = os.path.join(SCRIPT_DIR, "arsenal_repos.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().lower()
                if s and not s.startswith("#"):
                    names.add(s.split("/")[-1])
    try:
        with open(ARSENAL_JSON, encoding="utf-8") as f:
            for e in json.load(f).get("entries", []):
                names.add(e["repo"].split("/")[-1].lower())
    except Exception:
        pass
    return names


def collect(limit=60, include_known=False, history=None, apply_select=True):
    """Run the scan plan and return (rows, per_query). Rows are normalised,
    deduped and SELECTED per dimension -- see `select()` for why not by stars."""
    known = load_known_names()
    hist = history if history is not None else load_star_history()
    seen, rows = set(), []
    plan = scan_plan()
    print("[arsenal] scan plan: %d queries, budget %d, auth=%s"
          % (len(plan), SEARCH_BUDGET, "yes" if _token() else "NO (anon, slow)"), flush=True)
    per_query = {}
    by_repo = {}
    for dim, q, sort in plan:
        items, failed = gh_search(q, sort=sort, per_page=100)
        breadth = len(items)
        fresh = 0
        for pos, it in enumerate(items):
            full = it.get("full_name") or ""
            if not full:
                continue
            short = full.split("/")[-1].lower()
            is_known = short in known
            if is_known and not include_known:
                continue
            match = {"dimension": dim, "query": q, "sort": sort,
                     "query_breadth": breadth, "position": pos}
            prev = by_repo.get(full)
            if prev is not None:
                # NARROWEST MATCH WINS. A repo caught by a 1-result query is a
                # targeted hit; the same repo caught by `awesome in:name`
                # (100 results) is background noise. Keeping whichever query
                # happened to run first would throw that signal away.
                if (breadth, pos) < (prev["found_by"]["query_breadth"],
                                     prev["found_by"]["position"]):
                    prev["found_by"] = match
                prev["matched_by_n_queries"] = prev.get("matched_by_n_queries", 1) + 1
                continue
            seen.add(it.get("id"))
            spdx = ((it.get("license") or {}).get("spdx_id") or "")
            created = it.get("created_at") or ""
            stars = it.get("stargazers_count", 0)
            age = _age_days(created)
            by_repo[full] = {
                "repo": full,
                "url": it.get("html_url", ""),
                "description": (it.get("description") or "").replace("\n", " ").strip()[:400],
                "stars": stars,
                "topics": (it.get("topics") or [])[:12],
                "lang": it.get("language") or "",
                "created_at": created[:10],
                "pushed_at": (it.get("pushed_at") or "")[:10],
                "age_days": age,
                "stars_per_day_lifetime": (round(stars / age, 2) if age else None),
                "license": spdx or "NONE",
                "license_family": license_family(spdx),
                "found_by": match,
                "matched_by_n_queries": 1,
                "known_in_arsenal": is_known,
                "new_to_us": full not in hist,
            }
            fresh += 1
        per_query[q] = {"returned": len(items), "new": fresh, "failed": failed,
                        "dimension": dim, "sort": sort}
        print("    [%s] ret=%-3d new=%-3d %s%s"
              % (dim, len(items), fresh, q[:60], "  <FAILED>" if failed else ""), flush=True)
    rows = list(by_repo.values())
    total = len(rows)
    # selftest asks for the RAW set: select() caps rows per query, so running it
    # there would let the cap -- not the scan -- decide whether a target repo
    # "was found", which is precisely the thing the test has to measure.
    rows = select(rows, limit) if apply_select else sorted(rows, key=_sort_key)
    print("[arsenal] candidates: %d raw -> %d selected (budget %d/%d, errors %d)"
          % (total, len(rows), _state["search_calls"], SEARCH_BUDGET, _state["errors"]),
          flush=True)
    return rows, per_query


PER_QUERY_CAP = int(os.environ.get("ARSENAL_PER_QUERY_CAP", "8"))


def _sort_key(r):
    fb = r["found_by"]
    return (not r["new_to_us"],        # already snapshotted in a past run -> later
            fb["query_breadth"],       # narrow query = targeted hit
            fb["position"],            # rank inside that query's own ordering
            -(r.get("stars") or 0))


def select(rows, limit):
    """Pick which candidates the council actually gets to see.

    NOT a sort by stars, and this was MEASURED, not assumed. The first real run
    (2026-07-26) produced 1023 raw candidates; the first version of this
    function sorted by (unseen, stars) and, because run 1 has an empty history,
    "unseen" was true for everything and the tiebreak collapsed to pure stars.
    The council would have been handed awesome-python (310k), awesome-selfhosted
    (308k), n8n (198k) -- and the distiller correctly answered `unknown` for
    every single line, i.e. 24 rows of zero signal. That is the same myopia
    wearing a different hat, and it is exactly what a report that only checked
    "did it run" would have shipped.

    The ordering that replaced it uses only hard facts about HOW a repo was
    found, never an opinion about the repo:
      * `query_breadth` -- how many results the query that found it returned.
        A repo found by a 1-result query (`topic:auto-publish` -> AiToEarn) is a
        targeted hit; the same repo appearing in a 100-result `awesome in:name`
        sweep is background. Narrowest match wins, decided in collect().
      * `position` -- its rank inside that query's own ordering.
      * `new_to_us` -- absent from our star history. Neutral on run 1 by
        construction, and correctly demotes repeats from run 2 on.
      * stars, last, purely to break exact ties.

    Two structural caps stop any one query or dimension from flooding the set:
    PER_QUERY_CAP rows per query, then round-robin across dimensions."""
    rows = sorted(rows, key=_sort_key)
    capped, per_q = [], {}
    for r in rows:
        q = r["found_by"]["query"]
        if per_q.get(q, 0) >= PER_QUERY_CAP:
            continue
        per_q[q] = per_q.get(q, 0) + 1
        capped.append(r)
    groups = {}
    for r in capped:
        groups.setdefault(r["found_by"]["dimension"], []).append(r)
    out, i = [], 0
    while len(out) < limit and any(len(g) > i for g in groups.values()):
        for dim in sorted(groups):
            if len(out) >= limit:
                break
            if len(groups[dim]) > i:
                out.append(groups[dim][i])
        i += 1
    return out


# ---------------------------------------------------------------------------
# distillation -- OBJECTIVE ONLY
# ---------------------------------------------------------------------------
# Our real production lines. A candidate that fits none of them must come back
# "unknown"; inventing a fit is the failure this enum exists to prevent.
LINES = ["video", "formula", "image", "modelpool", "ocr", "rag", "unknown"]
TRANSFERS = ["mcp", "skill", "architecture", "deploy", "dataset", "library", "unknown"]

# Built from ZH so the prompt has exactly one source of truth. The %s left for
# the repo listing is inserted at call time.
DISTILL_PROMPT = (ZH["P1"] + ZH["P2"] + (ZH["P3"] % ZH["LINE_HINT"])
                  + ZH["P4"] + ZH["P5"])


def _load_roster():
    """Import the council's FREE model bench. READ-ONLY: this module never
    writes to scripts/council/. MAX_LLM_CALLS is set before the import because
    llm_roster reads it at module level, and the radar's distillation budget is
    much smaller than a council session's."""
    os.environ.setdefault("MAX_LLM_CALLS", os.environ.get("ARSENAL_LLM_CALLS", "12"))
    council = os.path.join(REPO_ROOT, "scripts", "council")
    if council not in sys.path:
        sys.path.insert(0, council)
    import llm_roster                      # noqa: E402
    return llm_roster


def _extract_json_array(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = "\n".join(t.split("\n")[1:])
        t = t.split("```")[0]
    i, j = t.find("["), t.rfind("]")
    if i < 0 or j <= i:
        return []
    try:
        out = json.loads(t[i:j + 1])
        return out if isinstance(out, list) else []
    except Exception:
        return []


def distill(rows, batch=10):
    """Attach objective `capability` / `transfer_forms` / `line_candidates` to
    each row using the free bench. Every seat may fail; the rows still ship,
    just with distilled=False, because a candidate the council never sees is a
    worse outcome than a candidate with a thin description."""
    if not rows:
        return rows, {"seat": None, "calls": 0, "ok": 0}
    try:
        roster = _load_roster()
    except Exception as e:
        print("[arsenal] free bench unavailable (%s) -- shipping raw rows"
              % type(e).__name__, flush=True)
        return rows, {"seat": None, "calls": 0, "ok": 0}
    seats = roster._bench()
    if not seats:
        print("[arsenal] no free pool has credentials -- shipping raw rows", flush=True)
        return rows, {"seat": None, "calls": 0, "ok": 0}
    print("[arsenal] free bench wired: %s" % [s["id"] for s in seats], flush=True)

    stats = {"seat": None, "calls": 0, "ok": 0}
    # 2026-08-05 run 30973033360: sensenova failed identically on every batch,
    # burning 3 of the 12-call budget on retries that could never succeed, and
    # once the budget ran out the loop kept polling every seat twice more --
    # each poll another BudgetExhausted print. Two rules from that:
    #   * a seat that fails twice is benched for the rest of this distill;
    #   * BudgetExhausted is GLOBAL, not the seat's -- stop the whole distill
    #     and ship the remaining rows raw (distilled=False is a designed
    #     outcome, see docstring), instead of grinding the bench.
    seat_fails: dict = {}
    budget_out = False
    for start in range(0, len(rows), batch):
        if budget_out:
            break
        chunk = rows[start:start + batch]
        listing = "\n".join(
            "[%d] %s | \u2b50%s | %s | topics: %s | %s"
            % (start + k + 1, r["repo"], r["stars"], r["lang"] or "-",
               ", ".join(r["topics"]) or "-", r["description"][:220])
            for k, r in enumerate(chunk))
        user = DISTILL_PROMPT % listing
        parsed = []
        for seat in seats:
            if seat_fails.get(seat["id"], 0) >= 2:
                continue
            try:
                stats["calls"] += 1
                txt = roster.call(seat, "You output JSON only.", user,
                                  max_tokens=1600, temperature=0.1,
                                  stage="arsenal", tag="distill", retries=0)
                parsed = _extract_json_array(txt)
                if parsed:
                    stats["seat"] = seat["id"]
                    stats["ok"] += 1
                    break
            except roster.BudgetExhausted:
                print("    [arsenal] distill budget exhausted -- shipping the "
                      "rest raw", flush=True)
                budget_out = True
                break
            except Exception as e:
                seat_fails[seat["id"]] = seat_fails.get(seat["id"], 0) + 1
                bench_note = (" (benched for this distill)"
                              if seat_fails[seat["id"]] >= 2 else "")
                print("    [arsenal] seat %s failed: %s%s"
                      % (seat["id"], type(e).__name__, bench_note), flush=True)
        by_i = {}
        for o in parsed:
            try:
                by_i[int(o.get("i"))] = o
            except (TypeError, ValueError):
                continue
        for k, r in enumerate(chunk):
            o = by_i.get(start + k + 1) or {}
            r["capability"] = str(o.get("capability") or "")[:160]
            r["evidence"] = str(o.get("evidence") or "")[:120]
            tf = [x for x in (o.get("transfer_forms") or []) if x in TRANSFERS]
            ln = [x for x in (o.get("line_candidates") or []) if x in LINES]
            r["transfer_forms"] = tf or ["unknown"]
            r["line_candidates"] = ln or ["unknown"]
            r["distilled"] = bool(r["capability"])
    print("[arsenal] distilled with seat=%s (%d/%d batches ok)"
          % (stats["seat"], stats["ok"], (len(rows) + batch - 1) // batch), flush=True)
    return rows, stats


# ---------------------------------------------------------------------------
# ledgers
# ---------------------------------------------------------------------------
STATUS_NEW = "pending"          # \u5f85\u8bc4\u4f30
# `debated` added 2026-07-26. The first end-to-end test of the council handoff
# applied ZERO decisions: the council writes `hold` for everything it argued
# (it must not infer approval from a judge's prose -- a human gates adoption),
# `hold` mapped to `pending`, and apply_council_decisions skips a row whose
# status would not change. So "this has already been to committee" had nowhere
# to live, and the radar would have gone on re-proposing the same repos forever
# while believing it had a memory. The lifecycle needs a state between "found"
# and "ruled on", or the loop cannot actually close.
#   pending -> found, never discussed
#   debated -> the council argued it; awaiting the founder's call
#   verified / integrated / dropped -> ruled on
VALID_STATUS = ("pending", "debated", "verified", "integrated", "dropped")


def write_candidates(rows, today, scan_stats, distill_stats):
    """The council's INPUT FILE. Overwritten every run: it is "what the radar
    saw today", not a history. The history is arsenal.json."""
    os.makedirs(ARSENAL_DIR, exist_ok=True)
    doc = {
        "schema": SCHEMA_VERSION,
        "generated": today,
        "producer": "scripts/intel_radar/arsenal_radar.py",
        "consumer": "scripts/council (triage topic source)",
        "contract": ("objective description and candidate options only; "
                     "no adoption judgement, no score, no ranking"),
        "enums": {"line_candidates": LINES, "transfer_forms": TRANSFERS,
                  "license_family": ["permissive", "copyleft", "none", "unknown"]},
        "scan": scan_stats,
        "distill": distill_stats,
        "count": len(rows),
        "candidates": rows,
    }
    with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=False)
    print("[arsenal] wrote %s (%d candidates)" % (CANDIDATES_JSON, len(rows)), flush=True)
    return doc


def merge_arsenal(rows, today):
    """Append-only ledger with a workflow status. Status and first_seen are
    NEVER overwritten by a scan -- they record what WE decided, and a radar run
    must not silently reset a human/council decision back to 'pending'."""
    os.makedirs(ARSENAL_DIR, exist_ok=True)
    try:
        with open(ARSENAL_JSON, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = {"schema": SCHEMA_VERSION, "entries": []}
    by_repo = {e["repo"]: e for e in doc.get("entries", [])}
    added = 0
    for r in rows:
        e = by_repo.get(r["repo"])
        if e is None:
            e = {"repo": r["repo"], "url": r["url"], "first_seen": today,
                 "status": STATUS_NEW, "status_note": ""}
            by_repo[r["repo"]] = e
            added += 1
        e["last_seen"] = today
        for k in ("stars", "license", "license_family", "lang", "pushed_at",
                  "created_at", "stars_per_day_lifetime", "stars_delta",
                  "stars_delta_window_days", "capability", "transfer_forms",
                  "line_candidates", "topics"):
            if k in r and r[k] not in (None, "", []):
                e[k] = r[k]
        e.setdefault("found_by", r.get("found_by"))
        if e.get("status") not in VALID_STATUS:
            e["status"] = STATUS_NEW
    doc["schema"] = SCHEMA_VERSION
    doc["updated"] = today
    doc["status_enum"] = list(VALID_STATUS)
    doc["entries"] = sorted(by_repo.values(), key=lambda e: (-(e.get("stars") or 0), e["repo"]))
    apply_council_decisions(doc)
    # adoption.txt runs AFTER the council: a thing we have actually put into
    # production outranks any verdict about whether we should.
    apply_adoption_to_ledger(doc)
    with open(ARSENAL_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=False)
    print("[arsenal] ledger %s: %d entries (+%d new)"
          % (ARSENAL_JSON, len(doc["entries"]), added), flush=True)
    return doc, added


COUNCIL_DECISIONS = os.path.join(REPO_ROOT, "reports", "council", "decisions.json")

# council verdict -> ledger status. Anything else is ignored rather than
# guessed at, so a new verdict word can never silently retire an entry.
_DECISION_TO_STATUS = {
    "adopt": "verified", "integrate": "integrated", "integrated": "integrated",
    "verified": "verified", "drop": "dropped", "reject": "dropped",
    "dropped": "dropped", "hold": "debated",
    # 2026-08-07: the council now emits `watch` when a plan drew a veto vote.
    # It maps to `debated`, NOT `dropped`, on purpose -- a veto queues an entry
    # for re-review, it does not write it off ("never write anyone off, just
    # put them further back", founder 2026-06-20). Landing in `debated` keeps
    # it eligible to be picked up again once the judgement bar or the entry
    # itself has moved.
    "watch": "debated",
}


def apply_council_decisions(doc):
    """Close the loop: read reports/council/decisions.json (written by the
    council, NOT by this file) and move ledger entries out of `pending`.

    This is the read side of the handoff and it is deliberately defensive --
    the file does not exist yet, and this module must not need a second edit on
    the day the council starts writing it. A missing or malformed file is a
    silent no-op, never an error: the radar's own job must not depend on the
    council having run."""
    try:
        with open(COUNCIL_DECISIONS, encoding="utf-8") as f:
            decisions = json.load(f).get("decisions", [])
    except Exception:
        return 0
    by_repo = {e["repo"]: e for e in doc.get("entries", [])}
    n = 0
    for d in decisions:
        src = str(d.get("source") or "")
        if not src.startswith("arsenal:"):
            continue
        e = by_repo.get(src.split(":", 1)[1])
        if e is None:
            continue
        st = _DECISION_TO_STATUS.get(str(d.get("decision") or "").lower())
        if not st or e.get("status") == st:
            continue
        e["status"] = st
        e["status_note"] = "council %s%s" % (
            d.get("date") or "", (" #%s" % d["issue"]) if d.get("issue") else "")
        n += 1
    if n:
        print("[arsenal] council decisions applied to %d ledger entries" % n, flush=True)
    return n


def load_adoption():
    """Parse adoption.txt -> [{"id","date","note"}]. See that file's header for
    the three-part bar an entry must clear.

    Defensive on purpose, same as apply_council_decisions: a missing or garbled
    file yields an EMPTY list, never an exception. Zero landings is a real
    answer here -- it is what the stall alarm is for -- so this must never be
    able to take the daily report down with it."""
    rows = []
    try:
        with open(ADOPTION_TXT, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception:
        return rows
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        ident = parts[0]
        if not ident:
            continue
        date = parts[1] if len(parts) > 1 else "-"
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = ""          # '-' (pre-existing stock) or malformed: no date
        rows.append({"id": ident, "date": date,
                     "note": parts[2] if len(parts) > 2 else ""})
    return rows


def apply_adoption_to_ledger(doc):
    """Mirror adoption.txt onto arsenal.json's status field.

    adoption.txt is the authority; the ledger's `status` is a derived view kept
    in sync so the council reads one truth and not two.

    Entries absent from the ledger are CREATED, not skipped. Measured on the
    real 330-entry ledger: none of the four things we actually run had ever been
    surfaced by a scan, so an update-only version moved zero rows and `integrated`
    -- the terminal state of the whole lifecycle -- stayed permanently
    unreachable, which is the same dead-field bug this file exists to fix. A
    thing in production belongs in the arsenal's history by definition. The row
    is stamped found_by=adoption.txt so it is never mistaken for a scan result,
    and carries no stars/topics we did not measure."""
    by_repo = {e["repo"]: e for e in doc.get("entries", [])}
    n = 0
    for r in load_adoption():
        note = ("adopted %s" % r["date"]) if r["date"] else "adopted (stock)"
        e = by_repo.get(r["id"])
        if e is None:
            e = {"repo": r["id"],
                 "url": ("https://github.com/%s" % r["id"]) if "/" in r["id"] else "",
                 "first_seen": r["date"] or "", "status": "pending",
                 "status_note": "", "found_by": {"dimension": "adoption.txt"}}
            doc.setdefault("entries", []).append(e)
            by_repo[r["id"]] = e
        if e.get("status") == "integrated":
            continue
        e["status"] = "integrated"
        e["status_note"] = note
        n += 1
    if n:
        print("[arsenal] adoption.txt applied to %d ledger entries" % n, flush=True)
    return n


def _count_known_candidates():
    """How many distinct things we have on the books as candidates: the local
    ledger's committed ASCII snapshot (arsenal_repos.txt) unioned with what the
    cloud scan itself has accumulated (arsenal.json). Union, not sum -- the two
    overlap, and double counting would flatter the denominator of the very
    absorption rate this is here to expose."""
    names = set()
    try:
        with open(REPOS_TXT, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    names.add(s.lower())
    except Exception:
        pass
    try:
        with open(ARSENAL_JSON, encoding="utf-8") as f:
            for e in json.load(f).get("entries", []):
                if e.get("repo"):
                    names.add(e["repo"].lower())
    except Exception:
        pass
    return len(names)


def adoption_snapshot(today=None, window_days=7):
    """The numbers the daily report puts at the top. Pure local reads, no
    network, no LLM -- safe to call even when the arsenal scan is skipped
    entirely, which is exactly when the header must still be truthful.

    days_since is None when nothing dated has ever landed; callers must render
    that as "never", not as 0, or a cold start would look like a fresh landing.
    Entries dated '-' (pre-existing stock) count toward `landed` but cannot set
    days_since, since we do not know when they landed."""
    today = today or datetime.date.today().isoformat()
    rows = load_adoption()
    dated = sorted([r for r in rows if r["date"]], key=lambda r: r["date"])
    try:
        t = datetime.date.fromisoformat(today)
    except ValueError:
        t = datetime.date.today()
    recent, days_since, last = 0, None, None
    for r in dated:
        try:
            d = datetime.date.fromisoformat(r["date"])
        except ValueError:
            continue
        age = (t - d).days
        if 0 <= age < window_days:
            recent += 1
    if dated:
        last = dated[-1]
        try:
            days_since = max(0, (t - datetime.date.fromisoformat(last["date"])).days)
        except ValueError:
            days_since = None
    return {
        "candidates": _count_known_candidates(),
        "landed": len(rows),
        "landed_recent": recent,
        "window_days": window_days,
        "days_since": days_since,
        "last_id": last["id"] if last else "",
        "last_date": last["date"] if last else "",
        "stall_days": ADOPTION_STALL_DAYS,
        # never-landed is a stall too, and the loudest one
        "stall": days_since is None or days_since >= ADOPTION_STALL_DAYS,
    }


def write_star_history(hist):
    os.makedirs(ARSENAL_DIR, exist_ok=True)
    with open(STAR_HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# ---------------------------------------------------------------------------
# report section (a POINTER, not the deliverable)
# ---------------------------------------------------------------------------
def render_section(rows, added, distill_stats, max_show=12):
    """The daily report keeps a short pointer so a human CAN look, but the
    machine-readable candidates.json is the real output. Deliberately carries no
    ranking or recommendation."""
    if not rows:
        return ""
    L = [ZH["SEC_H"], "", ZH["SEC_NOTE"], "", ZH["SEC_TBL"],
         "|---|---|---|---|---|---|---|"]
    for r in rows[:max_show]:
        L.append("| [%s](%s) | %s | %s | %s | %s | %s(%s) | %s |" % (
            r["repo"], r["url"], r["stars"],
            (r.get("capability") or r["description"][:60] or "-").replace("|", "/"),
            "/".join(r.get("transfer_forms") or ["unknown"]),
            "/".join(r.get("line_candidates") or ["unknown"]),
            r.get("license", "?"), r.get("license_family", "?"),
            _DIM_LABEL.get((r.get("found_by") or {}).get("dimension"), "?")))
    L += ["", ZH["SEC_FOOT"] % (added, distill_stats.get("seat")
                                or ZH["SEC_NOSEAT"]), ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
def run(limit=60, do_distill=True, today=None):
    """Full pass. Returns (rows, section_md, meta). Never raises."""
    today = today or datetime.date.today().isoformat()
    hist = load_star_history()
    rows, per_query = collect(limit=limit, history=hist)
    update_star_history(hist, rows, today)
    for r in rows:
        d, span = star_delta(hist, r["repo"], today)
        r["stars_delta"] = d
        r["stars_delta_window_days"] = span
    if do_distill:
        rows, dstats = distill(rows)
    else:
        dstats = {"seat": None, "calls": 0, "ok": 0}
    scan_stats = {
        "queries": len(per_query),
        "search_calls": _state["search_calls"],
        "search_budget": SEARCH_BUDGET,
        "errors": _state["errors"],
        "authenticated": bool(_token()),
        # a query is "dead" only if it ANSWERED and answered with nothing; one
        # that failed on the wire is reported separately so a network blip never
        # gets a working dimension pruned
        "dead_queries": [q for q, s in per_query.items()
                         if s["returned"] == 0 and not s["failed"]],
        "failed_queries": [q for q, s in per_query.items() if s["failed"]],
    }
    write_candidates(rows, today, scan_stats, dstats)
    _, added = merge_arsenal(rows, today)
    write_star_history(hist)
    return rows, render_section(rows, added, dstats), {"scan": scan_stats, "distill": dstats}


# ---------------------------------------------------------------------------
SELFTEST_TARGETS = [
    "yikart/AiToEarn",
    "tashfeenahmed/freellmapi",
    "tmstack/awesome-persona-skills",
    "alchaincyf/zhangxuefeng-skill",
    "YouMind-OpenLab/ai-image-prompts-skill",
]


def selftest():
    """Regression test for the myopia fix: the scan plan MUST still surface the
    five repos the founder had to hand us by hand on 2026-07-26. Runs the real
    API -- a mocked version of this test would prove nothing."""
    print("[selftest] scanning for the 5 repos the old radar missed ...", flush=True)
    rows, per_query = collect(limit=10 ** 9, include_known=True, apply_select=False)
    found, rank = {}, {}
    for i, r in enumerate(rows, 1):
        if r["repo"] in SELFTEST_TARGETS:
            found[r["repo"]] = r["found_by"]
            rank[r["repo"]] = i
    print("\n--- coverage ---")
    for tgt in SELFTEST_TARGETS:
        fb = found.get(tgt)
        print("%-4s %-42s rank=%-5s %s"
              % ("HIT" if fb else "MISS", tgt, rank.get(tgt, "-"),
                 (fb or {}).get("query", "")[:56]))
    dead = [q for q, s in per_query.items() if s["returned"] == 0 and not s["failed"]]
    bad = [q for q, s in per_query.items() if s["failed"]]
    if dead:
        print("\n--- answered with 0 results (real prune candidates) ---")
        for q in dead:
            print("  " + q[:90])
    if bad:
        print("\n--- failed on the wire (NOT prune candidates, retry) ---")
        for q in bad:
            print("  " + q[:90])
    print("\nsearch calls used: %d/%d | errors: %d"
          % (_state["search_calls"], SEARCH_BUDGET, _state["errors"]))
    ok = len(found) == len(SELFTEST_TARGETS)
    print("\nRESULT: %s (%d/%d)" % ("PASS" if ok else "FAIL",
                                    len(found), len(SELFTEST_TARGETS)))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="deep github radar -> council input")
    ap.add_argument("--selftest", action="store_true",
                    help="verify the scan plan still hits the 5 known-missed repos")
    ap.add_argument("--no-distill", action="store_true", help="skip LLM distillation")
    ap.add_argument("--limit", type=int, default=60)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    rows, section, meta = run(limit=a.limit, do_distill=not a.no_distill)
    print("\n" + section)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
