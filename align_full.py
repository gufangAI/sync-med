# coding: utf-8
"""
Full-scale page<->text alignment (GitHub Actions matrix worker).

This reuses, unchanged, the alignment algorithm validated in the 2026-07-25
small-sample experiment (10 books, see docs/new alignment validation report):
  per-page anchor extraction (consecutive CJK run >=6 chars, top 5 by length,
  12-char anchor) -> locate by exact find (unique hit; 2-5 hits pick nearest
  to previous page's position; >5 hits = high-frequency phrase, drop) -> fuzzy
  fallback (rapidfuzz partial_ratio_alignment, score>=80, windowed around the
  previous position when known) -> page offset = median of >=2 clustered hits
  (spread <=30000) -> monotonic check (adjacent aligned pages may regress
  <=1000 normalized-CJK chars).
Only the plumbing is new for production scale: env/secrets instead of a local
.env file, shard/limit slicing over the full ~889-book population, a per
text_id full-text cache (multiple volumes/books can share one text_id's full
text -- avoid re-fetching/re-normalizing a huge text repeatedly), a 3-tier
confidence gate, defensive per-book error handling, and a --summarize mode
that merges every shard's JSON and prints the tier distribution.

Read-only / no side effects: D1 only SELECT, R2 only get_object against a key
built from book_id/page number -- never list_objects / get_paginator (that is
also enforced repo-wide by .github/workflows/guard_no_list.yml). Nothing is
written to any production table or bucket; the only output is a JSON file
(uploaded by the workflow as a build artifact).

Modes:
  (default)          -- shard worker: reads SHARD/TOTAL/LIMIT env, processes
                         this shard's slice of the population, writes
                         align_full_result.json
  --summarize DIR     -- merges every *.json under DIR, prints the tier
                         distribution, writes align_full_all.json
"""
import os, sys, re, json, glob, time, statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

T0 = time.time()
RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")

# ---------------------------------------------------------------------------
# --summarize mode: pure local merge, no network/D1/R2/opencc/rapidfuzz needed
# ---------------------------------------------------------------------------
if len(sys.argv) >= 2 and sys.argv[1] == "--summarize":
    src_dir = sys.argv[2] if len(sys.argv) >= 3 else "."
    files = sorted(glob.glob(os.path.join(src_dir, "*.json")))
    print(f"[summarize] run {RUN_ID}: merging {len(files)} shard file(s) from {src_dir}", flush=True)
    merged = []
    for f in files:
        try:
            data = json.load(open(f, encoding="utf-8"))
            merged += data
            print(f"  + {f}: {len(data)} 册", flush=True)
        except Exception as e:
            print(f"  WARN 读取 {f} 失败: {str(e)[:200]}", flush=True)

    tiers = Counter(r.get("tier", "?") for r in merged)
    known = ("A", "B", "C", "no_ocr", "no_volumes", "error")
    print(f"\n=== 全量对齐汇总(run {RUN_ID}) ===", flush=True)
    print(f"合并册数: {len(merged)}", flush=True)
    for k in known:
        print(f"  tier {k:10s}: {tiers.get(k, 0)}", flush=True)
    for k in sorted(set(tiers) - set(known)):
        print(f"  tier {k:10s}: {tiers[k]}", flush=True)

    with_ap = [r["anchor_pct"] for r in merged if isinstance(r.get("anchor_pct"), (int, float))]
    if with_ap:
        print(f"锚定% 均值(排除no_ocr/no_volumes/error): {round(sum(with_ap)/len(with_ap),1)}", flush=True)

    out = "align_full_all.json"
    json.dump(merged, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"合并结果 -> {out}", flush=True)
    sys.exit(0)

# ---------------------------------------------------------------------------
# worker mode
# ---------------------------------------------------------------------------
import requests
import boto3
from botocore.config import Config
from rapidfuzz import fuzz
from opencc import OpenCC

CC = OpenCC("t2s")

CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
D1_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
BUCKET_OCR = os.environ["S_BUCKET"]   # holds _ocr/{book_id}/page_NNNN.txt (same bucket ocr_ndl.py/ocr.py write to)
BUCKET_TEXT = "guyaofang-assets"      # holds books_text_volumes.r2_key full-text files (validated experiment's fixed bucket; not a secret, just a bucket name)

LIMIT = os.environ.get("LIMIT", "").strip()
SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))
# Safety net only (not a feature cap): real max observed page_count is 2153
# (本草纲目). Guards a single corrupt-data book from blowing the 120min job
# timeout. The small-sample experiment capped at 60 for a quick look; this
# production run tests every real page, matching the cost estimate in the
# validation report (~11.3万页 total across the whole 889-book population).
PAGE_CAP = 3000

S = requests.Session()

def q(sql, params=None):
    body = {"sql": sql}
    if params is not None: body["params"] = params
    for att in range(3):
        try:
            r = S.post(D1_URL, json=body, headers={"Authorization": f"Bearer {D1_TOK}"}, timeout=60)
            j = r.json()
            if j.get("success"):
                return j["result"][0]["results"]
            raise RuntimeError(json.dumps(j.get("errors"))[:200])
        except Exception as e:
            if att == 2:
                raise
            time.sleep(2)

# No proxy config here on purpose: GitHub Actions runners reach Cloudflare
# directly (the local proxy workaround in the small-sample experiment was
# only needed on the developer's censored-network machine).
s3 = boto3.client(
    "s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK,
    region_name="auto",
    config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 2}, max_pool_connections=16),
)

def get_txt(bucket, key):
    """get_object by constructed key; NoSuchKey -> None; other errors retry once.
    Never list_objects/get_paginator -- enforced repo-wide by guard_no_list.yml."""
    for att in range(2):
        try:
            return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", errors="replace")
        except s3.exceptions.NoSuchKey:
            return None
        except Exception:
            if att == 1:
                return None
            time.sleep(1)

CJK = r"㐀-鿿"
def cjk_only(s):
    return re.sub(f"[^{CJK}]", "", s)

def norm_fulltext(raw):
    s = re.sub(r"<[^>\n]{1,30}>", "", raw)   # strip <篇名>/<目录> style markers
    s = CC.convert(s)
    return cjk_only(s)

def extract_anchors(ocr_raw):
    s = CC.convert(ocr_raw)
    runs = re.findall(f"[{CJK}]{{6,}}", s)
    runs = sorted(set(runs), key=lambda x: (-len(x), x))   # longest first, then lexical -- reproducible
    anchors, seen = [], set()
    for r in runs:
        a = r[:12]
        if a not in seen:
            anchors.append(a); seen.add(a)
        if len(anchors) >= 5:
            break
    return anchors, len(cjk_only(s))

def locate(anchor, ft, prev_pos):
    """returns (pos, method, score) or None."""
    c = ft.count(anchor)
    if c == 1:
        return ft.find(anchor), "exact", 100.0
    if 2 <= c <= 5:
        if prev_pos is None:
            return None   # no prior position, ambiguous anchor -> give up
        best, bd, start = None, None, 0
        while True:
            i = ft.find(anchor, start)
            if i < 0:
                break
            d = abs(i - prev_pos)
            if bd is None or d < bd:
                best, bd = i, d
            start = i + 1
        return (best, "exact_multi", 100.0) if best is not None else None
    if c > 5:
        return None   # high-frequency phrase, unreliable
    # c == 0 -> fuzzy fallback
    if prev_pos is not None:
        lo, hi = max(0, prev_pos - 3000), min(len(ft), prev_pos + 60000)
    else:
        lo, hi = 0, len(ft)
    r = fuzz.partial_ratio_alignment(anchor, ft[lo:hi], score_cutoff=80)
    if r is None:
        return None
    return lo + r.dest_start, "fuzzy", round(r.score, 1)

