#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import os
import time

import requests
import boto3

BASE = "https://lab.ndl.go.jp/dl/api/book"
UA = {"User-Agent": "Mozilla/5.0 (compatible; GujiArchive/1.0)"}

EP = os.environ.get("R2_ENDPOINT") or os.environ["VEC_R2_ENDPOINT"]
AK = os.environ.get("R2_KEY") or os.environ["VEC_R2_ACCESS_KEY"]
SK = os.environ.get("R2_SECRET") or os.environ["VEC_R2_SECRET_KEY"]
BUCKET = os.environ.get("TEXT_BUCKET", "guyaofang-assets")   
PREFIX = os.environ.get("TEXT_PREFIX", "text/ndl/")          
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto")

def fetch_json(session, url, timeout=60, tries=3):
    last = None
    for a in range(tries):
        try:
            r = session.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = r.status_code
        except Exception as e:
            last = str(e)[:50]
        time.sleep(2 * (a + 1))
    raise RuntimeError(f"HTTP fail: {last}")

def pull_fulltext(session, bid):
    j = fetch_json(session, f"{BASE}/fulltext-json/{bid}")
    blocks = j.get("list", []) if isinstance(j, dict) else (j or [])
    pages = {}
    for b in blocks:
        pages.setdefault(b.get("page", 0), []).append(b)
    parts = []
    for pg in sorted(pages):
        bl = sorted(pages[pg], key=lambda b: (b.get("rectY", 0), b.get("rectX", 0)))
        parts.append("".join(b.get("contents", "") or "" for b in bl))
    return "\n".join(parts), len(pages)

# 2026-07-19\u4fee\u590d:\u539f\u6bcf\u672c\u6bcf\u6b21\u4e00\u6b21s3.head_object()\u67e5\u91cd,\u6539GitHub\u7f13\u5b58\u672c\u5730_DONE\u8bb0\u8d26\u3002
_DONE = set()

def one(session, bid):
    key = f"{PREFIX}{bid}.txt"
    if bid in _DONE:
        print(f"{bid}: skip(\u5df2\u5728R2)", flush=True)
        return "skip"
    try:
        text, npages = pull_fulltext(session, bid)
    except Exception as e:
        print(f"{bid}: FAIL {str(e)[:70]}", flush=True)
        return False
    if not text.strip():
        print(f"{bid}: \u96f6\u6587\u672c(\u5168\u6587\u63a5\u53e3\u65e0\u5185\u5bb9)", flush=True)
        return False
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}{bid}._meta.json",
                  Body=json.dumps({"id": bid, "pages": npages, "chars": len(text),
                                   "source": "ndl_fulltext"}, ensure_ascii=False).encode("utf-8"))
    print(f"{bid}: {npages}\u9875 {len(text)}\u5b57 -> R2 {BUCKET}/{key}", flush=True)
    _DONE.add(bid)
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worklist", default="worklist_ndl_text.csv")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--total", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    global _DONE
    if os.path.exists("ledger.json"):
        try:
            _DONE = set(json.load(open("ledger.json", encoding="utf-8")))
        except Exception:
            _DONE = set()
    print(f"ledger已有 {len(_DONE)} 条记录", flush=True)

    rows = list(csv.DictReader(open(a.worklist, encoding="utf-8-sig")))
    jobs = [r["id"].strip() for i, r in enumerate(rows)
            if r.get("id", "").strip() and i % a.total == a.shard]
    if a.limit:
        jobs = jobs[: a.limit]
    print(f"shard {a.shard}/{a.total}: \u5206\u5230 {len(jobs)} \u672c -> \u76f4\u5199 R2 {BUCKET}/{PREFIX}", flush=True)

    s = requests.Session()
    s.trust_env = False   

    ok = fail = skip = 0
    t0 = time.time()
    for i, bid in enumerate(jobs, 1):
        r = one(s, bid)
        if r == "skip":
            skip += 1
        elif r:
            ok += 1
        else:
            fail += 1
        if i % 20 == 0:
            print(f"  \u8fdb\u5ea6 {i}/{len(jobs)} · ok={ok} skip={skip} fail={fail}", flush=True)
        time.sleep(0.2)   

    json.dump(sorted(_DONE), open("ledger.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n=== shard {a.shard} \u5b8c ok={ok} skip={skip} fail={fail} · {(time.time()-t0)/60:.1f}min ===", flush=True)

if __name__ == "__main__":
    main()
