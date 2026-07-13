# page-level audit: for every book with pan_dir_id, count real page_*.webp files in its 123 folder
# and compare against D1 page_count. Output per-book verdict + missing page numbers.
import os, sys, csv, re, time
from concurrent.futures import ThreadPoolExecutor
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
SYNC_URL = os.environ.get("SYNC_URL", "https://gufangai.com/api/admin/asset/pan-sync")
TOKEN = os.environ["ASSET_SYNC_TOKEN"]
LIMIT = int(os.environ.get("LIMIT", "0") or 0)
OFFSET = int(os.environ.get("OFFSET", "0") or 0)
CONC = int(os.environ.get("CONC", "6") or 6)
FIX = os.environ.get("FIX", "") == "1"
TOKEN2 = TOKEN  # alias for fix posts

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


def page_nums(fid):
    # returns (set of page numbers with size>0, zero-byte count, list failure flag)
    nums, zero = set(), 0
    last = 0
    while True:
        j = pan(f"/api/v2/file/list?parentFileId={fid}&limit=100&lastFileId={last}")
        if not j:
            return None, None, True
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True) or it.get("type") == 1:
                continue
            m = re.match(r"page_(\d+)\.webp$", str(it.get("filename", "")))
            if not m:
                continue
            if not it.get("size"):
                zero += 1
                continue
            nums.add(int(m.group(1)))
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return nums, zero, False


def main():
    r = requests.get(SYNC_URL + "?mode=audit", headers={"X-Asset-Sync-Token": TOKEN}, timeout=180)
    j = r.json() if r.status_code == 200 else {}
    books = j.get("books") or (j.get("data") or {}).get("books")
    if books is None:
        sys.exit(f"audit query failed: http{r.status_code} {str(j)[:150]}")
    books = books[OFFSET:]
    if LIMIT > 0:
        books = books[:LIMIT]
    print(f"books to audit: {len(books)} (offset={OFFSET})", flush=True)

    stats = {"ok": 0, "short": 0, "empty": 0, "extra": 0, "listfail": 0, "no_expect": 0}
    results = []

    def one(b):
        bid = b["book_id"]; fid = b["pan_dir_id"]; expect = b.get("page_count") or 0
        nums, zero, fail = page_nums(fid)
        if fail:
            return bid, expect, -1, "", 0, "listfail"
        have = len(nums)
        if not expect:
            return bid, 0, have, "", zero, "no_expect"
        missing = sorted(set(range(1, expect + 1)) - nums)
        if have == 0:
            st = "empty"
        elif missing:
            st = "short"
        elif have > expect:
            st = "extra"
        else:
            st = "ok"
        mtxt = ",".join(map(str, missing[:30])) + ("..." if len(missing) > 30 else "")
        return bid, expect, have, mtxt, zero, st

    done = 0
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        for res in ex.map(one, books):
            results.append(res)
            stats[res[5]] = stats.get(res[5], 0) + 1
            done += 1
            if done % 1000 == 0:
                print(f"progress {done}/{len(books)} {stats}", flush=True)

    print(f"DONE audit total={len(books)} {stats}", flush=True)

    if FIX:
        # page-refresh + revive: 123 is source of truth for arrived pages.
        # rule: have >= expect (fully arrived, possibly more) -> update page_count to have (if grew) + visible=1
        #       expect missing/0 and have >= 4 -> set page_count + visible=1
        #       have < expect -> leave (still uploading; stays delisted if it was)
        fixes = []
        bm = {b["book_id"]: b for b in books}
        for bid, expect, have, mtxt, zero, st in results:
            if have is None or have < 0 or have < 4:
                continue
            vis = (bm.get(bid) or {}).get("frontend_visible")
            grew = expect and have > expect
            arrived_hidden = expect and have >= expect and vis == 0
            fresh = (not expect)
            if grew or arrived_hidden or fresh:
                fixes.append((bid, have))
        print(f"FIX candidates: {len(fixes)}", flush=True)
        fstats = {"ok": 0, "err": 0}
        def fix_one(t):
            bid, have = t
            b = bm.get(bid) or {}
            for att in range(4):
                try:
                    r = requests.post(SYNC_URL, headers={"X-Asset-Sync-Token": TOKEN2},
                                      json={"book_id": bid, "table": "books_assets_v2",
                                            "pan_dir_id": str(b.get("pan_dir_id")),
                                            "page_count": have, "frontend_visible": 1}, timeout=30)
                    if r.status_code == 200:
                        return "ok"
                except Exception:
                    pass
                time.sleep(2 * (att + 1))
            return "err"
        with ThreadPoolExecutor(max_workers=6) as ex2:
            for st2 in ex2.map(fix_one, fixes):
                fstats[st2] += 1
        print(f"DONE fix {fstats}", flush=True)

    os.makedirs("out", exist_ok=True)
    with open("out/audit_all.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "expect", "have", "missing_pages", "zero_byte", "status"])
        for row in results:
            w.writerow(row)
    with open("out/audit_problem.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["book_id", "expect", "have", "missing_pages", "zero_byte", "status"])
        for row in results:
            if row[5] != "ok":
                w.writerow(row)


if __name__ == "__main__":
    main()
