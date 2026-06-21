# Cloud mirror worker. Runs on CI runner only (no local hop).
# For each source group: stream a zip (on disk, not memory) + build a pdf, push both to pan storage in
# separate folders. All credentials come from env (CI secrets); nothing hardcoded; no CJK in source.
import os, re, time, json, hashlib, tempfile
import boto3, requests
from botocore.exceptions import ClientError
from PIL import Image

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
SRC = os.environ["S_BUCKET"]; PFX = os.environ.get("S_PREFIX", "").strip("/")
PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
DIR_A = os.environ["PAN_DIR_A"]      # folder id for image archives
DIR_B = os.environ["PAN_DIR_B"]      # folder id for documents (separate)
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
TMP = os.environ.get("RUNNER_TEMP", tempfile.gettempdir())

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto")
# title map (req-number -> book name) loaded from private storage; source stays CJK-free
NAMES = {}
_nk = os.environ.get("NAME_KEY")
if _nk:
    NAMES = json.loads(s3.get_object(Bucket=SRC, Key=_nk)["Body"].read().decode("utf-8"))
S = requests.Session()
_tok = {"v": None, "t": 0}


def token():
    if time.time() - _tok["t"] > 1500:
        r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
                   json={"clientID": PCID, "clientSecret": PSEC}, timeout=60).json()
        _tok["v"] = (r.get("data") or {}).get("accessToken"); _tok["t"] = time.time()
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
                _tok["t"] = 0                     # force token refresh mid-flight
            time.sleep(delay); delay = min(delay * 2, 60); continue
        return last
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
        return "err:" + str(cr.get("message"))[:40]
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
    zp = os.path.join(TMP, gid_tail + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:
        for name, b in blobs:
            z.writestr(name, b)
    st_a = put_file(zp, DIR_A, disp + ".zip")         # zip name = D1 book_title too (was book_id)
    os.remove(zp)
    pdfp = os.path.join(TMP, gid_tail + ".pdf")
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for _, b in blobs]
    imgs[0].save(pdfp, "PDF", save_all=True, append_images=imgs[1:])
    st_b = put_file(pdfp, DIR_B, disp + ".pdf")
    os.remove(pdfp)
    return st_a, st_b


def _req_of(g):                                   # book/zi021-0001-01 -> 021-0001
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


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
        a, b = handle(g, keys)
        ledger.append({"gid": g.split("/")[-1], "pages": len(keys), "zip": a, "pdf": b})
        ok += 1
        if ok % 20 == 0:
            print(f"done {ok}/{len(mine)} last={g} a={a} b={b}", flush=True)
    lk = os.environ.get("LEDGER_PREFIX", "_ledger/") + f"shard_{SHARD}.json"
    s3.put_object(Bucket=SRC, Key=lk, Body=json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    print(f"=== shard {SHARD} complete {ok}/{len(mine)} | ledger -> {lk} ===", flush=True)


if __name__ == "__main__":
    main()
