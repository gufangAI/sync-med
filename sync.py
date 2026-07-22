# Cloud mirror worker. Runs on CI runner only (no local hop).
# For each source group: stream a zip (on disk, not memory) + build a pdf, push both to pan storage in
# separate folders. All credentials come from env (CI secrets); nothing hardcoded; no CJK in source.
import os, re, time, json, hashlib, tempfile
import boto3, requests
from botocore.exceptions import ClientError
from botocore.config import Config
from PIL import Image

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
SRC = os.environ["S_BUCKET"]; PFX = os.environ.get("S_PREFIX", "").strip("/")
PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
DIR_A = os.environ["PAN_DIR_A"]      # folder id for image archives
DIR_B = os.environ["PAN_DIR_B"]      # folder id for documents (separate)
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
TMP = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())
ZIP_ONLY = os.environ.get("ZIP_ONLY") == "1"   

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto",
                  config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}))

# 2026-07-22: book/ 影像已于 2026-07-17 迁 123、R2 已删空。取图改从 123(照 ocr_ndl.py 已验证的
# fetch_page_from_123,与生产 functions/api/_lib/pan123.js 同逻辑)。用 PAN_CLIENT_ID/SECRET 凭据
# (open_platform,与推 zip/pdf 的 PAN_CID/PSEC 是不同凭据对,workflow secret 已存在于仓库)。
PAN_CLIENT_ID = os.environ.get("PAN_CLIENT_ID"); PAN_CLIENT_SECRET = os.environ.get("PAN_CLIENT_SECRET")
GID_PDID = {}          # gid -> pan_dir_id,由 list_groups 填充
_PAN_TOK = {"v": None}

def pan_token():
    if _PAN_TOK["v"]:
        return _PAN_TOK["v"]
    r = requests.post(PAN + "/api/v1/access_token",
                      headers={"Platform": "open_platform", "Content-Type": "application/json"},
                      json={"clientID": PAN_CLIENT_ID, "clientSecret": PAN_CLIENT_SECRET}, timeout=30)
    tok = (r.json().get("data") or {}).get("accessToken")
    if not tok:
        raise SystemExit("123 token 获取失败: " + r.text[:200])
    _PAN_TOK["v"] = tok
    return tok

def fetch_page_from_123(pan_dir_id, page_str):
    # 与 ocr_ndl.py / 生产 pan123.js 的 fetchPageFrom123 同一逻辑
    if not pan_dir_id:
        return None
    h = {"Platform": "open_platform", "Authorization": "Bearer " + pan_token()}
    filename = f"page_{page_str}.webp"
    last_id, file_id = 0, None
    for _ in range(20):
        r = requests.get(f"{PAN}/api/v2/file/list",
                         params={"parentFileId": pan_dir_id, "limit": 100, "lastFileId": last_id},
                         headers=h, timeout=30)
        d = r.json().get("data") or {}
        fl = d.get("fileList") or []
        hit = next((f for f in fl if f.get("filename") == filename), None)
        if hit:
            file_id = hit.get("fileId") or hit.get("fileID")
            break
        last_id = d.get("lastFileId")
        if last_id in (None, -1) or not fl:
            break
    if not file_id:
        return None
    r = requests.get(f"{PAN}/api/v1/file/download_info", params={"fileId": file_id}, headers=h, timeout=30)
    url = (r.json().get("data") or {}).get("downloadUrl")
    if not url:
        return None
    r = requests.get(url, timeout=60)
    return r.content if r.status_code == 200 else None


def _key_of(book_id):
    # normalize book_id -> lookup key: strip non-digit prefix, de-zero-pad trailing volume no.
    # shared by handle() and _rebuild_names_from_d1() so the two can never drift apart.
    p = re.sub(r"^\D+", "", book_id).split("-")
    return "-".join(p[:-1] + [str(int(p[-1]))]) if p and p[-1].isdigit() else "-".join(p)


