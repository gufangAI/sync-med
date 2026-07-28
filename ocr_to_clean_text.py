# coding: utf-8
# Bridge OCR output into the RAG corpus.
#
# The OCR pipeline has been running for weeks, writing recognised pages to
# _ocr/{book_id}/page_NNNN.txt. The ingest pipeline reads clean_text/ and gets
# its book list from clean_text/_manifest.json. Nobody connected the two: the
# manifest holds 1160 books from daizhige / kanripo / siku / wiki, and exactly
# zero from OCR. Everything OCR has produced is reachable by the reader's
# full-text search and by nothing else -- it never reaches the AI.
#
# This joins them. Per book: read the page files, concatenate in page order,
# write one clean_text/ocr/{book_id}.txt, and append an entry to the manifest.
# From there the existing ingest picks it up like any other source.
#
# Zero LIST, as the standing rule requires: the page count comes from D1's
# ocr_jobs table, so keys are constructed rather than discovered. Never
# list_objects, never a paginator -- the repo's CI guard fails the build for it
# and the bill is the reason.
#
# Additive only: books already in the manifest are left alone, page files are
# never deleted, and a book whose pages are all empty is skipped rather than
# written as an empty entry.
import io
import json
import os
import sys
import time

from concurrent.futures import ThreadPoolExecutor

import boto3
import requests
from botocore.config import Config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
OCR_PREFIX = "_ocr/"
CLEAN_PREFIX = "clean_text/"
MANIFEST_KEY = CLEAN_PREFIX + "_manifest.json"
SOURCE = "ocr"                      # the manifest's source label for this line

CF_ACC = os.environ["CF_ACCOUNT_ID"]
D1_DB = os.environ["D1_DATABASE_ID"]
D1_TOK = os.environ["D1_API_TOKEN"]

LIMIT = int(os.environ.get("LIMIT", "200"))          # books per run
MIN_CHARS = int(os.environ.get("MIN_CHARS", "500"))  # below this a book is not worth a vector
DRY = os.environ.get("DRY_RUN", "") == "1"
# Page GETs dominate the wall clock; R2 handles this fanout fine and the
# operations are Class B, which is the cheap side of the bill.
FETCH_WORKERS = int(os.environ.get("FETCH_WORKERS", "16"))
# How many shelved books to probe for unrecorded OCR output. One GET each, not
# one per page, so sweeping thousands costs cents.
PROBE_LIMIT = int(os.environ.get("PROBE_LIMIT", "3000"))
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5}),
    )


def d1_query(sql, params=None):
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query"
           % (CF_ACC, D1_DB))
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError("D1 query failed: " + str(j.get("errors"))[:200])
    return (j.get("result") or [{}])[0].get("results") or []


def get_text(s3, key):
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception:
        return None


def record_bridged(book_id, pages, chars, probed):
    """Write one ledger row per bridged book.

    Two reasons this is not optional. First, a probe that is not recorded gets
    repeated every run -- the discovery is thrown away the moment the job ends.
    Second, and the reason it matters more: OCR has been running for months with
    its only progress record in a GitHub Actions cache that dies with the runner,
    so nobody -- not the dashboard, not a report, not a person asking -- can say
    what it has actually produced. Work with no record is work nobody can see.

    Failure here must never lose the bridged text: the R2 object and the manifest
    entry are already written by this point, so a D1 hiccup is reported and the
    run continues.
    """
    now = int(time.time())
    try:
        d1_query(
            "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
            "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
            "created_at, started_at, finished_at, updated_at) "
            "VALUES (?, '_bridged_to_rag', ?, 0, 'done', ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, ?)",
            [book_id, RUN_ID, "ocr-to-rag" + ("/probe" if probed else ""),
             pages, pages, "chars=%d" % chars, now, now, now, now],
        )
    except Exception as e:
        print("  WARN 台账写入失败(桥接本身已成功,只是这本在看板上看不到): %s"
              % str(e)[:160], flush=True)


