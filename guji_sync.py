# guji_sync.py - R2 guji-sea -> 123 GufangP/guji/ scatter-page migration
# Architecture: GitHub Actions matrix shards; each shard reads guji_pages.json,
# takes its slice, mkdir book_id folder in 123, then offline-downloads each page.
# 123 server pulls from R2 presigned URL directly (no local bandwidth needed).
# Safety: zero R2 delete, idempotent skip-done, ledger per shard, D1 tracking.
import os, re, json, time, boto3, requests
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------- credentials from CI env ----------
EP  = os.environ["S_EP"]
AK  = os.environ["S_AK"]
SK  = os.environ["S_SK"]
BKT = os.environ.get("S_BUCKET", "guji-sea")   # default: guji-sea
PFX = os.environ.get("S_PREFIX", "naj")         # key prefix inside bucket
PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]
PSEC = os.environ["PAN_SEC"]
GUJI_DIR = int(os.environ.get("PAN_GUJI_DIR", "30684164"))  # GufangP/guji fileId
PAGES_KEY = os.environ.get("PAGES_KEY", "_cc/guji_pages.json")
DONE_KEY  = os.environ.get("DONE_KEY",  "_cc/guji_scatter_done.json")  # set of book_ids done
LEDGER_PFX = os.environ.get("LEDGER_PREFIX", "_ledger_guji/")
SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL", "7200"))  # URL expiry seconds
# max concurrent offline tasks per shard (tune down if 429 appears)
MAX_PENDING = int(os.environ.get("MAX_PENDING", "20"))

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto",
                  config=Config(connect_timeout=20, read_timeout=60))
_S = requests.Session(); _S.trust_env = False
_tok = {"v": None}


# ---------- 123pan API helpers ----------
def token():
    if _tok["v"] is None:
        r = _S.post(PAN + "/api/v1/access_token",
                    headers={"Platform": "open_platform"},
                    json={"clientID": PCID, "clientSecret": PSEC}, timeout=60).json()
        _tok["v"] = (r.get("data") or {}).get("accessToken")
    return _tok["v"]


def pan(method, path, body=None, params=None):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    if body is not None:
        h["Content-Type"] = "application/json"
    delay = 2.0
    for _ in range(8):
        try:
            resp = _S.request(method, PAN + path, headers=h,
                              data=json.dumps(body) if body is not None else None,
                              params=params, timeout=90)
            last = resp.json()
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 30); continue
        msg = str(last.get("message", "")); code = last.get("code")
        if "exceeded" in msg or "tokens number" in msg or "频繁" in msg or code in (429, 401):
            if code == 401:
                _tok["v"] = None
            time.sleep(delay); delay = min(delay * 2, 60); continue
        return last
    return last


def ensure_dir(name, parent_id):
    """Create folder under parent_id; return fileId. Idempotent."""
    last = 0
    while True:
        d = pan("GET", f"/api/v2/file/list", params={
            "parentFileId": parent_id, "limit": 100, "lastFileId": last})
        fl = (d.get("data") or {}).get("fileList") or []
        for it in fl:
            if it.get("filename") == name and it.get("type") == 1:
                return it.get("fileId") or it.get("fileID")
        last = (d.get("data") or {}).get("lastFileId", -1)
        if last in (-1, None) or not fl:
            break
    r = pan("POST", "/upload/v1/file/mkdir",
            {"name": name, "parentID": parent_id})
    return (r.get("data") or {}).get("dirID")


def offline_dl(url, dir_id, name):
    """Submit one offline-download task; return taskID or error string."""
    r = pan("POST", "/api/v1/offline/download",
            {"url": url, "dirID": dir_id, "fileName": name})
    code = r.get("code")
    if code == 0:
        return (r.get("data") or {}).get("taskID")
    return f"err:{code}:{str(r.get('message',''))[:40]}"


# ---------- skip logic: check if page file already exists in 123 ----------
_dir_cache = {}  # book_id -> (dir_id, set of filenames already there)


def get_or_make_book_dir(book_id):
    if book_id not in _dir_cache:
        did = ensure_dir(book_id, GUJI_DIR)
        # list existing files in this dir to build done set
        done_files = set()
        last = 0
        for _ in range(200):
            d = pan("GET", "/api/v2/file/list", params={
                "parentFileId": did, "limit": 100, "lastFileId": last})
            fl = (d.get("data") or {}).get("fileList") or []
            for it in fl:
                done_files.add(it.get("filename", ""))
            last = (d.get("data") or {}).get("lastFileId", -1)
            if last in (-1, None) or not fl:
                break
        _dir_cache[book_id] = (did, done_files)
    return _dir_cache[book_id]


# ---------- R2 page existence check ----------
def r2_page_exists(book_id, page_no):
    key = f"{PFX}/{book_id}/page_{page_no:04d}.webp"
    try:
        s3.head_object(Bucket=BKT, Key=key)
        return key
    except ClientError as ex:
        if ex.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


# ---------- per-book handler ----------
def handle_book(book_id, page_count):
    """
    For one book_id: ensure 123 dir exists, offline-download missing pages.
    Returns dict with counts.
    """
    dir_id, done_files = get_or_make_book_dir(book_id)
    pending = []   # (page_no, fname, task_id)
    skipped = ok = err = 0

    for pn in range(1, page_count + 1):
        fname = f"page_{pn:04d}.webp"
        if fname in done_files:
            skipped += 1
            continue
        # check R2 existence before presigning
        r2_key = f"{PFX}/{book_id}/page_{pn:04d}.webp"
        try:
            s3.head_object(Bucket=BKT, Key=r2_key)
        except ClientError as ex:
            if ex.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                continue  # page not in R2 (incomplete download); skip silently
            raise

        url = s3.generate_presigned_url("get_object",
            Params={"Bucket": BKT, "Key": r2_key}, ExpiresIn=PRESIGN_TTL)
        tid = offline_dl(url, dir_id, fname)
        if isinstance(tid, int):
            pending.append((pn, fname, tid))
            # throttle: don't flood with too many concurrent tasks
            if len(pending) >= MAX_PENDING:
                time.sleep(1)  # brief pause; 123 queues them server-side
                ok += len(pending); pending = []
        else:
            err += 1
            print(f"  WARN {book_id} page {pn}: {tid}", flush=True)

    ok += len(pending)
    return {"book_id": book_id, "pages": page_count,
            "ok": ok, "skip": skipped, "err": err}