def _rebuild_names_from_d1():
    # NAME_KEY 清单从 R2 读不到时,直接查 D1 现场生成并写回 R2 缓存(自愈),同 _rebuild_pages_from_d1 一个模式。
    # 治 2026-07-17 事故: 流氓 CF Worker 把 R2 桶里任意对象(含这份 manifest)每分钟扫 5 个转 123 后即删,
    # NAME_KEY 原本没有兜底 -> 至少 2026-07-11 起全部 shard 持续 skip-noname 零产出、空转烧 Actions 分钟。
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise RuntimeError("NAME_KEY 读不到 + 缺 CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN,无法从 D1 兜底")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    sql = ("SELECT book_id, book_title FROM books_assets_v2 "
           "WHERE frontend_visible=1 AND upload_status='done' AND book_title IS NOT NULL")
    r = requests.post(url, headers={"Authorization": "Bearer "+tok}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"): raise RuntimeError(f"D1 查询失败: {str(j.get('errors',''))[:200]}")
    rows = (j.get("result") or [{}])[0].get("results") or []
    names = {}
    for row in rows:
        bid = row.get("book_id"); title = row.get("book_title")
        if not bid or not title: continue
        names[_key_of(bid)] = title
    return names


# title map (req-number -> book name) loaded from private storage; source stays CJK-free
NAMES = {}
_nk = os.environ.get("NAME_KEY")
if _nk:
    try:
        NAMES = json.loads(s3.get_object(Bucket=SRC, Key=_nk)["Body"].read().decode("utf-8"))
    except Exception as e:
        print(f"WARNING: NAME_KEY {_nk} unreadable ({e}) -> rebuilding from D1...", flush=True)
        NAMES = _rebuild_names_from_d1()
        print(f"D1 rebuild ok: {len(NAMES)} names", flush=True)
        try:
            s3.put_object(Bucket=SRC, Key=_nk, Body=json.dumps(NAMES, ensure_ascii=False).encode("utf-8"))
            print(f"cached back to R2: {_nk}", flush=True)
        except Exception as e2:
            print(f"WARNING: cache-back failed ({e2}) -> next run will rebuild again, not fatal", flush=True)
# already-backed-up sets (built once by prep step listing the 123 backup folders): skip BEFORE any
# R2 GET or 123 create call -> no wasted ops, no "filename duplicate" errors.
DONE_ZIP = set(); DONE_PDF = set()
_dk = os.environ.get("PAN_DONE_KEY", "_cc/pan_done.json")
try:
    _dj = json.loads(s3.get_object(Bucket=SRC, Key=_dk)["Body"].read().decode("utf-8"))
    DONE_ZIP = set(_dj.get("zip", [])); DONE_PDF = set(_dj.get("pdf", []))
except Exception:
    pass
S = requests.Session()
_tok = {"v": None}
# circuit breaker: consecutive fully-exhausted pan() calls (persistent 123 rate-limit/token exhaustion).
# Without this, a shard that hits a *persistent* (not transient) 123 quota outage burns its whole
# runner window retrying book after book (~15-20min/book worst case) with only every-20-books logging,
# which looks like a silent hang for hours. Real incident: 2026-06-27 run, shard 76 logged "groups 83/21073"
# then nothing for 2h until GitHub killed it. Bail out fast instead of grinding to the timeout.
_rl = {"streak": 0}
RL_BREAKER = int(os.environ.get("RL_BREAKER", "5"))


def token():
    # 123 access_token is valid ~3 months -> fetch once and reuse for the whole run;
    # only re-fetched on a 401 (pan() clears it). Avoids per-25min re-fetch x many shards.
    if _tok["v"] is None:
        r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
                   json={"clientID": PCID, "clientSecret": PSEC}, timeout=60).json()
        _tok["v"] = (r.get("data") or {}).get("accessToken")
    return _tok["v"]


def pan(method, path, body=None):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    if body is not None:
        h["Content-Type"] = "application/json"
    delay = 2.0; last = {}
    for _ in range(7):
        try:
            last = S.request(method, PAN + path, headers=h,
                             data=json.dumps(body) if body is not None else None, timeout=120).json()
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 30); continue
        msg = str(last.get("message", "")); code = last.get("code")
        # 123 rate limit ("tokens number has exceeded the limit") / 429 / expired token -> backoff + retry
        if "exceeded" in msg or "tokens number" in msg or '\u9891\u7e41' in msg or code in (429, 401):
            if code == 401:
                _tok["v"] = None                  # auth failed -> force token re-fetch
            time.sleep(delay); delay = min(delay * 2, 60); continue
        _rl["streak"] = 0                 # got a real (non-rate-limit) response -> breaker resets
        return last
    _rl["streak"] += 1                    # exhausted all 7 retries -> counts toward the circuit breaker
    return last


