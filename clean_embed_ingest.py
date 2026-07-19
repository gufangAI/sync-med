# coding: utf-8
# Clean-text ingest into tcm-rag-clean-768 (cloud-native, replaces the local process
# that silently dies). Runs inside a GitHub Actions matrix shard (ubuntu-latest hosted
# runner). Mirrors the vectorize_ingest.py pattern already proven in this repo.
#
# Pipeline: R2 clean_text/<source>/<key>.txt -> chunk (~700 chars, 80 overlap)
#   -> Xunfei embeddings (768-d) -> Vectorize REST upsert -> tcm-rag-clean-768.
#
# ONLY writes to tcm-rag-clean-768. Never touches tcm-rag-768 / tcm-rag-xf / any other index.
#
# Source discovery: reads R2 clean_text/_manifest.json (small file, NOT a bucket scan) ->
#   shard by (index % TOTAL == SHARD). Zero list_objects/get_paginator anywhere (repo-wide
#   CI guard fails the build otherwise).
#
# Idempotency: per-book done marker at clean_text/_done/<source>_<key_md5_8>.done (0-byte).
#   Checked before re-embedding; even if re-inserted, Vectorize upsert on the same id
#   overwrites (double idempotent). Never deletes/overwrites other existing R2 objects.
#
# Embedding endpoint (must match guyaofang-web/functions/api/gateway/_embed.js exactly):
#   host does NOT include /v2; endpoint path already carries /v2/embeddings ->
#   concat as host + "/v2/embeddings", never double /v2.
#
# Env vars (injected via GitHub Actions secrets):
#   R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY   -- R2 S3-compatible (read clean_text/, write _done/)
#   CF_ACCOUNT_ID -- Cloudflare account id
#   CF_API_TOKEN_SCOPED -- Vectorize REST auth, preferred (scoped token: Vectorize Write +
#     Account Analytics Read, this account only; added 2026-07-16 credential rotation)
#   CF_GLOBAL_EMAIL, CF_GLOBAL_API_KEY -- legacy Global Key auth, used only as fallback
#     when CF_API_TOKEN_SCOPED is not set (kept for rollback; see cf_auth_headers())
#   XF_KEYS                                      -- Xunfei key pool, comma/space separated "appid:key" or bare key
#   SHARD, TOTAL                                 -- matrix shard index
#   VEC_INDEX (default tcm-rag-clean-768)
import os, sys, re, json, time, hashlib, io
from concurrent.futures import ThreadPoolExecutor
import boto3
import requests
from botocore.config import Config

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BUCKET = "guyaofang-lib"
PREFIX = "clean_text/"
DONE_PREFIX = PREFIX + "_done/"
MANIFEST_KEY = PREFIX + "_manifest.json"

INDEX = os.environ.get("VEC_INDEX", "tcm-rag-clean-768")
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

# Xunfei embedding endpoint -- host WITHOUT /v2 (differs from the OCR/chat XF_BASE convention
# which already includes /v2). Keep this separate constant so the two conventions never collide.
XF_EMB_HOST = os.environ.get("XF_EMB_HOST", "https://maas-api.cn-huabei-1.xf-yun.com")
XF_EMB_PATH = "/v2/embeddings"
XF_EMB_MODEL = os.environ.get("XF_EMB_MODEL", "xop3qwen8bembedding")
EXPECT_DIM = 768

CHUNK = 700
OVERLAP = 80
META_TEXT_MAX = 1800
MAX_ROWS_PER_REQ = 1000
MIN_BOOK_CHARS = 200
MAX_CHUNKS_PER_BOOK = int(os.environ.get("MAX_CHUNKS_PER_BOOK", "60"))  # even sampling cap, same as local pipeline
EMB_WORKERS = int(os.environ.get("EMB_WORKERS", "6"))  # per-shard concurrency against one key


def parse_keys(raw):
    raw = (raw or "").strip()
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [k.strip() for k in v if ":" in str(k)]
    except Exception:
        pass
    parts = re.split(r"[\s,]+", raw)
    return [p.strip() for p in parts if ":" in p]


KEYS = parse_keys(os.environ.get("XF_KEYS", ""))
if not KEYS:
    raise SystemExit("no XF_KEYS")
XF_KEY = KEYS[SHARD % len(KEYS)]  # bind one account per shard, avoid cross-shard quota collision


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5}),
    )


