#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P12 collector (gate 2B.2): crawl4ai sample run with a reproduction check.

Blueprint v2.1 trunk node 3: "crawl4ai sample source -> N items stored +
reproduction spot-check 100%". This is the sample that proves the collector
works before any full pipeline is built on it.

Sample source: article links pulled at runtime from the radar's existing CN
RSS feeds (qbitai / ithome). Runtime-fetched so this file stays free of CJK
literals (public-repo rule) and never goes stale.

What "stored" means here: a CJK-free summary (url / sha256 / char count /
repro verdict) is committed to data/crawl/ -- that is the evidence path the
blueprint names. The full extracted markdown goes to a run artifact only:
substantive content does not belong in a public repo.

Reproduction check: re-crawl a subset and compare title + text length within
10%. Content hash alone would flap on rotating ads; title+length is what
"same article, extracted the same way" actually looks like.

Exit: 0 = N>0 and repro 100%; 1 = anything else (the gate number must be
earned, not assumed).
"""
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FEEDS = [
    "https://www.qbitai.com/feed",
    "https://www.ithome.com/rss/",
]
N_TARGET = int(os.environ.get("P12_N", "5"))
N_REPRO = int(os.environ.get("P12_REPRO", "2"))


def feed_links():
    links = []
    for feed in FEEDS:
        try:
            req = urllib.request.Request(feed, headers={"User-Agent": "IntelRadar/3.0 (SueAI)"})
            xml = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            found = re.findall(r"<link>\s*(https?://[^<\s]+)\s*</link>", xml)
            links += [u for u in found if "/feed" not in u and u.rstrip("/") not in
                      ("https://www.qbitai.com", "https://www.ithome.com")]
        except Exception as exc:                                 # noqa: BLE001
            print("feed failed %s: %s" % (feed, str(exc)[:60]))
        if len(links) >= N_TARGET:
            break
    return links[:N_TARGET]


async def crawl(urls):
    from crawl4ai import AsyncWebCrawler
    out = []
    async with AsyncWebCrawler() as crawler:
        for u in urls:
            t0 = time.time()
            try:
                r = await crawler.arun(url=u)
                md = (r.markdown or "") if r.success else ""
                title = ""
                m = re.search(r"^#\s+(.+)$", md, re.M)
                if m:
                    title = m.group(1).strip()
                elif r.metadata and r.metadata.get("title"):
                    title = str(r.metadata["title"]).strip()
                out.append({"url": u, "ok": bool(md.strip()), "title": title,
                            "chars": len(md), "sha256": hashlib.sha256(md.encode()).hexdigest(),
                            "ms": int((time.time() - t0) * 1000), "md": md})
            except Exception as exc:                             # noqa: BLE001
                out.append({"url": u, "ok": False, "err": str(exc)[:100],
                            "chars": 0, "sha256": "", "ms": int((time.time() - t0) * 1000),
                            "title": "", "md": ""})
    return out


def main():
    urls = feed_links()
    print("sample source yielded %d urls" % len(urls))
    if not urls:
        print("no urls from either feed -- cannot run the sample")
        return 1

    first = asyncio.run(crawl(urls))
    stored = [x for x in first if x["ok"]]
    print("crawled: %d ok / %d attempted" % (len(stored), len(first)))
    for x in first:
        print("  %-4s %6dch %6dms  %s" % ("ok" if x["ok"] else "FAIL",
                                          x["chars"], x["ms"], x["url"][:70]))

    # reproduction spot-check on the first N_REPRO successes
    repro_ok = 0
    targets = stored[:N_REPRO]
    if targets:
        second = asyncio.run(crawl([x["url"] for x in targets]))
        for a, b in zip(targets, second):
            same_title = bool(a["title"]) and a["title"] == b["title"]
            close_len = a["chars"] > 0 and abs(a["chars"] - b["chars"]) <= 0.1 * a["chars"]
            ok = b["ok"] and (same_title or (a["sha256"] == b["sha256"])) and close_len
            repro_ok += 1 if ok else 0
            print("  repro %-4s title_same=%s len_close=%s  %s"
                  % ("ok" if ok else "FAIL", same_title, close_len, a["url"][:60]))
    repro_rate = (100.0 * repro_ok / len(targets)) if targets else 0.0

    day = time.strftime("%Y%m%d")
    os.makedirs("data/crawl", exist_ok=True)
    summary = {"date": day, "attempted": len(first), "stored": len(stored),
               "repro_checked": len(targets), "repro_ok": repro_ok,
               "repro_rate_pct": round(repro_rate, 1),
               "items": [{k: x[k] for k in ("url", "ok", "chars", "sha256", "ms")}
                         for x in first]}
    io.open("data/crawl/p12_sample_%s.json" % day, "w", encoding="utf-8",
            newline="\n").write(json.dumps(summary, indent=1) + "\n")

    # full markdown -> artifact only (never committed: substantive content)
    io.open("p12_full_%s.jsonl" % day, "w", encoding="utf-8", newline="\n").write(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in first) + "\n")

    print()
    print("GATE 2B.2 NUMBERS: stored=%d  repro=%d/%d (%.0f%%)"
          % (len(stored), repro_ok, len(targets), repro_rate))
    return 0 if (stored and repro_rate == 100.0) else 1


if __name__ == "__main__":
    sys.exit(main())
