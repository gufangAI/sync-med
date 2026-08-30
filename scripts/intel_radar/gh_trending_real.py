#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Real GitHub trending, as opposed to what the radar was calling "trending".

Why this file exists
--------------------
`daily_report_v3.fetch_github_trending()` queries the Search API with
`created:>{7 days ago}`. That only ever surfaces repositories *created* in the
last week -- it is a new-repo feed, not a trending feed. A project that gained
40,000 stars this week but was created in 2023 is invisible to it. The radar's
own docstring already admitted this ("can only ever see brand-new").

Trending is defined by *star velocity*, which the Search API does not expose at
all. So the two sources that actually publish it:

  1. github.com/trending -- the official board. No API, so the HTML is parsed.
     The markup is stable (`<article class="Box-row">`), and the parse degrades
     to an empty list rather than raising, so a layout change costs us this one
     source for a day instead of breaking the whole radar run.

  2. OpenGithubs/github-weekly-rank -- a repo that publishes the top-20 by
     weekly star *growth* every Monday. This one needs no scraping at all: it
     is a GitHub repository, so the Contents API reads it directly. Weekly
     growth is the number the official board does not give us numerically.

Deliberately no crawler dependency. Both sources are one HTTP GET and a regex
away; pulling in a headless-browser scraper for two static pages would add a
heavyweight dependency, and heavy local compute is against our standing rules.
Everything here runs inside Actions on stdlib only.

Usage:
    from gh_trending_real import fetch_trending_all
    items = fetch_trending_all()          # unified dicts, same shape as the
                                          # radar's other fetch_* functions

    python gh_trending_real.py            # standalone smoke run

KNOWN GAP (2026-08-31, not yet solved -- stated rather than hidden)
------------------------------------------------------------------
`fetch_trending_html` returns 0 rows against the live board, while every step
of it verifies correctly in isolation: _get() returns 669KB, the split yields
19 blocks, and the h2 regex matches "THU-MAIC/OpenMAIC" on blocks[0]. Run as a
whole, the loop still appends nothing. Cause not identified.

Time-boxed out under the no-rabbit-hole rule (30 minutes on one point with no
verifiable output -> stop and report, do not keep guessing).

This does NOT block the feature. `fetch_weekly_rank` works and is the more
valuable of the two: it yields actual weekly star *growth* numbers, which the
official board never exposes numerically. Verified live -- 20 repos, top entry
+6,969 stars in a week.

Next step when someone picks this up: instrument inside the loop body itself
(print len(blocks), and the value of `full` per iteration) rather than testing
the regex again in isolation -- isolation already passes, so the divergence is
somewhere between the loop and the append, not in the pattern.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

TRENDING_URL = "https://github.com/trending"
RANK_REPO = "OpenGithubs/github-weekly-rank"
UA = "IntelRadar/3.0 (SueAI)"


def _headers(github_api: bool = False) -> dict:
    """Auth header when a token exists.

    Same reasoning as the radar's own `_gh_api_headers`: an unauthenticated
    call sits in the per-IP anonymous bucket, shared with every other job on
    the same runner. GH_TOKEN is already injected by the workflow.
    """
    h = {"User-Agent": UA}
    if github_api:
        h["Accept"] = "application/vnd.github+json"
        h["X-GitHub-Api-Version"] = "2022-11-28"
    tok = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    if tok and github_api:
        h["Authorization"] = "Bearer " + tok
    return h