def put_file(local_path, parent_id, name):
    
    
    size = os.path.getsize(local_path)
    h = hashlib.md5()
    with open(local_path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    cr = pan("POST", "/upload/v1/file/create",
             {"parentFileID": parent_id, "filename": name, "etag": h.hexdigest(), "size": size})
    d = cr.get("data") or {}
    if d.get("reuse"):
        return "reuse"
    pid = d.get("preuploadID")
    if not pid:
        msg = str(cr.get("message") or "")
        if '\u91cd\u590d' in msg or '\u5df2\u5b58\u5728' in msg or "exist" in msg.lower():
            return "dup"
        return "err:" + msg[:40]
    url = (pan("POST", "/upload/v1/file/get_upload_url",
              {"preuploadID": pid, "sliceNo": 1}).get("data") or {}).get("presignedURL")
    with open(local_path, "rb") as f:                 # stream upload, no full read into memory
        S.put(url, data=f, timeout=1200)              
    cd = pan("POST", "/upload/v1/file/upload_complete", {"preuploadID": pid}).get("data") or {}
    if cd.get("async"):
        for _ in range(180):
            time.sleep(1)
            if (pan("POST", "/upload/v1/file/upload_async_result",
                    {"preuploadID": pid}).get("data") or {}).get("completed"):
                return "ok"
        return "timeout"
    return "ok"


def _rebuild_pages_from_d1():
    'PAGES_KEY \u6e05\u5355\u4ece R2 \u8bfb\u4e0d\u5230\u65f6,\u76f4\u63a5\u67e5 D1 \u73b0\u573a\u751f\u6210\u5e76\u5199\u56de R2 \u7f13\u5b58(\u81ea\u6108)\u3002\n    \u6cbb 2026-07-05 \u4e8b\u6545: R2 \u7f13\u5b58\u6587\u4ef6\u7f3a\u5931\u5bfc\u81f4\u6240\u6709 shard \u542f\u52a8\u5373 NoSuchKey \u5168\u5d29\u3001\u96f6\u4ea7\u51fa\u3002\n    \u6ca1\u6709\u515c\u5e95\u65f6\u662f"\u5b9a\u65f6\u70b8\u5f39":\u54ea\u5929\u7f13\u5b58\u6587\u4ef6\u88ab\u8bef\u5220/\u672a\u751f\u6210,\u6574\u6761 sync \u5c31\u505c\u6446\u7b49\u4eba\u5de5\u4ecb\u5165\u3002\n    \u6709\u515c\u5e95\u540e\u7cfb\u7edf\u81ea\u6108:D1 \u662f\u6743\u5a01\u6e90\u3001\u51e0\u6beb\u79d2 API \u67e5\u8be2,\u4e0d\u70e7 R2 LIST\u3002'
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise RuntimeError('PAGES_KEY \u8bfb\u4e0d\u5230 + \u7f3a CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN,\u65e0\u6cd5\u4ece D1 \u515c\u5e95')
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    
    # 2026-07-22: \u591a\u53d6 pan_dir_id(123 \u6587\u4ef6\u5939id)\u2014\u2014\u56fe\u5df2\u8fc1 123,\u53d6\u56fe\u8d70 fetch_page_from_123 \u800c\u975e R2\u3002
    sql = ("SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
           "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
    r = requests.post(url, headers={"Authorization": "Bearer "+tok}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"): raise RuntimeError(f"D1 \u67e5\u8be2\u5931\u8d25: {str(j.get('errors',''))[:200]}")
    rows = (j.get("result") or [{}])[0].get("results") or []
    # pages: book_id -> {"pc": \u9875\u6570, "pdid": 123\u6587\u4ef6\u5939id}
    pages = {row["book_id"]: {"pc": int(row["page_count"]), "pdid": row.get("pan_dir_id")}
             for row in rows if row.get("book_id") and row.get("page_count")}
    return pages


def list_groups():
    # Page manifest (book_id -> page_count) is built from the D1 catalog by deploy.py.
    # Reading it (1 GET) replaces a full-bucket ListObjects scan PER SHARD: with 200 shards
    # the old version cost ~200 x full scans of a 3.26M-object bucket = ~600K+ Class A LIST/run.
    # Keys are deterministic (book/{id}/page_{NNNN}.webp), so no R2 listing is needed at all.
    pk = os.environ.get("PAGES_KEY", "_cc/med_pages.json")
    try:
        pages = json.loads(s3.get_object(Bucket=SRC, Key=pk)["Body"].read().decode("utf-8"))
    except Exception as e:
        
        
        print(f"WARNING: PAGES_KEY {pk} unreadable ({e}) -> rebuilding from D1...", flush=True)
        pages = _rebuild_pages_from_d1()
        print(f"D1 rebuild ok: {len(pages)} books", flush=True)
        try:
            s3.put_object(Bucket=SRC, Key=pk, Body=json.dumps(pages, ensure_ascii=False).encode("utf-8"))
            print(f"cached back to R2: {pk}", flush=True)
        except Exception as e2:
            print(f"WARNING: cache-back failed ({e2}) -> next run will rebuild again, not fatal", flush=True)
    pre = (PFX.strip("/") if PFX else "book")
    groups = {}
    global GID_PDID
    GID_PDID = {}
    for bid, v in pages.items():
        # 兼容:新格式 v={"pc":页数,"pdid":123文件夹id};旧缓存 v=页数(int)则 pdid=None(缺 pan_dir_id 的书会走空->skip)
        if isinstance(v, dict):
            pc = v.get("pc"); pdid = v.get("pdid")
        else:
            pc = v; pdid = None
        gid = pre + "/" + bid
        groups[gid] = [f"{gid}/page_{n:04d}.webp" for n in range(1, int(pc) + 1)]
        GID_PDID[gid] = pdid
    return groups


def handle(gid, keys):
    keys.sort(key=lambda k: int(re.search(r"(\d+)\.\w+$", k.rsplit("/", 1)[-1]).group(1)))  # numeric page order for OCR, never string-sort
    gid_tail = gid.split("/")[-1]
    _key = _key_of(gid_tail)
    if _key not in NAMES:
        return ("skip-noname", "skip-noname")                  # no D1 title -> skip, never write book_id-named files (OCR cleanliness)
    disp = NAMES[_key]                                          # = D1 book_title; CJK from private storage
    need_zip = (disp + ".zip") not in DONE_ZIP
    need_pdf = (not ZIP_ONLY) and ((disp + ".pdf") not in DONE_PDF)
    if not need_zip and not need_pdf:
        return ("skip-done", "skip-done")                      # already in 123 -> skip BEFORE any R2 GET / 123 call -> no waste, no dup error
    import io, zipfile
    # 2026-07-22 根治: 图已于 2026-07-17 迁 123、R2 book/ 已删空。取图从 123(fetch_page_from_123),
    # 不再直读 R2(那会全 404、烧 Class B: 2026-07-21 单日 sync+ocr 共刷 566万次≈$2.08)。
    pdid = GID_PDID.get(gid)
    if not pdid:
        return ("skip-nopdid", "skip-nopdid")                  # 无 pan_dir_id(未迁123/合规待批)-> 跳,不取图
    blobs = []
    for k in keys:
        m = re.search(r"page_(\d+)\.webp$", k)
        if not m:
            continue
        b = fetch_page_from_123(pdid, m.group(1))              # 从 123 取该页 webp
        if b is None:
            continue                                            # 该页 123 里也没有 -> 跳(D1 页数可能多于实际)
        blobs.append((k.split("/")[-1], b))
    if not blobs:
        return ("skip-empty", "skip-empty")                    # 123 里一页都没取到 -> 跳,不上传空包
    st_a = "have"
    if need_zip:
        zp = os.path.join(TMP, gid_tail + ".zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
            for name, b in blobs:
                z.writestr(name, b)
        st_a = put_file(zp, DIR_A, disp + ".zip")              # zip name = D1 book_title
        os.remove(zp)
    st_b = "have"
    if need_pdf:
        pdfp = os.path.join(TMP, gid_tail + ".pdf")
        imgs = [Image.open(io.BytesIO(b)).convert("RGB") for _, b in blobs]
        imgs[0].save(pdfp, "PDF", save_all=True, append_images=imgs[1:])
        st_b = put_file(pdfp, DIR_B, disp + ".pdf")
        os.remove(pdfp)
    return st_a, st_b


def _req_of(g):                                   # book/zi021-0001-01 -> 021-0001
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def prep():
    # List the two 123 backup folders once and persist the set of names already there to R2.
    # Run-shards read it and skip already-backed-up books up front (no R2 GET, no 123 create, no dup error).
    def ls(parent):
        names = set(); last = 0
        while True:
            data = (pan("GET", f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}") or {}).get("data") or {}
            fl = data.get("fileList") or []
            for it in fl:
                if it.get("filename"):
                    names.add(it["filename"])
            last = data.get("lastFileId", -1)
            if last in (-1, None) or not fl:
                break
        return names
    zip_done = ls(DIR_A); pdf_done = ls(DIR_B)
    dk = os.environ.get("PAN_DONE_KEY", "_cc/pan_done.json")
    s3.put_object(Bucket=SRC, Key=dk, Body=json.dumps({"zip": sorted(zip_done), "pdf": sorted(pdf_done)}, ensure_ascii=False).encode("utf-8"))
    print(f"prep: 123 already-done zip={len(zip_done)} pdf={len(pdf_done)} -> {dk}", flush=True)


def main():
    groups = list_groups()
    items = sorted(groups.items())
    ak = os.environ.get("ALLOW_KEY")              # private allow-list (req numbers, one per line) in source bucket
    if ak:
        try:
            body = s3.get_object(Bucket=SRC, Key=ak)["Body"].read().decode("utf-8")
            allow = set(x.strip() for x in body.splitlines() if x.strip())
            items = [(g, k) for g, k in items if _req_of(g) in allow]
            print(f"allow-list active: {len(allow)} reqs -> {len(items)} groups", flush=True)
        except Exception as e:
            # 同 ocr.py/ocr_xf.py 2026-07-14 已定的做法: 对象缺失不当致命错误崩溃，不设白名单限制。
            print(f"WARN allow-list unavailable ({str(e)[:80]}) -> no whitelist filter this run, processing all groups", flush=True)
    mine = [(g, k) for i, (g, k) in enumerate(items) if i % TOTAL == SHARD]
    print(f"shard {SHARD}/{TOTAL} groups {len(mine)}/{len(items)}", flush=True)
    ledger = []
    ok = 0
    for g, keys in mine:
        if _rl["streak"] >= RL_BREAKER:
            print(f"circuit-breaker: {_rl['streak']} consecutive 123-API calls exhausted all retries "
                  f"(persistent rate-limit/token exhaustion) -> 123 account is out of quota right now. "
                  f"Stopping shard early at {ok}/{len(mine)} instead of burning the full runner window; "
                  f"remaining books stay unsynced and will be picked up next scheduled run (idempotent).",
                  flush=True)
            break
        try:
            a, b = handle(g, keys)
        except Exception as e:
            a = b = "err:" + str(e)[:50]      
        ledger.append({"gid": g.split("/")[-1], "pages": len(keys), "zip": a, "pdf": b})
        ok += 1
        if ok % 20 == 0:
            print(f"done {ok}/{len(mine)} last={g} a={a} b={b}", flush=True)
    lk = os.environ.get("LEDGER_PREFIX", "_ledger/") + f"shard_{SHARD}.json"
    s3.put_object(Bucket=SRC, Key=lk, Body=json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    print(f"=== shard {SHARD} complete {ok}/{len(mine)} | ledger -> {lk} ===", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "prep":
        prep()
    else:
        main()
