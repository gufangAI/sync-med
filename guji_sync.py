# guji_sync.py - R2 guji-sea -> 123 GufangP/guji/ direct-upload migration
# Architecture: GitHub Actions matrix shards; each shard reads guji_pages.json,
# takes its slice, mkdir book_id folder in 123, then DIRECTLY uploads each page.
# Method: R2 get_object -> 123 upload API (create/get_url/PUT/complete).
# ⚠️ S.put timeout MUST be 1200s -- proven by sync.py (115 concurrent, 6501 books ok).
# Safety: zero R2 delete, reuse-skip idempotent, ledger per shard.
import os, json, time, hashlib, threading, boto3, requests
from concurrent.futures import ThreadPoolExecutor
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------- credentials from CI env ----------
EP   = os.environ["S_EP"]
AK   = os.environ["S_AK"]
SK   = os.environ["S_SK"]
BKT  = os.environ.get("S_BUCKET", "guji-sea")
PFX  = os.environ.get("S_PREFIX", "naj")
PAN  = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]
PSEC = os.environ["PAN_SEC"]
GUJI_DIR   = int(os.environ.get("PAN_GUJI_DIR", "30684164"))
PAGES_KEY  = os.environ.get("PAGES_KEY",  "_cc/guji_pages.json")
LEDGER_PFX = os.environ.get("LEDGER_PREFIX", "_ledger_guji/")
SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto",
                  config=Config(connect_timeout=15, read_timeout=120,
                                retries={"max_attempts": 3}))
_S = requests.Session(); _S.trust_env = False

_adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64)
_S.mount("https://", _adapter); _S.mount("http://", _adapter)
_tok = {"v": None}
_tok_lock = threading.Lock()
PAGE_CONC = int(os.environ.get("PAGE_CONCURRENCY", "8"))  


# ---------- 123pan API helpers ----------
def token():
    with _tok_lock:  
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
                              params=params, timeout=120)
            last = resp.json()
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 30); continue
        msg = str(last.get("message", "")); code = last.get("code")
        if "exceeded" in msg or "tokens number" in msg or '\u9891\u7e41' in msg or code in (429, 401):
            if code == 401:
                _tok["v"] = None
            time.sleep(delay); delay = min(delay * 2, 60); continue
        return last
    return last


# ---------- book dir cache (preloaded once per shard) ----------
_book_dirs = {}  # {book_id: dir_id}


def preload_book_dirs():
    """List all existing book folders under GufangP/guji/ into cache."""
    last = 0
    while True:
        d = pan("GET", "/api/v2/file/list",
                params={"parentFileId": GUJI_DIR, "limit": 100, "lastFileId": last})
        fl = (d.get("data") or {}).get("fileList") or []
        for it in fl:
            if it.get("type") == 1:
                _book_dirs[it["filename"]] = it.get("fileId") or it.get("fileID")
        last = (d.get("data") or {}).get("lastFileId", -1)
        if last in (-1, None) or not fl:
            break
    print(f"preload: {len(_book_dirs)} existing dirs cached", flush=True)


def get_book_dir(book_id):
    """Return dir_id for book_id under GUJI_DIR; mkdir if needed."""
    if book_id in _book_dirs:
        return _book_dirs[book_id]
    r = pan("POST", "/upload/v1/file/mkdir", {"name": book_id, "parentID": GUJI_DIR})
    did = (r.get("data") or {}).get("dirID")
    if not did:
        # mkdir may fail if dir already exists (race); find it
        last = 0
        while True:
            d = pan("GET", "/api/v2/file/list",
                    params={"parentFileId": GUJI_DIR, "limit": 100, "lastFileId": last})
            fl = (d.get("data") or {}).get("fileList") or []
            for it in fl:
                if it.get("filename") == book_id and it.get("type") == 1:
                    did = it.get("fileId") or it.get("fileID")
                    break
            if did:
                break
            last = (d.get("data") or {}).get("lastFileId", -1)
            if last in (-1, None) or not fl:
                break
    _book_dirs[book_id] = did
    return did