def _get(url: str, timeout: int = 30, github_api: bool = False) -> str:
    req = urllib.request.Request(url, headers=_headers(github_api))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def fetch_trending_html(spoken: str = "", lang: str = "", since: str = "daily") -> list:
    """Parse github.com/trending.

    Returns [] on any failure. A dead source must not take down the radar run --
    the rest of the pipeline treats an empty list as "nothing new today", which
    is the correct degradation for one source out of a dozen.
    """
    q = {}
    if lang:
        q["language"] = lang
    if spoken:
        q["spoken_language_code"] = spoken
    if since in ("weekly", "monthly"):
        q["since"] = since
    url = TRENDING_URL + ("?" + urllib.parse.urlencode(q) if q else "")
    try:
        html = _get(url)
    except Exception as exc:                                     # noqa: BLE001
        print("    [trending] fetch failed: %s" % str(exc)[:90], flush=True)
        return []

    out = []
    # Each entry is one <article class="Box-row">. Split on it rather than
    # trying to match the whole card in one regex -- the inner markup varies
    # (sponsor badges, built-by avatars), the outer wrapper does not.
    for block in re.split(r'<article class="Box-row"', html)[1:]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*?href="/([^"]+?)"', block, re.S)
        if not m:
            continue
        full = m.group(1).strip("/")
        if full.count("/") != 1:
            continue
        dm = re.search(r'<p class="col-9[^"]*">(.*?)</p>', block, re.S)
        desc = _clean(dm.group(1)) if dm else ""
        sm = re.search(r'stargazers"[^>]*>(.*?)</a>', block, re.S)
        stars = 0
        if sm:
            t = _clean(sm.group(1)).replace(",", "")
            try:
                stars = int(float(t[:-1]) * 1000) if t.endswith("k") else int(t)
            except ValueError:
                stars = 0
        tm = re.search(r'itemprop="programmingLanguage">([^<]+)<', block)
        gm = re.search(r'([\d,]+)\s*stars\s+(?:today|this week|this month)', _clean(block))
        gain = int(gm.group(1).replace(",", "")) if gm else 0
        out.append({
            "id": "trend:" + full,
            "title": full,
            "abstract": "%s | stars %s | gained %s (%s)"
                        % (desc[:200], stars, gain, since),
            "url": "https://github.com/" + full,
            "source": "GitHub Trending Board",
            "stars": stars,
            "gain": gain,
            "lang": tm.group(1).strip() if tm else "",
        })
    print("    [trending/%s] %d repos" % (since, len(out)), flush=True)
    return out


def fetch_weekly_rank(limit: int = 20) -> list:
    """Read OpenGithubs/github-weekly-rank through the Contents API.

    No scraping: it is a repository, so we walk its tree and read the newest
    markdown file. This source is the only one that gives weekly star *growth*
    as a number, which is what "trending" actually means.
    """
    try:
        tree = json.loads(_get(
            "https://api.github.com/repos/%s/git/trees/HEAD?recursive=1" % RANK_REPO,
            github_api=True))
    except Exception as exc:                                     # noqa: BLE001
        print("    [weekly-rank] tree failed: %s" % str(exc)[:90], flush=True)
        return []

    mds = [n["path"] for n in tree.get("tree", [])
           if n.get("type") == "blob" and n["path"].lower().endswith(".md")
           and "readme" not in n["path"].lower()]
    if not mds:
        print("    [weekly-rank] no dated markdown found", flush=True)
        return []
    newest = sorted(mds)[-1]

    try:
        blob = json.loads(_get(
            "https://api.github.com/repos/%s/contents/%s"
            % (RANK_REPO, urllib.parse.quote(newest)), github_api=True))
        import base64
        md = base64.b64decode(blob.get("content", "")).decode("utf-8", "replace")
    except Exception as exc:                                     # noqa: BLE001
        print("    [weekly-rank] read failed: %s" % str(exc)[:90], flush=True)
        return []

    out = []
    for line in md.split("\n"):
        m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", line)
        if not m:
            continue
        full = m.group(1).rstrip(")/")
        gain = 0
        for n in re.findall(r"\d[\d,]*", line.replace(full, "")):
            try:
                v = int(n.replace(",", ""))
            except ValueError:
                continue
            if v > gain:
                gain = v
        out.append({
            "id": "wrank:" + full,
            "title": full,
            "abstract": "weekly star growth ~%s | source %s" % (gain, newest),
            "url": "https://github.com/" + full,
            "source": "GitHub Weekly Rank",
            "stars": 0,
            "gain": gain,
            "lang": "",
        })
        if len(out) >= limit:
            break
    print("    [weekly-rank] %d repos from %s" % (len(out), newest), flush=True)
    return out


def fetch_trending_all() -> list:
    """All real-trending sources, deduplicated by full_name.

    Kept separate from the radar's existing fetch_github_trending() rather than
    replacing it: that one is a legitimate new-repo feed, just mislabelled.
    Both feed the same downstream scoring.
    """
    print("[fetch] real GitHub trending (board + weekly rank) ...", flush=True)
    items = []
    for since in ("daily", "weekly"):
        items += fetch_trending_html(since=since)
    items += fetch_weekly_rank()

    seen, out = set(), []
    for it in items:
        key = it["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    print("  real trending total: %d unique repos" % len(out), flush=True)
    return out


if __name__ == "__main__":
    got = fetch_trending_all()
    for x in sorted(got, key=lambda r: -(r.get("gain") or 0))[:15]:
        print("  %-44s gain=%-8s stars=%-8s %s"
              % (x["title"][:44], x.get("gain"), x.get("stars"), x["source"]))
    sys.exit(0 if got else 1)