def probe_has_ocr(bid):
    """peek first 3 pages: any page with CJK>=20 counts as 'has usable OCR layer'
    (identical threshold to the small-sample experiment)."""
    for p in (1, 2, 3):
        t = get_txt(BUCKET_OCR, f"_ocr/{bid}/page_{p:04d}.txt")
        if t and len(cjk_only(CC.convert(t))) >= 20:
            return True
    return False

def align_book(bid, page_count, ft):
    """Same per-page algorithm as the validated experiment; only the loop
    bound changed (full page_count instead of a 60-page sample) and the
    per-page diagnostic fields (fail detail / ocr_head samples) were dropped
    since production output only needs the book-level aggregate."""
    n_pages = min(PAGE_CAP, page_count)
    keys = [f"_ocr/{bid}/page_{p:04d}.txt" for p in range(1, n_pages + 1)]
    with ThreadPoolExecutor(8) as ex:
        ocr_pages = list(ex.map(lambda k: get_txt(BUCKET_OCR, k), keys))

    statuses, prev_pos = [], None
    aligned_offsets = []
    with_text_n = 0
    for p, raw in enumerate(ocr_pages, 1):
        if raw is None:
            statuses.append("no_file"); continue
        anchors, cjk_len = extract_anchors(raw)
        if cjk_len < 20:
            statuses.append("no_text"); continue
        with_text_n += 1
        hits = []
        for a in anchors:
            r = locate(a, ft, prev_pos)
            if r:
                hits.append({"pos": r[0], "m": r[1]})
        ok, offset = False, None
        if len(hits) >= 2:
            ps = sorted(h["pos"] for h in hits)
            med = statistics.median(ps)
            core = [x for x in ps if abs(x - med) <= 15000]
            if len(core) >= 2 and (max(core) - min(core) <= 30000):
                offset = int(statistics.median(core)); ok = True
        elif len(hits) == 1 and len(anchors) == 1 and hits[0]["m"].startswith("exact"):
            offset = hits[0]["pos"]; ok = True
        if ok:
            statuses.append("aligned"); prev_pos = offset; aligned_offsets.append(offset)
        else:
            statuses.append("fail")

    no_file = statuses.count("no_file")
    no_text = statuses.count("no_text")
    aligned_n = statuses.count("aligned")
    mono_ok = sum(1 for i in range(len(aligned_offsets) - 1) if aligned_offsets[i + 1] >= aligned_offsets[i] - 1000)
    mono_pct = round(100.0 * mono_ok / (len(aligned_offsets) - 1), 1) if len(aligned_offsets) >= 2 else None
    anchor_pct = round(100.0 * aligned_n / with_text_n, 1) if with_text_n else 0.0
    return {
        "page_count": page_count, "pages_tested": n_pages,
        "no_file": no_file, "no_text": no_text,
        "with_text": with_text_n, "aligned": aligned_n,
        "anchor_pct": anchor_pct, "mono_pct": mono_pct,
    }

def classify_tier(anchor_pct, mono_pct, aligned_n):
    """Three-tier confidence gate, verbatim from the 2026-07-25 validation
    report (thresholds not to be re-tuned here):
      A 逐页同步: anchor_pct>=60 and mono_pct>=90
      B 骨架同步: anchor_pct>=20 and mono_pct>=85 and aligned_n>=8
      C 不启用 : otherwise
    """
    mono = mono_pct if mono_pct is not None else 0.0
    if anchor_pct >= 60 and mono >= 90:
        return "A"
    if anchor_pct >= 20 and mono >= 85 and aligned_n >= 8:
        return "B"
    return "C"