# ---------- direct upload (proven method from sync.py, 06-20) ----------
def put_bytes(data, dir_id, name):
    """Upload bytes to 123pan dir. Returns 'ok' / 'reuse' / 'err:...'
    ⚠️ S.put timeout=1200s is MANDATORY -- short timeout kills slow-but-valid uploads."""
    etag = hashlib.md5(data).hexdigest()
    size = len(data)
    cr = pan("POST", "/upload/v1/file/create",
             {"parentFileID": dir_id, "filename": name, "etag": etag, "size": size})
    d = cr.get("data") or {}
    if d.get("reuse"):
        return "reuse"
    pid = d.get("preuploadID")
    if not pid:
        msg = str(cr.get("message") or "")
        if '\u91cd\u590d' in msg or '\u5df2\u5b58\u5728' in msg or "exist" in msg.lower():
            return "reuse"
        return "err:create:" + msg[:50]
    
    ok_put = False
    for _att in range(5):
        url_r = pan("POST", "/upload/v1/file/get_upload_url", {"preuploadID": pid, "sliceNo": 1})
        url = (url_r.get("data") or {}).get("presignedURL")
        if not url:
            time.sleep(2); continue
        try:
            pr = _S.put(url, data=data, timeout=1200)  
            if pr.status_code in (200, 204):
                ok_put = True; break
            time.sleep(1.5 * (_att + 1))
        except Exception:
            time.sleep(1.5 * (_att + 1))
    if not ok_put:
        return "err:put"
    cd = (pan("POST", "/upload/v1/file/upload_complete", {"preuploadID": pid}).get("data") or {})
    if cd.get("async"):
        for _ in range(180):
            time.sleep(1)
            if (pan("POST", "/upload/v1/file/upload_async_result",
                    {"preuploadID": pid}).get("data") or {}).get("completed"):
                return "ok"
        return "err:async_timeout"
    return "ok"



def list_existing(dir_id):
    '\u5217\u51fa\u8be5\u4e66\u76ee\u5f55\u4e0b\u5df2\u4f20\u7684\u9875\u6587\u4ef6\u540d\u96c6\u5408(\u5e42\u7b49\u7528,\u8df3\u8fc7\u5df2\u4f20)\u3002'
    names = set(); last = 0
    while True:
        d = pan("GET", "/api/v2/file/list",
                params={"parentFileId": dir_id, "limit": 100, "lastFileId": last})
        fl = (d.get("data") or {}).get("fileList") or []
        for it in fl:
            if it.get("type") == 0:
                names.add(it.get("filename"))
        last = (d.get("data") or {}).get("lastFileId", -1)
        if last in (-1, None) or not fl:
            break
    return names


def handle_book(book_id, page_count):
    '\u5e42\u7b49\u8865\u7f3a:\u53ea\u4f20\u6ca1\u4f20\u8fc7\u7684\u9875;put_bytes \u5185\u542b\u91cd\u8bd5(\u6cbb429);\u6838\u9875\u6570,\u591f\u4e86\u624d\u7b97\u5b8c\u6574\u3002'
    dir_id = get_book_dir(book_id)
    if not dir_id:
        return {"book_id": book_id, "pages": page_count, "ok": 0, "reuse": 0,
                "err": 1, "r2_miss": 0, "note": "no_dir", "complete": False}

    existing = list_existing(dir_id)            
    todo = [pn for pn in range(1, page_count + 1)
            if f"page_{pn:04d}.webp" not in existing]

    def put_page(pn):
        r2_key = f"{PFX}/{book_id}/page_{pn:04d}.webp"
        try:
            data = s3.get_object(Bucket=BKT, Key=r2_key)["Body"].read()
        except ClientError as ex:
            code = ex.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return "miss"
            return "err:r2"
        except Exception:
            return "err:r2x"
        return put_bytes(data, dir_id, f"page_{pn:04d}.webp")

    ok = len(existing); reuse = err = r2_miss = 0
    with ThreadPoolExecutor(max_workers=PAGE_CONC) as pool:
        for result in pool.map(put_page, todo):
            if result == "ok":
                ok += 1
            elif result == "reuse":
                ok += 1; reuse += 1
            elif result == "miss":
                r2_miss += 1
            else:
                err += 1

    complete = ok >= (page_count - r2_miss)     
    return {"book_id": book_id, "pages": page_count, "ok": ok, "reuse": reuse,
            "err": err, "r2_miss": r2_miss, "complete": complete}