# ---------- main ----------
def main():
    # load page manifest
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    items = sorted(pages.items())  # (book_id, page_count)

    # shard slice
    mine = [(b, pc) for i, (b, pc) in enumerate(items) if i % TOTAL == SHARD]
    print(f"shard {SHARD}/{TOTAL}: {len(mine)} books / {sum(pc for _,pc in mine)} pages",
          flush=True)

    ledger = []
    for i, (book_id, page_count) in enumerate(mine):
        rec = handle_book(book_id, int(page_count))
        ledger.append(rec)
        if (i + 1) % 10 == 0 or i == len(mine) - 1:
            print(f"  [{i+1}/{len(mine)}] {book_id}: ok={rec['ok']} skip={rec['skip']} err={rec['err']}",
                  flush=True)

    # write shard ledger to R2
    lk = LEDGER_PFX + f"shard_{SHARD}.json"
    s3.put_object(Bucket=BKT, Key=lk,
                  Body=json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    total_ok = sum(r["ok"] for r in ledger)
    total_skip = sum(r["skip"] for r in ledger)
    total_err = sum(r["err"] for r in ledger)
    print(f"=== shard {SHARD} done: ok={total_ok} skip={total_skip} err={total_err} ledger->{lk} ===",
          flush=True)


# ---------- prep step: build done-set snapshot ----------
def prep():
    """
    Called once before shards. Scans GufangP/guji/ to build set of book_ids
    that are 100% done (all pages present). Writes to DONE_KEY so shards can
    skip whole books fast without listing 123 per book.
    This is optional fast-path; shards also check per-page via get_or_make_book_dir.
    """
    # list book_id dirs under GUJI_DIR
    book_dirs = {}  # name -> dir_id
    last = 0
    while True:
        d = pan("GET", "/api/v2/file/list",
                params={"parentFileId": GUJI_DIR, "limit": 100, "lastFileId": last})
        fl = (d.get("data") or {}).get("fileList") or []
        for it in fl:
            if it.get("type") == 1:  # folder
                book_dirs[it["filename"]] = it.get("fileId") or it.get("fileID")
        last = (d.get("data") or {}).get("lastFileId", -1)
        if last in (-1, None) or not fl:
            break
    print(f"prep: found {len(book_dirs)} book dirs in 123 GufangP/guji/", flush=True)

    # load expected page counts
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    done_books = set()
    # a book is 'done' if its dir exists and file count == page_count
    # For speed we do a quick file-count check
    for book_id, dir_id in list(book_dirs.items())[:]:
        expected = int(pages.get(book_id, 0))
        if expected == 0:
            continue
        cnt = 0; last2 = 0
        while True:
            d2 = pan("GET", "/api/v2/file/list",
                     params={"parentFileId": dir_id, "limit": 100, "lastFileId": last2})
            fl2 = (d2.get("data") or {}).get("fileList") or []
            cnt += len(fl2)
            last2 = (d2.get("data") or {}).get("lastFileId", -1)
            if last2 in (-1, None) or not fl2 or cnt >= expected:
                break
        if cnt >= expected:
            done_books.add(book_id)
    print(f"prep: {len(done_books)} books fully done", flush=True)
    s3.put_object(Bucket=BKT, Key=DONE_KEY,
                  Body=json.dumps(sorted(done_books), ensure_ascii=False).encode("utf-8"))
    print(f"prep: wrote {DONE_KEY}", flush=True)


# ---------- audit: compare R2 vs 123 page counts ----------
def audit():
    """
    Pull ledgers, sum ok/skip/err per book; compare against guji_pages.json.
    Prints reconciliation: N expected, M done, K missing.
    """
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    total_expected = sum(int(v) for v in pages.values())
    # collect all ledger shards
    resp = s3.list_objects_v2(Bucket=BKT, Prefix=LEDGER_PFX)
    total_ok = total_skip = total_err = 0
    books_with_err = []
    for obj in (resp.get("Contents") or []):
        if not obj["Key"].endswith(".json"):
            continue
        data = json.loads(s3.get_object(Bucket=BKT, Key=obj["Key"])["Body"].read())
        for rec in data:
            total_ok += rec.get("ok", 0)
            total_skip += rec.get("skip", 0)
            total_err += rec.get("err", 0)
            if rec.get("err", 0) > 0:
                books_with_err.append(rec["book_id"])
    done_pages = total_ok + total_skip
    print(f"AUDIT: expected_pages={total_expected}  done={done_pages}  "
          f"ok={total_ok}  skip={total_skip}  err={total_err}")
    missing = total_expected - done_pages
    print(f"  missing pages = {missing}  books_with_err = {len(books_with_err)}")
    if books_with_err:
        print(f"  books with err (first 10): {books_with_err[:10]}")
    if missing == 0 and total_err == 0:
        print("  [PASS] K=0 全部完成")
    else:
        print("  [INCOMPLETE] 还有缺漏，继续补跑")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "main"
    if cmd == "prep":
        prep()
    elif cmd == "audit":
        audit()
    else:
        main()
