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
ZIP_ONLY = os.environ.get("ZIP_ONLY") == "1"   # 只备份 zip、跳过慢的 pdf 生成(pdf 冗余·123 的已删·创始人只要 zip)

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto",
                  config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}))  # 防 R2 半死连接挂死
# title map (req-number -> book name) loaded from private storage; source stays CJK-free
NAMES = {}
_nk = os.environ.get("NAME_KEY")
if _nk:
    NAMES = json.loads(s3.get_object(Bucket=SRC, Key=_nk)["Body"].read().decode("utf-8"))
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
        if "exceeded" in msg or "tokens number" in msg or "频繁" in msg or code in (429, 401):
            if code == 401:
                _tok["v"] = None                  # auth failed -> force token re-fetch
            time.sleep(delay); delay = min(delay * 2, 60); continue
        return last
    return last


def put_file(local_path, parent_id, name):
    # 06-20 验证过的直传(GitHub 云端·115 并发成功传 6501):create -> get_upload_url -> S.put -> complete。
    # ⚠️ S.put 超时必须慷慨(1200s)——之前改成 (15,300) 把"慢但能成"的 123 上传掐死了(write timed out)。
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
        if "重复" in msg or "已存在" in msg or "exist" in msg.lower():
            return "dup"
        return "err:" + msg[:40]
    url = (pan("POST", "/upload/v1/file/get_upload_url",
              {"preuploadID": pid, "sliceNo": 1}).get("data") or {}).get("presignedURL")
    with open(local_path, "rb") as f:                 # stream upload, no full read into memory
        S.put(url, data=f, timeout=1200)              # 06-20 proven 慷慨超时·绝不改激进短超时
    cd = pan("POST", "/upload/v1/file/upload_complete", {"preuploadID": pid}).get("data") or {}
    if cd.get("async"):
        for _ in range(180):
            time.sleep(1)
            if (pan("POST", "/upload/v1/file/upload_async_result",
                    {"preuploadID": pid}).get("data") or {}).get("completed"):
                return "ok"
        return "timeout"
    return "ok"


def list_groups():
    # Page manifest (book_id -> page_count) is built from the D1 catalog by deploy.py.
    # Reading it (1 GET) replaces a full-bucket ListObjects scan PER SHARD: with 200 shards
    # the old version cost ~200 x full scans of a 3.26M-object bucket = ~600K+ Class A LIST/run.
    # Keys are deterministic (book/{id}/page_{NNNN}.webp), so no R2 listing is needed at all.
    pk = os.environ.get("PAGES_KEY", "_cc/med_pages.json")
    pages = json.loads(s3.get_object(Bucket=SRC, Key=pk)["Body"].read().decode("utf-8"))
    pre = (PFX.strip("/") if PFX else "book")
    groups = {}
    for bid, pc in pages.items():
        gid = pre + "/" + bid
        groups[gid] = [f"{gid}/page_{n:04d}.webp" for n in range(1, int(pc) + 1)]
    return groups


def handle(gid, keys):
    keys.sort(key=lambda k: int(re.search(r"(\d+)\.\w+$", k.rsplit("/", 1)[-1]).group(1)))  # numeric page order for OCR, never string-sort
    gid_tail = gid.split("/")[-1]
    _p = re.sub(r"^\D+", "", gid_tail).split("-")               # normalize: strip prefix + leading zeros in volume no.
    _key = "-".join(_p[:-1] + [str(int(_p[-1]))]) if _p and _p[-1].isdigit() else "-".join(_p)
    if _key not in NAMES:
        return ("skip-noname", "skip-noname")                  # no D1 title -> skip, never write book_id-named files (OCR cleanliness)
    disp = NAMES[_key]                                          # = D1 book_title; CJK from private storage
    need_zip = (disp + ".zip") not in DONE_ZIP
    need_pdf = (not ZIP_ONLY) and ((disp + ".pdf") not in DONE_PDF)
    if not need_zip and not need_pdf:
        return ("skip-done", "skip-done")                      # already in 123 -> skip BEFORE any R2 GET / 123 call -> no waste, no dup error
    import io, zipfile
    # Fetch each page once, tolerating missing keys: D1 page_count may exceed the actual webp
    # pages in R2 for incomplete downloads, so a constructed key can 404 -> skip it, don't crash.
    blobs = []
    for k in keys:
        try:
            b = s3.get_object(Bucket=SRC, Key=k)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                continue
            raise
        blobs.append((k.split("/")[-1], b))
    if not blobs:
        return ("skip-empty", "skip-empty")                    # no pages actually in R2 -> skip, never upload empty
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
        body = s3.get_object(Bucket=SRC, Key=ak)["Body"].read().decode("utf-8")
        allow = set(x.strip() for x in body.splitlines() if x.strip())
        items = [(g, k) for g, k in items if _req_of(g) in allow]
        print(f"allow-list active: {len(allow)} reqs -> {len(items)} groups", flush=True)
    mine = [(g, k) for i, (g, k) in enumerate(items) if i % TOTAL == SHARD]
    print(f"shard {SHARD}/{TOTAL} groups {len(mine)}/{len(items)}", flush=True)
    ledger = []
    ok = 0
    for g, keys in mine:
        try:
            a, b = handle(g, keys)
        except Exception as e:
            a = b = "err:" + str(e)[:50]      # 一本超时/出错绝不拖垮整 shard,记错继续下一本(幂等下次续)
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