def key_hash(source, key):
    return hashlib.md5((source + "|" + key).encode("utf-8")).hexdigest()[:8]


def split_chunks(t):
    t = t.strip()
    out = []
    i = 0
    while i < len(t):
        out.append(t[i:i + CHUNK])
        i += CHUNK - OVERLAP
    return out


def embed(text, tries=4):
    for a in range(tries):
        try:
            s = requests.Session(); s.trust_env = False
            r = s.post(XF_EMB_HOST + XF_EMB_PATH,
                       headers={"Authorization": "Bearer " + XF_KEY, "Content-Type": "application/json"},
                       json={"model": XF_EMB_MODEL, "input": text}, timeout=60)
            if r.status_code == 200:
                return r.json()["data"][0]["embedding"]
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 + a * 3); continue
            print("  [embed HTTP %s] %s" % (r.status_code, r.text[:120]), flush=True)
        except Exception as e:
            print("  [embed EXC] %s" % str(e)[:120], flush=True)
        time.sleep(2)
    return None


def upsert_rows(rows, tries=4):
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
                time.sleep(min(2 ** attempt, 30)); continue
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            time.sleep(min(2 ** attempt, 30)); last_err = str(e)
    return False, "exhausted retries: " + (locals().get("last_err") or "")


def main():
    s3 = s3_client()

    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = manifest["books"]   # [{"source":.., "key":.., "book":..}, ...]
    mine = [b for i, b in enumerate(books) if i % TOTAL == SHARD]
    print(f"[shard {SHARD}/{TOTAL}] key#{SHARD % len(KEYS)} manifest={len(books)} books, mine={len(mine)}", flush=True)

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

    for i, item in enumerate(mine, 1):
        source, key, label = item["source"], item["key"], item.get("book", item["key"])
        bh = key_hash(source, key)
        done_key = DONE_PREFIX + source + "_" + bh + ".done"
        lkey = source + "_" + bh

        if lkey in ledger:
            skip_books += 1
            continue

        src_key = "%s%s/%s.txt" % (PREFIX, source, key)
        try:
            text = s3.get_object(Bucket=BUCKET, Key=src_key)["Body"].read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[{i}/{len(mine)}] GET_FAIL {source}/{key[:40]} :: {e}", flush=True)
            fail_books += 1
            continue

        if not text or len(text.strip()) < MIN_BOOK_CHARS:
            s3.put_object(Bucket=BUCKET, Key=done_key, Body=b"")  # empty book, mark done to avoid re-reading forever
            ledger.add(lkey)
            skip_books += 1
            continue

        chunks_all = split_chunks(text)
        if MAX_CHUNKS_PER_BOOK and len(chunks_all) > MAX_CHUNKS_PER_BOOK:
            step = len(chunks_all) / float(MAX_CHUNKS_PER_BOOK)
            picks = sorted(set(int(j * step) for j in range(MAX_CHUNKS_PER_BOOK)))
            chunks = [(j, chunks_all[j]) for j in picks]
        else:
            chunks = list(enumerate(chunks_all))

        vecs = [None] * len(chunks)
        def emb_one(idx): vecs[idx] = embed(chunks[idx][1])
        with ThreadPoolExecutor(max_workers=EMB_WORKERS) as ex:
            list(ex.map(emb_one, range(len(chunks))))

        rows = []
        for (orig_idx, c), v in zip(chunks, vecs):
            if isinstance(v, list) and len(v) == EXPECT_DIM:
                rows.append({
                    "id": "%s_%s#%d" % (source, bh, orig_idx),
                    "values": v,
                    "metadata": {"book": label, "source": source, "text": c[:META_TEXT_MAX], "page": None},
                })
        if not rows:
            print(f"[{i}/{len(mine)}] FAIL_ALL_EMBED {source}/{key[:40]}", flush=True)
            fail_books += 1
            continue

        book_ok = True
        for start in range(0, len(rows), MAX_ROWS_PER_REQ):
            chunk_rows = rows[start:start + MAX_ROWS_PER_REQ]
            ok, info = upsert_rows(chunk_rows)
            if not ok:
                print(f"[{i}/{len(mine)}] UPSERT_FAIL {source}/{key[:40]} @{start} :: {info}", flush=True)
                book_ok = False
                break

        if book_ok:
            s3.put_object(Bucket=BUCKET, Key=done_key, Body=b"")
            ledger.add(lkey)
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
