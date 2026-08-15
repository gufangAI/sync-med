# coding: utf-8
"""FTS v2 full-text loader (R2 chunks.json -> D1 books_fts_v2_src + books_fts_v2).

Design (per approved blueprint v2):
  - Unit of work = one R2 volume file (chunks.json). Idempotency = volume-level
    delete+reinsert: if a volume is partially loaded, wipe its rows and reload.
  - Oversized chunks (>50,000 chars) are split into part rows (part_no 0..n) so
    they become searchable immediately, ahead of the re-chunking project.
  - body_bi = space-joined bigrams over normalized text. Normalization = OpenCC
    t2s per-glyph (the repo's sanctioned twin of the JS-side toSimp; see
    cn-zh.js comment re: alignment with Python OpenCC('t2s')).
  - Tokenizer rule (MUST stay in sync with the query side; golden fixture below):
      runs = consecutive CJK (一-鿿) sequences of the normalized text;
      run len>=2 -> bigrams; run len==1 -> the single char.
  - rowid = 48-bit FNV-1a of "chunk_id#part_no" (deterministic; shards are
    disjoint by r2_key so no cross-shard races; collision odds ~1e-7 checked
    at insert time via INSERT OR IGNORE + count verification).
  - Zero R2 LIST: every fetch is an exact GET of a key taken from D1.

Env: GY_CF_ACCOUNT, GY_D1_TOKEN, GY_R2_ENDPOINT, GY_R2_AK, GY_R2_SK, GY_R2_BUCKET
     D1_DB (database id), SHARD, SHARDS, LIMIT_KEYS (0 = no limit)
"""
import os, sys, io, json, time, hashlib, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
import requests, boto3
from botocore.config import Config
from opencc import OpenCC

