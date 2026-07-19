# coding: utf-8


#







#





#





import os, sys, re, json, time, hashlib, io
import boto3
import requests
from botocore.config import Config

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BUCKET = "guyaofang-lib"
PREFIX = "sueai-blackbox/embeddings/"
DONE_PREFIX = PREFIX + "_done/"
MANIFEST_KEY = PREFIX + "_manifest.json"

INDEX = os.environ.get("VEC_INDEX", "tcm-rag-768")
ACCT = os.environ["CF_ACCOUNT_ID"]
# 2026-07-16 credential rotation: prefer the narrowly-scoped API Token (Vectorize Write +
# Account Analytics Read, this CF account only). Falls back to the legacy account-wide
# Global Key only if CF_API_TOKEN_SCOPED isn't set yet -- keeps old behavior as a real,
# zero-code-change rollback path. See docs/new/CF凭据轮换_sync-med_2026-07-16.md
CF_SCOPED_TOKEN = os.environ.get("CF_API_TOKEN_SCOPED", "")
EMAIL = os.environ.get("CF_GLOBAL_EMAIL", "")
GKEY = os.environ.get("CF_GLOBAL_API_KEY", "")
UPSERT_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/vectorize/v2/indexes/{INDEX}/upsert"


def cf_auth_headers():
    if CF_SCOPED_TOKEN:
        return {"Authorization": f"Bearer {CF_SCOPED_TOKEN}"}
    return {"X-Auth-Email": EMAIL, "X-Auth-Key": GKEY}

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))

META_TEXT_MAX = 1800
MAX_ROWS_PER_REQ = 1000     

PAGE_RE = re.compile('\u7b2c\\s*(\\d{1,5})\\s*\u9875')


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5}),
    )


def book_hash(book):
    return hashlib.md5(book.encode("utf-8")).hexdigest()[:8]


def extract_page(text):
    m = PAGE_RE.search(text or "")
    return int(m.group(1)) if m else None


def upsert_rows(rows, tries=4):
    """POST NDJSON to Vectorize REST upsert. Retry with backoff on 429/5xx."""
    ndjson = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    body = ndjson.encode("utf-8")
    headers = {**cf_auth_headers(), "Content-Type": "application/x-ndjson"}
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(UPSERT_URL, headers=headers, data=body, timeout=90)
            if r.status_code == 200:
                j = r.json()
                if j.get("success"):
                    return True, j.get("result", {}).get("mutationId", "")
                return False, str(j.get("errors"))[:200]
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 30))
                continue
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            time.sleep(min(2 ** attempt, 30))
            last_err = str(e)
    return False, "exhausted retries: " + (locals().get("last_err") or "")


def book_to_rows(text_bytes, book):
    bh = book_hash(book)
    rows = []
    for ln in io.TextIOWrapper(io.BytesIO(text_bytes), encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        vec = o.get("vec"); text = o.get("text") or ""; idx = o.get("idx")
        if not isinstance(vec, list) or len(vec) != 768 or not text or idx is None:
            continue
        rows.append({
            "id": "%s#%d" % (bh, int(idx)),
            "values": vec,
            "metadata": {"book": book, "text": text[:META_TEXT_MAX], "page": extract_page(text)},
        })
    return rows


def main():
    s3 = s3_client()

    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = manifest["books"]
    mine = [b for i, b in enumerate(books) if i % TOTAL == SHARD]
    print(f"[shard {SHARD}/{TOTAL}] manifest={len(books)} books, mine={len(mine)}", flush=True)

    # 2026-07-19修复:原来每本书一次 s3.head_object() 查重(每6小时对全量候选重复扫一遍),
    # 是R2 HeadObject成本异常的确认真凶之一,改用GitHub Actions cache本地ledger.json记账。
    LEDGER = "ledger.json"
    ledger = set()
    if os.path.exists(LEDGER):
        try:
            ledger = set(json.load(open(LEDGER, encoding="utf-8")))
        except Exception:
            ledger = set()
    print(f"ledger已有 {len(ledger)} 条记录", flush=True)

    ok_books, skip_books, fail_books, total_vecs = 0, 0, 0, 0
    t0 = time.time()

    for i, book in enumerate(mine, 1):
        bh = book_hash(book)
        done_key = DONE_PREFIX + bh + ".done"

        if bh in ledger:
            skip_books += 1
            continue

        src_key = PREFIX + book + ".emb.jsonl"
        try:
            body = s3.get_object(Bucket=BUCKET, Key=src_key)["Body"].read()
        except Exception as e:
            print(f"[{i}/{len(mine)}] GET_FAIL {book[:40]} :: {e}", flush=True)
            fail_books += 1
            continue

        rows = book_to_rows(body, book)
        if not rows:

            s3.put_object(Bucket=BUCKET, Key=done_key, Body=b"")
            ledger.add(bh)
            skip_books += 1
            continue

        book_ok = True
        for start in range(0, len(rows), MAX_ROWS_PER_REQ):
            chunk = rows[start:start + MAX_ROWS_PER_REQ]
            ok, info = upsert_rows(chunk)
            if not ok:
                print(f"[{i}/{len(mine)}] UPSERT_FAIL {book[:40]} chunk@{start} :: {info}", flush=True)
                book_ok = False
                break

        if book_ok:
            s3.put_object(Bucket=BUCKET, Key=done_key, Body=b"")
            ledger.add(bh)
            ok_books += 1
            total_vecs += len(rows)
            if i % 20 == 0 or i == len(mine):
                elapsed = time.time() - t0
                print(f"[{i}/{len(mine)}] ok={ok_books} skip={skip_books} fail={fail_books} "
                      f"vecs={total_vecs} · {elapsed/60:.1f}min · {elapsed/max(ok_books,1):.2f}s/book",
                      flush=True)
        else:
            fail_books += 1

    json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)

    elapsed = time.time() - t0
    print(f"=== shard {SHARD} done: ok={ok_books} skip={skip_books} fail={fail_books} "
          f"vecs={total_vecs} · {elapsed/60:.1f}min ===", flush=True)


    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"### Shard {SHARD}/{TOTAL}\n")
            f.write(f"- assigned: {len(mine)} books\n")
            f.write(f"- ok: {ok_books}  skip(already done): {skip_books}  fail: {fail_books}\n")
            f.write(f"- vectors upserted this run: {total_vecs}\n")
            f.write(f"- elapsed: {elapsed/60:.1f} min\n")

    if fail_books > 0:
        print(f"WARNING: {fail_books} books failed this shard, will retry next run (idempotent)", flush=True)


if __name__ == "__main__":
    main()