def main():
    s3 = s3_client()

    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = manifest.get("books") or []
    have = {(b.get("source"), b.get("key")) for b in books}
    print("manifest 现有 %d 本,其中 source=ocr 的 %d 本"
          % (len(books), sum(1 for b in books if b.get("source") == SOURCE)), flush=True)

    # Ask which books OCR has actually finished, not which books exist.
    #
    # The first version of this queried books_assets_v2 -- every book whose
    # images are online -- and then went looking for page files. Most of those
    # books have never been through OCR, so the run spent its time issuing GETs
    # that could only 404: fifty books took over nine minutes, and at 40k books
    # it would never finish while billing Class B for the privilege.
    #
    # ocr_jobs carries one row per book with done_pages, written by ocr_ndl.py
    # as it goes. Reading from there means every key we construct is one that
    # should exist. Still zero LIST -- page numbers come from a count, not from
    # enumerating the bucket.
    known = d1_query(
        "SELECT j.book_id AS book_id, MAX(j.done_pages) AS done_pages, "
        "       COALESCE(b.book_title, j.book_id) AS book_title "
        "FROM ocr_jobs j LEFT JOIN books_assets_v2 b ON b.book_id = j.book_id "
        "WHERE j.book_id != '_ndl_pipeline' AND j.done_pages > 0 "
        "GROUP BY j.book_id ORDER BY j.book_id", [])
    known_ids = {str(r.get("book_id")) for r in known}
    print("D1 ocr_jobs 有记录的书:%d 本" % len(known), flush=True)

    # Find OCR output that no ledger knows about.
    #
    # There are two OCR lines. ocr_ndl.py (took over 2026-07-22) writes a row per
    # book into ocr_jobs. The older ocr.py does not -- it keeps progress in a
    # GitHub Actions cache (ledger.json), which evaporates with the runner and
    # cannot be read from outside. So months of output from the older line is
    # invisible to every ledger we have: nobody can say how many pages exist.
    #
    # Rather than wait for the old line to be retrofitted, probe: one GET for
    # page_0001.txt per book tells us whether that book has any OCR output at
    # all. One request per book, not per page -- still zero LIST, and Class B
    # operations at roughly $0.36 per million.
    #
    # Whatever the probe finds gets WRITTEN BACK into ocr_jobs, so the next run
    # (and the admin dashboard, and anyone asking "what has OCR actually done")
    # reads it from the ledger instead of rediscovering it. Work without a record
    # is work nobody can see.
    cand = d1_query(
        "SELECT book_id, book_title, page_count FROM books_assets_v2 "
        "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0 "
        "ORDER BY book_id LIMIT ?", [PROBE_LIMIT])
    unknown = [r for r in cand if str(r.get("book_id")) not in known_ids]
    print("待探测(有影像但 ocr_jobs 无记录):%d 本" % len(unknown), flush=True)

    def probe(r):
        bid = str(r.get("book_id") or "")
        if not bid:
            return None
        return r if get_text(s3, "%s%s/page_0001.txt" % (OCR_PREFIX, bid)) is not None else None

    found = []
    if unknown:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            for hit in ex.map(probe, unknown):
                if hit:
                    found.append(hit)
    print("探测到有 OCR 产出但台账里没有的:%d 本" % len(found), flush=True)

    rows = list(known)
    for r in found:
        rows.append({"book_id": r.get("book_id"),
                     "done_pages": int(r.get("page_count") or 0),
                     "book_title": r.get("book_title"),
                     "_probed": True})
    rows = rows[:LIMIT * 3]
    print("本轮候选合计 %d 本" % len(rows), flush=True)

    added = skipped = empty = 0
    for row in rows:
        if added >= LIMIT:
            break
        bid = str(row.get("book_id") or "")
        title = str(row.get("book_title") or bid)
        pages = int(row.get("done_pages") or 0)
        if not bid or pages <= 0:
            continue
        if (SOURCE, bid) in have:
            skipped += 1
            continue

        # Fetch pages concurrently but keep them in page order -- the text of a
        # classical work is meaningless shuffled, and a prescription split across
        # a page break has to rejoin in the right sequence.
        def one(p):
            return p, get_text(s3, "%s%s/page_%04d.txt" % (OCR_PREFIX, bid, p))

        pairs = []
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            for p, t in ex.map(one, range(1, pages + 1)):
                if t is not None:
                    pairs.append((p, t))
        pairs.sort(key=lambda x: x[0])
        got = len(pairs)
        parts = [t.strip() for _, t in pairs if t.strip()]

        body = "\n\n".join(parts).strip()
        if len(body) < MIN_CHARS:
            # Either OCR has not reached this book, or every page came back
            # empty (blank scans / all blocks below the confidence gate).
            empty += 1
            continue

        key = "%socr/%s.txt" % (CLEAN_PREFIX, bid)
        if DRY:
            print("  [dry] %s  %d/%d 页有文本  %d 字%s"
                  % (bid, got, pages, len(body), "  (探测发现)" if row.get("_probed") else ""),
                  flush=True)
        else:
            s3.put_object(Bucket=BUCKET, Key=key,
                          Body=body.encode("utf-8"),
                          ContentType="text/plain; charset=utf-8")
            books.append({"source": SOURCE, "key": bid, "book": title})
            # Record what this run did, per book, so the next one reads a ledger
            # instead of probing again -- and so the number is answerable without
            # digging through Actions logs.
            record_bridged(bid, got, len(body), bool(row.get("_probed")))
            print("  + %s《%s》 %d/%d 页  %d 字%s"
                  % (bid, title[:24], got, pages, len(body),
                     "  (探测发现,已补台账)" if row.get("_probed") else ""), flush=True)
        added += 1

    print("\n新增 %d 本 | 已在册跳过 %d | 无有效文本 %d" % (added, skipped, empty), flush=True)

    if added and not DRY:
        manifest["books"] = books
        manifest["count"] = len(books)
        manifest["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        s3.put_object(Bucket=BUCKET, Key=MANIFEST_KEY,
                      Body=json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"),
                      ContentType="application/json; charset=utf-8")
        print("manifest 已更新:%d 本(原 %d 本,纯追加)" % (len(books), len(books) - added),
              flush=True)
    elif not added:
        print("本轮没有可接入的书 —— 不动 manifest", flush=True)

    # One summary row per run, mirroring ocr_ndl's sentinel row, so the dashboard
    # can show this line's activity without anyone opening GitHub.
    if not DRY:
        now = int(time.time())
        try:
            d1_query(
                "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
                "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
                "created_at, started_at, finished_at, updated_at) "
                "VALUES ('_bridge_pipeline','_bridge_run',?,0,'done','ocr-to-rag', ?,?,?,0,0,?, ?,?,?,?)",
                [RUN_ID, len(rows), added, skipped, "probed=%d empty=%d" % (len(found), empty),
                 now, now, now, now])
            print("台账已记本轮汇总行(_bridge_pipeline)", flush=True)
        except Exception as e:
            print("WARN 汇总行写入失败: %s" % str(e)[:160], flush=True)


main()
