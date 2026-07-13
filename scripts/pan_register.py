# scan 123 page-image folders and register dir ids into D1 via the narrow sync endpoint.
# MODE=scan     -> enumerate folders under /<root>/GufangP/yaofang, dump CSV, peek one sample folder
# MODE=register -> same enumeration, then POST each (book_id, fileId) to SYNC_URL (idempotent)
import os, sys, csv, time
from concurrent.futures import ThreadPoolExecutor
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
SYNC_URL = os.environ.get("SYNC_URL", "https://gufangai.com/api/admin/asset/pan-sync")
MODE = os.environ.get("MODE", "scan")
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
# path segments from drive root; first segment is CJK, kept escaped (public repo opsec)
PATHS = [p.split(",") for p in (os.environ.get("PAN_PATH")
         or "\u53e4\u7c4d,GufangP,yaofang;\u53e4\u7c4d,GufangP,guji").split(";") if p.strip()]

def to_book_id(name):
    # 123 folder names for naikaku segments lack the catalog prefix: 301-0027-01 -> zi301-0027-01
    import re as _re
    if _re.match(r"^\d{3}-", name):
        return "zi" + name
    return name


S = requests.Session()
_tok = {"v": None}


def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
               json={"clientID": PCID, "clientSecret": PSEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        sys.exit("access_token failed: " + r.text[:200])
    return _tok["v"]


def pan(method, path, body=None):
    for att in range(8):
        h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
        try:
            r = S.request(method, PAN + path, headers=h, json=body, timeout=60)
            j = r.json()
            if j.get("code") == 0:
                return j
            msg = str(j.get("message", ""))
            if r.status_code == 401 or "token" in msg.lower():
                _tok["v"] = None
        except Exception:
            pass
        time.sleep(min(60, 2 * (2 ** att)))
    return None


def iter_children(parent):
    last = 0
    while True:
        j = pan("GET", f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            print(f"list failed at parent={parent} last={last}", flush=True)
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


def main():
    d1map = None
    if MODE == "auto":
        tok = os.environ["ASSET_SYNC_TOKEN"]
        r = requests.get(SYNC_URL + "?mode=map", headers={"X-Asset-Sync-Token": tok}, timeout=180)
        j = r.json() if r.status_code == 200 else {}
        d1map = j.get("map") or (j.get("data") or {}).get("map")
        if d1map is None:
            sys.exit(f"map query failed: http{r.status_code} {str(j)[:150]}")
        print(f"D1 visible books: {len(d1map)}", flush=True)

    folders, files_seen = [], 0
    for segs in PATHS:
        cur = 0
        for seg in segs:
            cur = find_child_folder(cur, seg)
            if cur is None:
                sys.exit(f"path segment not found: {seg!r}")
        print(f"path ok ({len(segs)} segs) -> {cur}", flush=True)
        n0 = len(folders)
        for it in iter_children(cur):
            if it.get("type") == 1:
                folders.append((it.get("filename"), it.get("fileId")))
                if len(folders) % 2000 == 0:
                    print(f"..{len(folders)} folders", flush=True)
            else:
                files_seen += 1
        print(f"  subtotal this path: {len(folders) - n0}", flush=True)
    print(f"SCAN total folders={len(folders)} loose_files={files_seen}", flush=True)

    os.makedirs("out", exist_ok=True)
    with open("out/pan_scan.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId"])
        for name, fid in folders:
            w.writerow([name, fid])

    if folders:
        name0, fid0 = folders[0]
        sample = [it.get("filename") for _, it in zip(range(5), iter_children(fid0))]
        print(f"sample folder {name0} ({fid0}) -> {sample}", flush=True)

    if MODE not in ("register", "auto"):
        return

    tok = os.environ["ASSET_SYNC_TOKEN"]
    if d1map is not None:
        # full alignment: 123 is the source of truth. write when D1 lacks pan_dir_id or it differs.
        seen_fids = set()
        todo2 = []
        n_new = n_changed = n_same = n_nod1 = 0
        for n, f in folders:
            bid = to_book_id(n)
            seen_fids.add(str(f))
            if bid not in d1map:
                n_nod1 += 1
                continue
            cur = str(d1map.get(bid) or "")
            if cur == str(f):
                n_same += 1
            elif cur == "":
                n_new += 1
                todo2.append((n, f))
            else:
                n_changed += 1
                todo2.append((n, f))
        stale = [b for b, v in d1map.items() if v and str(v) not in seen_fids]
        print(f"align: new={n_new} changed={n_changed} same={n_same} not-in-D1={n_nod1} stale-in-D1={len(stale)}", flush=True)
        os.makedirs("out", exist_ok=True)
        with open("out/stale_pan_in_d1.csv", "w", newline="", encoding="utf-8") as f2:
            w2 = csv.writer(f2)
            w2.writerow(["book_id", "pan_dir_id_not_seen_in_scan"])
            for b in stale:
                w2.writerow([b, d1map[b]])
        folders = todo2
        if not folders:
            print("aligned: nothing to write; exit", flush=True)
            return
    todo = folders[:LIMIT] if LIMIT > 0 else folders
    stats = {"ok": 0, "notfound": 0, "err": 0}
    results = []

    def reg(item):
        name, fid = item
        for att in range(4):
            try:
                r = requests.post(SYNC_URL, headers={"X-Asset-Sync-Token": tok},
                                  json={"book_id": to_book_id(name), "table": "books_assets_v2",
                                        "pan_dir_id": str(fid)}, timeout=30)
                if r.status_code == 200:
                    return name, fid, "ok"
                if r.status_code == 404:
                    return name, fid, "notfound"
                if r.status_code in (401, 400):
                    return name, fid, f"http{r.status_code}"
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return name, fid, "err"

    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(reg, todo):
            results.append(res)
            st = res[2]
            stats["ok" if st == "ok" else ("notfound" if st == "notfound" else "err")] += 1
            done += 1
            if done % 1000 == 0:
                print(f"progress {done}/{len(todo)} {stats}", flush=True)

    print(f"DONE register total={len(todo)} {stats}", flush=True)
    with open("out/pan_register.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "fileId", "status"])
        for row in results:
            w.writerow(row)


if __name__ == "__main__":
    main()