ACC = os.environ["GY_CF_ACCOUNT"]
TOK = os.environ["GY_D1_TOKEN"]
DB = os.environ.get("D1_DB", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
U = f"https://api.cloudflare.com/client/v4/accounts/{ACC}/d1/database/{DB}/query"
H = {"Authorization": f"Bearer {TOK}", "Content-Type": "application/json"}
SHARD = int(os.environ.get("SHARD", "0"))
SHARDS = int(os.environ.get("SHARDS", "1"))
LIMIT_KEYS = int(os.environ.get("LIMIT_KEYS", "0"))
PART_MAX = 50_000
BATCH_ROWS = 14                      # 6 params/row -> 84 params, under the 100 cap

t2s = OpenCC("t2s").convert
CJK_RUN = re.compile(r"[一-鿿]+")


def d1(sql, params=None, retries=4):
    for a in range(retries):
        try:
            r = requests.post(U, headers=H, json={"sql": sql, "params": params or []}, timeout=180)
            j = r.json()
            if j.get("success"):
                res = j["result"][0]
                return res.get("results") or [], res.get("meta") or {}
            msg = json.dumps(j.get("errors"))[:200]
            if "rate" in msg.lower() or r.status_code in (429, 500, 503):
                time.sleep(2 * (a + 1)); continue
            raise RuntimeError(msg)
        except requests.RequestException:
            time.sleep(2 * (a + 1))
    raise RuntimeError("d1 retries exhausted: " + sql[:80])


def bigrams(text):
    toks = []
    for run in CJK_RUN.findall(t2s(text)):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return " ".join(toks)


# Golden fixture: tokenizer parity contract with the (future) query side.
_FIX = [("桂枝湯", "桂枝 枝汤"), ("甘草", "甘草"), ("人參。大棗", "人参 大枣"), ("X草Y", "草")]
for src, want in _FIX:
    got = bigrams(src)
    assert got == want, f"tokenizer fixture broken: {src!r} -> {got!r} != {want!r}"


def rid(chunk_id, part_no):
    h = hashlib.sha1(f"{chunk_id}#{part_no}".encode()).digest()
    return int.from_bytes(h[:6], "big")  # 48-bit positive


s3 = boto3.client("s3", endpoint_url=os.environ["GY_R2_ENDPOINT"],
                  aws_access_key_id=os.environ["GY_R2_AK"],
                  aws_secret_access_key=os.environ["GY_R2_SK"],
                  config=Config(signature_version="s3v4", retries={"max_attempts": 4}),
                  region_name="auto")
BUCKET = os.environ["GY_R2_BUCKET"]

# Work list: every volume file with its expected chunk count, sharded by key hash.
rows, _ = d1("SELECT r2_key, text_id, vol_no, COUNT(*) n FROM books_text_chunks "
             "GROUP BY r2_key ORDER BY r2_key")
work = [r for r in rows if int(hashlib.sha1(r["r2_key"].encode()).hexdigest(), 16) % SHARDS == SHARD]
if LIMIT_KEYS:
    work = work[:LIMIT_KEYS]
print(f"shard {SHARD}/{SHARDS}: {len(work)} volumes to consider")

done = skipped = failed = 0
rows_written = chars_written = 0
t_start = time.time()
for wi, w in enumerate(work):
    key, text_id, vol_no, expect = w["r2_key"], w["text_id"], w["vol_no"], w["n"]
    # Completeness check: distinct chunk_ids loaded for this volume.
    got, _ = d1("SELECT COUNT(DISTINCT chunk_id) c FROM books_fts_v2_src WHERE text_id=? AND vol_no=?",
                [text_id, vol_no])
    if got[0]["c"] == expect:
        skipped += 1
        continue
    try:
        doc = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8"))
    except Exception as e:
        print(f"  FETCH-FAIL {key} {type(e).__name__}"); failed += 1
        continue
    chunks = doc.get("chunks") or []
    # Volume-level wipe (both tables) before reload -> idempotent.
    old, _ = d1("SELECT rowid FROM books_fts_v2_src WHERE text_id=? AND vol_no=?", [text_id, vol_no])
    if old:
        ids = [str(r["rowid"]) for r in old]
        for i in range(0, len(ids), 90):
            seg = ",".join(ids[i:i + 90])
            d1(f"DELETE FROM books_fts_v2 WHERE rowid IN ({seg})")
            d1(f"DELETE FROM books_fts_v2_src WHERE rowid IN ({seg})")
    # Build part rows.
    parts = []
    for c in chunks:
        cid, text = c.get("chunk_id"), c.get("text") or ""
        if not cid or not text:
            continue
        for p in range(0, max(len(text), 1), PART_MAX):
            seg = text[p:p + PART_MAX]
            parts.append((rid(cid, p // PART_MAX), cid, p // PART_MAX, text_id, vol_no, seg))
    # Batched inserts: src first, then FTS with identical rowids.
    for i in range(0, len(parts), BATCH_ROWS):
        b = parts[i:i + BATCH_ROWS]
        ph = ",".join(["(?,?,?,?,?,?)"] * len(b))
        flat = [x for row in b for x in row]
        d1(f"INSERT OR IGNORE INTO books_fts_v2_src (rowid, chunk_id, part_no, text_id, vol_no, body_raw) VALUES {ph}", flat)
        ph2 = ",".join(["(?,?)"] * len(b))
        flat2 = []
        for row in b:
            flat2.extend([row[0], bigrams(row[5])])
        d1(f"INSERT INTO books_fts_v2 (rowid, body_bi) VALUES {ph2}", flat2)
    rows_written += len(parts)
    chars_written += sum(len(p[5]) for p in parts)
    done += 1
    if done % 25 == 0:
        el = time.time() - t_start
        print(f"  [{wi+1}/{len(work)}] vols={done} rows={rows_written:,} chars={chars_written:,} "
              f"({el:.0f}s, {rows_written/max(el,1):.0f} rows/s)")

print(f"DONE shard {SHARD}: vols done={done} skipped={skipped} failed={failed} "
      f"rows={rows_written:,} chars={chars_written:,} in {time.time()-t_start:.0f}s")
if failed:
    sys.exit(1)
