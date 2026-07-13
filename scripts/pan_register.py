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
    # first token is the id when folder is named "<id> <title...>" (2026-07-14 founder convention);
    # naikaku segments may lack the catalog prefix: 301-0027-01 -> zi301-0027-01
    import re as _re
    tok = str(name).split()[0] if str(name).split() else str(name)
    if _re.match(r"^\d{3}-", tok):
        return "zi" + tok
    return tok


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

    # recursive discovery (2026-07-14): book folder = first token resolves in D1 map (or raw known id);
    # unknown folders are category layers -> descend, up to depth 5. Never descend into book folders.
    known = set(d1map.keys()) if d1map is not None else None
    folders, files_seen = [], 0
    unrecognized = []

    import re as _re
    BOOKISH = _re.compile(r"^([a-z]{0,8}\d{2,3}-\d{4}(-\d+)?|(ndl|nijl|osaka)-\d+|fuji-RB\d+|[a-z]{2,8}[_-]?\d{1,6})$")

    def kind_of(name):
        # book: id resolves in D1; orphan: looks like a book id but has no D1 record (enroll
        # candidate; do NOT descend -- it holds page files, listing them wasted ~10k calls / 2h);
        # layer: category folder -> descend
        bid = to_book_id(name)
        tok = str(name).split()[0] if str(name).split() else str(name)
        if known is not None and bid in known:
            return "book"
        if BOOKISH.match(tok) or BOOKISH.match(bid):
            return "book" if known is None else "orphan"
        return "layer"

    def walk(fid, depth, trail):
        nonlocal files_seen
        subdirs = []
        for it in iter_children(fid):
            if it.get("type") == 1:
                subdirs.append((it.get("filename"), it.get("fileId")))
            else:
                files_seen += 1
        for name, cid in subdirs:
            k = kind_of(name)
            if k == "book":
                folders.append((name, cid))
                if len(folders) % 2000 == 0:
                    print(f"..{len(folders)} folders", flush=True)
            elif k == "orphan":
                unrecognized.append((f"{trail}/{name}", cid))
            elif depth < 5:
                print(f"  descend [{trail}/{name}]", flush=True)
                walk(cid, depth + 1, f"{trail}/{name}")
            else:
                unrecognized.append((f"{trail}/{name}", cid))

    for segs in PATHS:
        cur = 0
        for seg in segs:
            cur = find_child_folder(cur, seg)
            if cur is None:
                sys.exit(f"path segment not found: {seg!r}")
        print(f"path ok ({len(segs)} segs) -> {cur}", flush=True)
        n0 = len(folders)
        walk(cur, 0, "~")
        print(f"  subtotal this path: {len(folders) - n0}", flush=True)
    print(f"SCAN total folders={len(folders)} loose_files={files_seen} unrecognized={len(unrecognized)}", flush=True)
    os.makedirs("out", exist_ok=True)
    with open("out/unrecognized.csv", "w", newline="", encoding="utf-8") as f0:
        w0 = csv.writer(f0)
        w0.writerow(["path", "fileId"])
        for row in unrecognized:
            w0.writerow(row)

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
    # dedup same book across paths (a book may exist in both yaofang and guji): last wins,
    # matching final write order, so the next align run sees it as stable instead of flip-flopping
    _d = {}
    for n, f in folders:
        _d[to_book_id(n)] = (n, f)
    folders = list(_d.values())
    print(f"after dedup: {len(folders)}", flush=True)
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
                                        "pan_dir_id": str(fid), "frontend_visible": 1}, timeout=30)
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