def main():
    books = q(
        """SELECT m.image_book_code AS book_id, m.text_id AS text_id,
                  a.page_count AS page_count, a.book_title AS book_title
           FROM books_text_image_map m
           JOIN books_assets_v2 a ON a.book_id = m.image_book_code
           WHERE a.page_count > 0
           ORDER BY m.image_book_code"""
    )
    print(f"[{time.time()-T0:.0f}s] 分母(books_text_image_map 有映射且 books_assets_v2 存在的册): {len(books)}", flush=True)

    if LIMIT:
        books = books[: int(LIMIT)]
        print(f"[{time.time()-T0:.0f}s] LIMIT={LIMIT} -> 截取前 {len(books)} 册", flush=True)

    mine = [b for i, b in enumerate(books) if i % TOTAL == SHARD]
    print(f"[{time.time()-T0:.0f}s] shard {SHARD}/{TOTAL} 分到 {len(mine)}/{len(books)} 册", flush=True)

    ft_cache = {}
    def get_ft(tid):
        """Full text is per text_id (shared across every 册/volume that maps to
        it), not per book -- cache it so sibling volumes in the same shard
        don't re-fetch/re-normalize a huge text (e.g. 本草纲目 134万字) repeatedly."""
        if tid in ft_cache:
            return ft_cache[tid]
        vol_rows = q("SELECT vol_no, r2_key FROM books_text_volumes WHERE text_id=? ORDER BY vol_no", [tid])
        if not vol_rows:
            ft_cache[tid] = None
            return None
        with ThreadPoolExecutor(6) as ex:
            txts = list(ex.map(lambda v: get_txt(BUCKET_TEXT, v["r2_key"]), vol_rows))
        ft_raw = "".join(t or "" for t in txts)
        ft_cache[tid] = norm_fulltext(ft_raw)
        return ft_cache[tid]

    results = []
    for i, b in enumerate(mine):
        bid, tid, pc = b["book_id"], b["text_id"], b["page_count"]
        title = (b.get("book_title") or "")[:60]
        t1 = time.time()
        rec = {"book_id": bid, "text_id": tid, "title": title, "page_count": pc}
        try:
            if not probe_has_ocr(bid):
                rec.update({"tier": "no_ocr", "pages_tested": 0, "anchor_pct": None, "mono_pct": None})
                results.append(rec)
                print(f"[{i+1}/{len(mine)}] {bid} no_ocr(前3页无OCR文本,跳过) {round(time.time()-t1,1)}s", flush=True)
                continue
            ft = get_ft(tid)
            if not ft:
                rec.update({"tier": "no_volumes", "pages_tested": 0, "anchor_pct": None, "mono_pct": None})
                results.append(rec)
                print(f"[{i+1}/{len(mine)}] {bid} no_volumes(该text_id无全文卷记录) {round(time.time()-t1,1)}s", flush=True)
                continue
            r = align_book(bid, pc, ft)
            rec.update(r)
            rec["tier"] = classify_tier(r["anchor_pct"], r["mono_pct"], r["aligned"])
            rec["elapsed_s"] = round(time.time() - t1, 1)
            results.append(rec)
            print(
                f"[{i+1}/{len(mine)}] {bid} 测{r['pages_tested']}页 有文本{r['with_text']} "
                f"对齐{r['aligned']}({r['anchor_pct']}%) 单调{r['mono_pct']}% tier={rec['tier']} {rec['elapsed_s']}s",
                flush=True,
            )
        except Exception as e:
            rec.update({"tier": "error", "error": str(e)[:300], "elapsed_s": round(time.time() - t1, 1)})
            results.append(rec)
            print(f"[{i+1}/{len(mine)}] {bid} ERROR {str(e)[:200]}", flush=True)

    tiers = Counter(r["tier"] for r in results)
    known = ("A", "B", "C", "no_ocr", "no_volumes", "error")
    print(
        f"\n[{time.time()-T0:.0f}s] shard {SHARD}/{TOTAL} 完成 {len(results)} 册。tier分布: "
        + " ".join(f"{k}={tiers.get(k,0)}" for k in known),
        flush=True,
    )

    # Filename MUST include the shard id: the workflow downloads every shard's
    # artifact into one flat directory (merge-multiple:true) for the
    # summarize step, and if every shard wrote the same filename they would
    # overwrite each other there (caught by the limit=30 dry run: only the
    # last-downloaded shard's 3-4 books survived instead of all ~30).
    OUT = f"align_full_result_shard{SHARD}.json"
    json.dump(results, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"结果 -> {OUT}", flush=True)

if __name__ == "__main__":
    main()
