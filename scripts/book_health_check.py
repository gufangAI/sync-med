# -*- coding: utf-8 -*-
"""
Shelf health check: which books on the site can a visitor actually open?

Why (founder, 2026-07-26): "the online books need to line up exactly -- some of them
just won't display". Sampling 270 shelved books found 21% that return 404 on page 1.
Before showing the site to anyone, the unreadable ones have to come off the shelf.

What counts as what -- this distinction is the whole point:
  403  = paywall doing its job          -> HEALTHY, not a fault
  200 image on page 1                   -> HEALTHY
  404 / non-image / error               -> BROKEN
A previous check called 84% of covers healthy by counting HTTP 200 + byte size; that
counted the fallback SVG placeholder as a success. Content-Type is the only honest test.

This run RECORDS ONLY -- it writes health_note into D1 and never flips
frontend_visible. Today alone I reached three confident conclusions that turned out
wrong, so nothing gets hidden until the numbers are reviewed against real samples.

Zero LIST: never enumerates R2 or the pan drive; it asks the public reader API the
same way a visitor's browser would.
"""
import argparse, json, os, sys, time, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SITE = "https://gufangai.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
D1API = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (ACC, DB)
_px = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
D1OP = urllib.request.build_opener(urllib.request.ProxyHandler({"http": _px, "https": _px} if _px else {}))
WEB = urllib.request.build_opener()

def d1(sql, tries=5):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(D1API, json.dumps({"sql": sql}).encode(),
                {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
            r = json.loads(D1OP.open(req, timeout=180).read())
            if not r.get("success"):
                raise RuntimeError(json.dumps(r.get("errors"))[:240])
            return r["result"][0].get("results", [])
        except Exception as e:
            last = e; time.sleep(2.0 * (a + 1))
    raise RuntimeError("d1 failed: %s" % str(last)[:160])

def fetch(url):
    try:
        r = WEB.open(urllib.request.Request(url, headers={"User-Agent": UA, "Referer": SITE + "/"}), timeout=40)
        ct = r.headers.get("Content-Type", "")
        n = len(r.read(200000))          # \u8bfb\u524d 200KB \u8db3\u591f\u5224\u7c7b\u578b,\u4e0d\u5fc5\u6574\u9875\u62c9\u4e0b\u6765
        if "svg" in ct: return "svg"
        if ct.startswith("image/") and n > 3000: return "img"
        return "other"
    except urllib.error.HTTPError as e:
        return "http%d" % e.code
    except Exception:
        return "err"

def judge(book_id):
    page = fetch("%s/api/reader/%s/page?p=1" % (SITE, book_id))
    if page == "http403":
        return "paywall"                  # \u4ed8\u8d39\u5899\u62e6\u4f4f = \u95e8\u7981\u5728\u5de5\u4f5c,\u4e66\u672c\u8eab\u6ca1\u95ee\u9898
    if page != "img":
        return "broken"                   # \u7b2c\u4e00\u9875\u90fd\u53d6\u4e0d\u5230 = \u8bfb\u8005\u70b9\u8fdb\u6765\u662f\u767d\u5c4f
    return "ok"
    # \u5c01\u9762\u4e0d\u5355\u72ec\u63a2\u6d4b:\u5168\u91cf\u6d4b\u5c01\u9762\u4f1a\u518d\u591a 4.5 \u4e07\u6b21\u8bf7\u6c42,\u800c Pages Functions \u514d\u8d39\u989d\u5ea6\u662f
    # 10 \u4e07\u6b21/\u5929 \u2014\u2014 \u4e3a\u4e00\u4e2a\u975e\u81f4\u547d\u7684\u7455\u75b5\u5403\u6389\u4e00\u534a\u65e5\u989d\u5ea6\u4e0d\u5212\u7b97\u3002\u7f3a\u5c01\u9762\u5355\u72ec\u53e6\u8dd1\u3002

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--total", type=int, default=1)
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--write", action="store_true", help="record verdicts into D1")
    a = ap.parse_args()

    rows = d1("SELECT book_id AS b FROM books_assets_v2 "
              "WHERE COALESCE(frontend_visible,1)=1 AND health_note IS NULL "
              "AND (rowid %% %d) = %d ORDER BY rowid LIMIT %d" % (a.total, a.shard, a.limit))
    print("[shard %d/%d] to check: %d" % (a.shard, a.total, len(rows)), flush=True)
    if not rows: return

    t0 = time.time(); out = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (r, v) in enumerate(zip(rows, ex.map(lambda r: judge(r["b"]), rows))):
            out.append((r["b"], v))
            if (i + 1) % 500 == 0:
                c = Counter(v for _, v in out)
                print("   %d/%d  ok=%d paywall=%d no_cover=%d broken=%d  %.0fs"
                      % (i + 1, len(rows), c["ok"], c["paywall"], c["no_cover"], c["broken"], time.time() - t0), flush=True)

    c = Counter(v for _, v in out)
    n = len(out)
    print("\n[shard %d] %d checked: ok=%d paywall=%d no_cover=%d broken=%d (%.1f%% broken)  %.0fs"
          % (a.shard, n, c["ok"], c["paywall"], c["no_cover"], c["broken"],
             100.0 * c["broken"] / max(n, 1), time.time() - t0), flush=True)

    if not a.write:
        print("dry run -- nothing written. Pass --write to record.")
        return
    for i in range(0, len(out), 120):
        chunk = out[i:i+120]
        cases = " ".join("WHEN '%s' THEN '%s'" % (b.replace("'", "''"), v) for b, v in chunk)
        ids = ",".join("'%s'" % b.replace("'", "''") for b, _ in chunk)
        d1("UPDATE books_assets_v2 SET health_note = CASE book_id %s END WHERE book_id IN (%s)" % (cases, ids))
    print("recorded %d verdicts (frontend_visible untouched)" % len(out), flush=True)

main()
