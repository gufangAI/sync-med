# enroll no-record books: read roster from R2, locate their 123 folders, count pages, INSERT via narrow endpoint.
# Roster (R2 _cc/*.json.gz): { "<book_id>": {"t": "<title>", "a": "<author>", "r": "<req_no>"}, ... }
import os, sys, csv, gzip, io, json, time
from concurrent.futures import ThreadPoolExecutor
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
ENROLL_URL = os.environ.get("ENROLL_URL", "https://gufangai.com/api/admin/asset/pan-enroll")
TOKEN = os.environ["ASSET_SYNC_TOKEN"]
ROSTER_KEY = os.environ.get("ROSTER_KEY", "_cc/fuji_enroll.json.gz")
SOURCE_ROOT = os.environ.get("SOURCE_ROOT", "fujikawa")
LIBRARY = os.environ.get("LIBRARY", "")
PREFIX = os.environ.get("FOLDER_PREFIX", "")  # only consider 123 folders whose name starts with this
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
PATHS = [p.split(",") for p in (os.environ.get("PAN_PATH")
         or "\u53e4\u7c4d,GufangP,yaofang;\u53e4\u7c4d,GufangP,guji").split(";") if p.strip()]

import boto3
from botocore.config import Config as BotoCfg

S = requests.Session()
_tok = {"v": None}


def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
               json={"clientID": PCID, "clientSecret": PSEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        sys.exit("access_token failed")
    return _tok["v"]


def pan(path):
    for att in range(8):
        h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
        try:
            r = S.get(PAN + path, headers=h, timeout=60)
            j = r.json()
            if j.get("code") == 0:
                return j
            if r.status_code == 401:
                _tok["v"] = None
        except Exception:
            pass
        time.sleep(min(60, 2 * (2 ** att)))
    return None


def iter_children(parent):
    last = 0
    while True:
        j = pan(f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            return
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True):
                continue
            yield it
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return


def find_child_folder(parent, name):
    for it in iter_children(parent):
        if it.get("filename") == name and it.get("type") == 1:
            return it.get("fileId")
    return None


def count_pages(fid):
    n = 0
    for it in iter_children(fid):
        if it.get("type") != 1 and str(it.get("filename", "")).startswith("page_") and it.get("size", 1):
            n += 1
    return n


def main():
    s3 = boto3.client("s3", endpoint_url=os.environ["S_EP"],
                      aws_access_key_id=os.environ["S_AK"], aws_secret_access_key=os.environ["S_SK"],
                      config=BotoCfg(retries={"max_attempts": 5}))
    obj = s3.get_object(Bucket=os.environ.get("S_BUCKET", "guyaofang-assets"), Key=ROSTER_KEY)
    roster = json.load(gzip.open(io.BytesIO(obj["Body"].read()), "rt", encoding="utf-8"))
    print(f"roster: {len(roster)} books", flush=True)

    # locate folders across both paths (last wins, same as aligner)
    fmap = {}
    for segs in PATHS:
        cur = 0
        for seg in segs:
            cur = find_child_folder(cur, seg)
            if cur is None:
                sys.exit(f"path segment not found: {seg!r}")
        for it in iter_children(cur):
            if it.get("type") == 1:
                name = it.get("filename")
                if not PREFIX or str(name).startswith(PREFIX):
                    fmap[name] = it.get("fileId")
    print(f"123 candidate folders (prefix={PREFIX!r}): {len(fmap)}", flush=True)

    todo = [(bid, fmap[bid]) for bid in roster if bid in fmap]
    missing_folder = [bid for bid in roster if bid not in fmap]
    print(f"to enroll: {len(todo)}  roster-without-folder: {len(missing_folder)}", flush=True)
    if LIMIT > 0:
        todo = todo[:LIMIT]

    stats = {"ok": 0, "exists": 0, "empty": 0, "err": 0}
    results = []

    def one(item):
        bid, fid = item
        pages = count_pages(fid)
        if pages < 1:
            return bid, fid, 0, "empty"
        meta = roster[bid]
        for att in range(4):
            try:
                r = requests.post(ENROLL_URL, headers={"X-Asset-Sync-Token": TOKEN},
                                  json={"book_id": bid, "book_title": meta.get("t"),
                                        "author": meta.get("a", ""), "req_no": meta.get("r", ""),
                                        "library": meta.get("l", LIBRARY), "source_root": SOURCE_ROOT,
                                        "page_count": pages, "pan_dir_id": str(fid)}, timeout=30)
                if r.status_code == 200:
                    return bid, fid, pages, "ok"
                if r.status_code == 409:
                    return bid, fid, pages, "exists"
                if r.status_code == 400:
                    return bid, fid, pages, "bad:" + r.text[:60]
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return bid, fid, pages, "err"

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        for res in ex.map(one, todo):
            results.append(res)
            st = res[3]
            key = st if st in stats else ("err" if st.startswith("bad") else st)
            stats[key] = stats.get(key, 0) + 1
            done += 1
            if done % 100 == 0:
                print(f"progress {done}/{len(todo)} {stats}", flush=True)

    print(f"DONE enroll total={len(todo)} {stats}", flush=True)
    os.makedirs("out", exist_ok=True)
    with open("out/enroll_result.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "pages", "status"])
        for row in results:
            w.writerow(row)
    with open("out/roster_without_folder.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id"])
        for b in missing_folder:
            w.writerow([b])


if __name__ == "__main__":
    main()