# ---------- main ----------
def main():
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    items = sorted(pages.items())
    
    HALVES = int(os.environ.get("HALVES", "1"))
    HALF = int(os.environ.get("HALF", "0"))
    if HALVES > 1:
        items = [it for j, it in enumerate(items) if j % HALVES == HALF]
    mine = [(b, pc) for i, (b, pc) in enumerate(items) if i % TOTAL == SHARD]
    print(f"shard {SHARD}/{TOTAL}: {len(mine)} books / {sum(pc for _,pc in mine)} pages "
          f"| PAGE_CONC={PAGE_CONC}", flush=True)

    preload_book_dirs()

    ledger = []
    for i, (book_id, page_count) in enumerate(mine):
        rec = handle_book(book_id, int(page_count))
        ledger.append(rec)
        if (i + 1) % 5 == 0 or i == len(mine) - 1:
            print(f"  [{i+1}/{len(mine)}] {book_id}: "
                  f"ok={rec['ok']} reuse={rec['reuse']} err={rec['err']} r2miss={rec['r2_miss']}",
                  flush=True)

    lk = LEDGER_PFX + f"shard_{SHARD}.json"
    s3.put_object(Bucket=BKT, Key=lk,
                  Body=json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    total_ok    = sum(r["ok"]     for r in ledger)
    total_reuse = sum(r["reuse"]  for r in ledger)
    total_err   = sum(r["err"]    for r in ledger)
    total_miss  = sum(r["r2_miss"] for r in ledger)
    print(f"=== shard {SHARD} done: ok={total_ok} reuse={total_reuse} "
          f"err={total_err} r2_miss={total_miss} ledger->{lk} ===", flush=True)


# ---------- audit ----------
def audit():
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    total_expected = sum(int(v) for v in pages.values())
    total_ok = total_reuse = total_err = 0
    # zero-LIST (guard): construct shard ledger keys from TOTAL, no list_objects
    for shard in range(TOTAL):
        key = LEDGER_PFX + f"shard_{shard}.json"
        try:
            data = json.loads(s3.get_object(Bucket=BKT, Key=key)["Body"].read())
        except Exception:
            continue  # shard ledger not written yet
        if not isinstance(data, list):
            continue  # skip malformed ledger
        for rec in data:
            if not isinstance(rec, dict):
                continue  # robust: skip non-dict record
            total_ok    += rec.get("ok", 0)
            total_reuse += rec.get("reuse", 0)
            total_err   += rec.get("err", 0)
    done = total_ok + total_reuse
    print(f"AUDIT: expected={total_expected} done={done} ok={total_ok} "
          f"reuse={total_reuse} err={total_err} missing={total_expected-done}")
    if total_expected - done == 0 and total_err == 0:
        print('  [PASS] K=0 \u5168\u90e8\u5b8c\u6210')
    else:
        print('  [INCOMPLETE] \u7ee7\u7eed\u8865\u8dd1')


def prep():
    """Legacy prep step: verify R2 manifest accessible. Shards preload dirs inline."""
    pages = json.loads(s3.get_object(Bucket=BKT, Key=PAGES_KEY)["Body"].read())
    total = sum(int(v) for v in pages.values())
    print(f"prep ok: manifest has {len(pages)} books / {total} total pages", flush=True)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "main"
    if cmd == "prep":
        prep()
    elif cmd == "audit":
        audit()
    else:
        main()
