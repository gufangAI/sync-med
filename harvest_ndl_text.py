#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# NDL 古籍全文采集(Actions runner 直连)-> 直写 R2 语料桶。
# CTO 2026-07-05:一步到位直写 R2 持久存储,不经 upload-artifact(临时缓存=白干)。
# 全文接口:fulltext-json/{id} 返回坐标块数组,按 page 分组、组内按 (rectY,rectX) 排序、拼 contents 成整页文本。
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
BUCKET = os.environ.get("TEXT_BUCKET", "guyaofang-assets")   # 语料桶(现有语料医书就在这)
PREFIX = os.environ.get("TEXT_PREFIX", "text/ndl/")          # NDL 古籍全文语料前缀
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


def one(session, bid):
    key = f"{PREFIX}{bid}.txt"
    try:
        s3.head_object(Bucket=BUCKET, Key=key)   # 幂等:已在 R2 就跳过
        print(f"{bid}: skip(已在R2)", flush=True)
        return "skip"
    except Exception:
        pass
    try:
        text, npages = pull_fulltext(session, bid)
    except Exception as e:
        print(f"{bid}: FAIL {str(e)[:70]}", flush=True)
        return False
    if not text.strip():
        print(f"{bid}: 零文本(全文接口无内容)", flush=True)
        return False
    s3.put_object(Bucket=BUCKET, Key=key,
                  Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}{bid}._meta.json",
                  Body=json.dumps({"id": bid, "pages": npages, "chars": len(text),
                                   "source": "ndl_fulltext"}, ensure_ascii=False).encode("utf-8"))
    print(f"{bid}: {npages}页 {len(text)}字 -> R2 {BUCKET}/{key}", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--worklist", default="worklist_ndl_text.csv")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--total", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.worklist, encoding="utf-8-sig")))
    jobs = [r["id"].strip() for i, r in enumerate(rows)
            if r.get("id", "").strip() and i % a.total == a.shard]
    if a.limit:
        jobs = jobs[: a.limit]
    print(f"shard {a.shard}/{a.total}: 分到 {len(jobs)} 本 -> 直写 R2 {BUCKET}/{PREFIX}", flush=True)

    s = requests.Session()
    s.trust_env = False   # 直连 NDL(CloudFront CDN,实测无限流无封IP)

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
            print(f"  进度 {i}/{len(jobs)} · ok={ok} skip={skip} fail={fail}", flush=True)
        time.sleep(0.2)   # 温和

    print(f"\n=== shard {a.shard} 完 ok={ok} skip={skip} fail={fail} · {(time.time()-t0)/60:.1f}min ===", flush=True)


if __name__ == "__main__":
    main()
