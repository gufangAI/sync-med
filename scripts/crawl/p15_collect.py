#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P15 batch collector: manifest-driven L0 collection for research tickets.

What this is, in plain terms: the research bureau's harvester. A ticket
manifest (research/R2/manifest.json etc.) declares seed queries hung on a
decision question; this script turns each query into result URLs via
DuckDuckGo's HTML endpoint (no key, no account -- public endpoint doctrine),
crawls each page with crawl4ai, and files L0 records.

Where things land (public-repo discipline):
  research/<ticket>/l0_index_<date>.jsonl   committed: ASCII-only index rows
                                            (url, ts, sha256, chars, query)
  p15_l0_full_<ticket>_<date>.jsonl         artifact-only: full extracted text.
                                            Substantive content stays out of
                                            the public repo.

Consulting rails enforced here (P15 contract):
  - every datapoint carries url + fetch timestamp (repro rule)
  - L0 stores, never judges: no summarising, no opinions, raw text only
  - zero metered AI: this stage calls no model at all

Exit: 0 when >= MIN_OK pages stored, else 1 (a collector that quietly
gathers nothing is worse than a red job).
"""
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TICKET = os.environ.get("P15_TICKET", "R2").strip()
PER_QUERY = int(os.environ.get("P15_PER_QUERY", "5"))
MAX_PAGES = int(os.environ.get("P15_MAX_PAGES", "10"))
MIN_OK = int(os.environ.get("P15_MIN_OK", "5"))
UA = "IntelRadar/3.0 (SueAI research; contact via repo)"


def ddg_urls(query, limit):
    """DuckDuckGo HTML endpoint -> result URLs. Fails soft to []."""
    try:
        q = urllib.parse.quote(query)
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/?q=" + q, headers={"User-Agent": UA})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        out = []
        for m in re.finditer(r'href="[^"]*?uddg=([^"&]+)', html):
            u = urllib.parse.unquote(m.group(1))
            if u.startswith("http") and "duckduckgo" not in u and u not in out:
                out.append(u)
            if len(out) >= limit:
                break
        return out
    except Exception as exc:                                     # noqa: BLE001
        print("  search failed [%s]: %s" % (query[:40], str(exc)[:60]))
        return []


async def crawl_all(urls):
    from crawl4ai import AsyncWebCrawler
    out = []
    async with AsyncWebCrawler() as crawler:
        for u in urls:
            t0 = time.time()
            try:
                r = await crawler.arun(url=u)
                md = (r.markdown or "") if r.success else ""
                out.append({"url": u, "ok": bool(md.strip()), "chars": len(md),
                            "sha256": hashlib.sha256(md.encode()).hexdigest(),
                            "fetched_at": int(time.time()),
                            "ms": int((time.time() - t0) * 1000), "md": md})
            except Exception as exc:                             # noqa: BLE001
                out.append({"url": u, "ok": False, "chars": 0, "sha256": "",
                            "fetched_at": int(time.time()), "err": str(exc)[:80],
                            "ms": int((time.time() - t0) * 1000), "md": ""})
    return out


def main():
    mp = "research/%s/manifest.json" % TICKET
    if not os.path.isfile(mp):
        print("manifest missing: %s" % mp)
        return 1
    man = json.load(io.open(mp, encoding="utf-8"))
    queries = (man.get("source_seeds") or {}).get("queries") or []
    print("ticket %s: %d seed queries, decision question: %s"
          % (TICKET, len(queries), str(man.get("decision_question"))[:70]))

    urls, seen = [], set()
    for q in queries:
        got = ddg_urls(q, PER_QUERY)
        print("  [%d urls] %s" % (len(got), q[:50]))
        for u in got:
            if u not in seen:
                seen.add(u)
                urls.append({"url": u, "query": q})
        if len(urls) >= MAX_PAGES:
            break
        time.sleep(1.2)                    # be gentle to the search endpoint
    urls = urls[:MAX_PAGES]
    if not urls:
        print("no urls from any query -- stopping red")
        return 1

    results = asyncio.run(crawl_all([x["url"] for x in urls]))
    for meta, r in zip(urls, results):
        r["query"] = meta["query"]
    ok = [r for r in results if r["ok"]]
    print("crawled %d ok / %d attempted" % (len(ok), len(results)))

    day = time.strftime("%Y%m%d")
    os.makedirs("research/%s" % TICKET, exist_ok=True)
    with io.open("research/%s/l0_index_%s.jsonl" % (TICKET, day), "a",
                 encoding="utf-8", newline="\n") as fh:
        for r in results:
            fh.write(json.dumps({k: r[k] for k in
                                 ("url", "ok", "chars", "sha256", "fetched_at", "ms", "query")},
                                ensure_ascii=True) + "\n")
    io.open("p15_l0_full_%s_%s.jsonl" % (TICKET, day), "w",
            encoding="utf-8", newline="\n").write(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n")

    print()
    print("P15 %s NUMBERS: stored=%d / attempted=%d  (cumulative target %s)"
          % (TICKET, len(ok), len(results), man.get("l0_target_sources")))
    return 0 if len(ok) >= MIN_OK else 1


if __name__ == "__main__":
    sys.exit(main())
