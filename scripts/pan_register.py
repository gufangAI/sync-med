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
PATHS = ([p.split(",") for p in os.environ["PAN_PATH"].split(";") if p.strip()]
         if os.environ.get("PAN_PATH") else None)  # None -> auto-discover under GufangP

_CN_CAT_PREFIX = {"\u5b50": "zi", "\u53f2": "shi", "\u5225": "bie", "\u522b": "bie", "\u96c6": "ji", "\u7d93": "jing", "\u7ecf": "jing"}

def to_book_id(name):
    # 2026-07-14 two-layer convention: volume folder named "<id> <title>", first token = book_id.
    # Chinese catalog prefix -> pinyin to match D1 book_id (e.g. \u5225024-... -> bie024-...);
    # bare naikaku segment 301-0027-01 -> zi301-0027-01.
    import re as _re
    tok = str(name).split()[0] if str(name).split() else str(name)
    m = _re.match(r"^([\u4e00-\u9fff])(\d{2,3}-\d{4}.*)$", tok)
    if m and m.group(1) in _CN_CAT_PREFIX:
        return _CN_CAT_PREFIX[m.group(1)] + m.group(2)
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

    # auto-discover scan roots under GufangP when PAN_PATH unset (download line keeps reshaping 123:
    # guji->guji1/guji2, added \u53e4\u65b9webp/\u53e4\u7c4dwebp...). Scan every child dir except transit/PDF/archive.
    global PATHS
    if PATHS is None:
        gp = 0
        for seg in ["\u53e4\u7c4d", "GufangP"]:
            gp = find_child_folder(gp, seg)
            if gp is None:
                sys.exit("GufangP root not found for auto-discover")
        roots = []
        for it in iter_children(gp):
            if it.get("type") != 1:
                continue
            nm = it.get("filename") or ""
            if nm in ("\u8f6c\u79fb", "fodian") or "\u539f\u6863" in nm or "\u5f52\u6863" in nm or "PDF" in nm:
                continue  # skip \u8f6c\u79fb / fodian / *\u539f\u6863* / *\u5f52\u6863* / *PDF*
            roots.append(["\u53e4\u7c4d", "GufangP", nm])
        PATHS = roots
        print("auto-discovered %d roots: %s" % (len(roots), [r[-1] for r in roots]), flush=True)

    # recursive discovery (2026-07-14): book folder = first token resolves in D1 map (or raw known id);
    # unknown folders are category layers -> descend, up to depth 5. Never descend into book folders.
    known = set(d1map.keys()) if d1map is not None else None
    folders, files_seen = [], 0
    unrecognized = []

    import re as _re
    FULL_ID = _re.compile(r"^([a-z]{1,8}\d{2,3}-\d{4}-\d+|(ndl|nijl|osaka)-\d+|fuji-RB\d+|[a-z]{2,8}[_-]?\d{1,6}-\d+)$")
    WORK_ID = _re.compile(r"^[a-z]{1,8}\d{2,3}-\d{4}$")

    def trailing_vol(name):
        m = _re.search(r"(\d+)\s*$", str(name))
        return int(m.group(1)) if m else None

    def _bump():
        if len(folders) % 2000 == 0:
            print("..%d folders" % len(folders), flush=True)

    def walk(fid, depth, trail, agg=None):
        # agg = work-level reqno (e.g. 'zi300-0074') when inside a book-aggregate folder;
        #       then this level's children are VOLUME folders.
        nonlocal files_seen
        subdirs = []
        for it in iter_children(fid):
            if it.get("type") == 1:
                subdirs.append((it.get("filename"), it.get("fileId")))
            else:
                files_seen += 1
        for name, cid in subdirs:
            rid = to_book_id(name)
            if agg is not None:
                # inside aggregate -> volume folder
                if (known is not None and rid in known) or FULL_ID.match(rid):
                    folders.append((rid, cid)); _bump()          # volume named with full id
                    continue
                v = trailing_vol(name)
                if v is not None:
                    folders.append(("%s-%02d" % (agg, v), cid)); _bump()   # title+volnum -> agg-05
                else:
                    unrecognized.append(("%s/%s" % (trail, name), cid))
                continue
            # not inside aggregate. ORDER MATTERS: known full-id first, then two-seg reqno as
            # aggregate (WORK before FULL so 'zi300-0074' descends instead of matching FULL's wide arm).
            if known is not None and rid in known:
                folders.append((rid, cid)); _bump()              # known full id (old flat layout)
            elif WORK_ID.match(rid):
                walk(cid, depth + 1, "%s/%s" % (trail, name), agg=rid)     # book-aggregate -> volumes
            elif FULL_ID.match(rid):
                folders.append((rid, cid)); _bump()              # full id, scan mode (known=None)
            elif depth < 6:
                walk(cid, depth + 1, "%s/%s" % (trail, name))    # category layer
            else:
                unrecognized.append(("%s/%s" % (trail, name), cid))

    for segs in PATHS:
        cur = 0
        ok = True
        for seg in segs:
            nxt = find_child_folder(cur, seg)
            if nxt is None:
                # tolerate: a path may be renamed/moved while download line reorganizes 123.
                # skip this path (do NOT abort whole run) + print real siblings to locate new layout.
                sibs = [it.get("filename") for it in iter_children(cur) if it.get("type") == 1][:40]
                print("path seg %r not found under parent %s; siblings: %s" % (seg, cur, sibs), flush=True)
                ok = False
                break
            cur = nxt
        if not ok:
            continue
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
        for bid, fid in folders:
            w.writerow([bid, fid])

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
    for bid, f in folders:
        _d[bid] = (bid, f)
    folders = list(_d.values())
    print(f"after dedup: {len(folders)}", flush=True)
    if d1map is not None:
        # full alignment: 123 is the source of truth. write when D1 lacks pan_dir_id or it differs.
        seen_fids = set()
        todo2 = []
        n_new = n_changed = n_same = n_nod1 = 0
        for bid, f in folders:
            seen_fids.add(str(f))
            if bid not in d1map:
                n_nod1 += 1
                continue
            cur = str(d1map.get(bid) or "")
            if cur == str(f):
                n_same += 1
            elif cur == "":
                n_new += 1
                todo2.append((bid, f))
            else:
                n_changed += 1
                todo2.append((bid, f))
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
        bid, fid = item
        for att in range(4):
            try:
                r = requests.post(SYNC_URL, headers={"X-Asset-Sync-Token": tok},
                                  json={"book_id": bid, "table": "books_assets_v2",
                                        "pan_dir_id": str(fid), "frontend_visible": 1}, timeout=30)
                if r.status_code == 200:
                    return bid, fid, "ok"
                if r.status_code == 404:
                    return bid, fid, "notfound"
                if r.status_code in (401, 400):
                    return bid, fid, f"http{r.status_code}"
            except Exception:
                pass
            time.sleep(2 * (att + 1))
        return bid, fid, "err"

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
