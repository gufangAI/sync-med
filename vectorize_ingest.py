# coding: utf-8
# 【RAG 灌库 · 云端化步骤2】GitHub Actions 矩阵内运行:R2 emb.jsonl → CF Vectorize REST API。
#   跑在 gufangAI/sync-med 仓库的 workflow 里(ubuntu-latest hosted runner,非本机、非自托管)。
#
#   认证(2026-07-01 实测,逐项核实,不沿用旧记忆):
#     - REST scoped token(CLOUDFLARE_API_TOKEN/D1_API_TOKEN)→ 401/403(没有 Vectorize 权限范围)。
#     - Global API Key(CLOUDFLARE_EMAIL + CLOUDFLARE_API_KEY,X-Auth-Email/X-Auth-Key 头)
#       → 实测 200,账号级全权限,天然带 Vectorize 权限,不受 scoped token 限制。已用它验证:
#       list indexes / info / upsert 全通,真实写入 tcm-rag-768 索引数从 3960 → 3961 → 4191。
#     - 走 REST 直连,不走 wrangler CLI 子进程 —— 消除本机"13.2秒/本子进程启动开销"瓶颈;
#       实测 REST 直连 3.08秒/本(约46块/本),比 CLI 快 4.3倍。
#
#   数据源:R2 guyaofang-lib/sueai-blackbox/embeddings/(先由本机 upload_emb_to_r2.py 一次性搬运)。
#   分片:读 R2 _manifest.json(小文件,非全桶 LIST)→ book_index % TOTAL == SHARD 分片。
#   幂等:每本书灌完在 R2 写一个 0 字节标记 sueai-blackbox/embeddings/_done/<book_md5_8>.done;
#         灌前先 HEAD 该标记,存在则跳过(不重复插入;即使重插入,Vectorize upsert 同 id 覆盖=天然幂等)。
#   只增不删不覆盖已有 R2 对象/D1 记录(铁律)。
#
#   环境变量(GitHub Actions secrets 注入):
#     CF_ACCOUNT_ID, CF_GLOBAL_EMAIL, CF_GLOBAL_API_KEY   — Vectorize REST 鉴权
#     R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY            — R2 S3 兼容读取(读 embeddings 源 + 写 done 标记)
#     SHARD, TOTAL                                          — 矩阵分片
#     VEC_INDEX (默认 tcm-rag-768)
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
EMAIL = os.environ["CF_GLOBAL_EMAIL"]
GKEY = os.environ["CF_GLOBAL_API_KEY"]
UPSERT_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/vectorize/v2/indexes/{INDEX}/upsert"

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))

META_TEXT_MAX = 1800
MAX_ROWS_PER_REQ = 1000     # REST 单文件上限 5000,保守用 1000(与本机批大小口径一致,留余量防超时)

PAGE_RE = re.compile(r"第\s*(\d{1,5})\s*页")


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
    headers = {
        "X-Auth-Email": EMAIL, "X-Auth-Key": GKEY,
        "Content-Type": "application/x-ndjson",
    }
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

    ok_books, skip_books, fail_books, total_vecs = 0, 0, 0, 0
    t0 = time.time()

    for i, book in enumerate(mine, 1):
        bh = book_hash(book)
        done_key = DONE_PREFIX + bh + ".done"

        # 幂等:标记已存在则跳过(不重复插入)
        try:
            s3.head_object(Bucket=BUCKET, Key=done_key)
            skip_books += 1
            continue
        except Exception:
            pass

        src_key = PREFIX + book + ".emb.jsonl"
        try:
            body = s3.get_object(Bucket=BUCKET, Key=src_key)["Body"].read()
        except Exception as e:
            print(f"[{i}/{len(mine)}] GET_FAIL {book[:40]} :: {e}", flush=True)
            fail_books += 1
            continue

        rows = book_to_rows(body, book)
        if not rows:
            # 空书(无有效向量)也标记 done,避免每轮重试白读
            s3.put_object(Bucket=BUCKET, Key=done_key, Body=b"")
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
            ok_books += 1
            total_vecs += len(rows)
            if i % 20 == 0 or i == len(mine):
                elapsed = time.time() - t0
                print(f"[{i}/{len(mine)}] ok={ok_books} skip={skip_books} fail={fail_books} "
                      f"vecs={total_vecs} · {elapsed/60:.1f}min · {elapsed/max(ok_books,1):.2f}s/book",
                      flush=True)
        else:
            fail_books += 1

    elapsed = time.time() - t0
    print(f"=== shard {SHARD} done: ok={ok_books} skip={skip_books} fail={fail_books} "
          f"vecs={total_vecs} · {elapsed/60:.1f}min ===", flush=True)

    # GitHub Actions job summary (self-reporting, per 纲领 "自带报告")
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
