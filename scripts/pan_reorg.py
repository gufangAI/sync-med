# reorganize 123 book folders into founder's convention (2026-07-14):
#   GufangP/gufang/<library>/<section>/<book folders...>
# move-only (fileId unchanged -> reader links stay intact). Unknown prefixes are left in place.
import os, sys, re, csv, time
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
LIMIT = int(os.environ.get("LIMIT", "0") or 0)   # max books to move this run (0 = all)
DRY = os.environ.get("DRY", "") == "1"
# source paths to drain (comma path; semicolon-separated)
# 2026-07-14: \u4e0b\u8f7d\u7ebf\u628a guji \u62c6\u6210 guji1/guji2 \u540e,\u786c\u7f16\u7801\u5355\u6570 "guji" \u4f1a src-not-found \u76f4\u63a5\u6574\u4e2a run \u70b8\u6389
# (\u8ddf pan_register.py \u8e29\u8fc7\u7684\u540c\u4e00\u4e2a\u5751)\u3002\u9ed8\u8ba4\u8def\u5f84\u6539\u6210\u5b9e\u9645\u5b58\u5728\u7684 yaofang/guji1/guji2;
# \u4e0d\u518d\u731c\u6d4b\u7684 \u53e4\u65b9webp/\u53e4\u7c4dwebp(webp\u56fe\u7247\u76ee\u5f55,\u975e\u4e66\u6587\u4ef6\u5939)/\u8f6c\u79fb(\u7528\u9014\u672a\u6838\u5b9e) \u4e00\u5f8b\u4e0d\u9ed8\u8ba4\u7eb3\u5165,
# \u8981\u6536\u7f16\u8fd9\u4e9b\u5f97\u663e\u5f0f\u4f20 SRC_PATHS\u3002
SRC = [p.split(",") for p in (os.environ.get("SRC_PATHS")
       or "\u53e4\u7c4d,GufangP,yaofang;\u53e4\u7c4d,GufangP,guji1;\u53e4\u7c4d,GufangP,guji2").split(";") if p.strip()]
ROOT_PATH = (os.environ.get("DEST_ROOT") or "\u53e4\u7c4d,GufangP,gufang").split(",")

# prefix -> (library, section) ; section "" = directly under library folder
NAIKAKU = "\u516c\u6587\u4e66\u9986\u5185\u9601"          # \u516c\u6587\u4e66\u9986\u5185\u9601
RULES = [
    (re.compile(r"^zi\d"),      (NAIKAKU, "\u5b50\u90e8")),      # \u5b50\u90e8
    (re.compile(r"^shi\d"),     (NAIKAKU, "\u53f2\u90e8")),      # \u53f2\u90e8
    (re.compile(r"^bie\d"),     (NAIKAKU, "\u5225\u90e8")),      # \u5225\u90e8
    (re.compile(r"^ji\d"),      (NAIKAKU, "\u96c6\u90e8")),      # \u96c6\u90e8
    (re.compile(r"^ndl-"),      ("\u56fd\u4f1a\u56fe\u4e66\u9986NDL", "")),
    (re.compile(r"^fuji-RB"),   ("\u4eac\u90fd\u5927\u5b66\u5bcc\u58eb\u5ddd\u6587\u5e93", "")),
    (re.compile(r"^nijl-"),     ("\u56fd\u6587\u5b66\u7814\u7a76\u8d44\u6599\u9986", "")),
    (re.compile(r"^osaka-"),    ("\u5927\u962a\u5e9c\u7acb\u56fe\u4e66\u9986", "")),
]

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


def pan(method, path, body=None):
    for att in range(8):
        h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
        try:
            r = S.request(method, PAN + path, headers=h, json=body, timeout=60)
            j = r.json()
            if j.get("code") == 0:
                return j
            msg = str(j.get("message", ""))[:90]
            print(f"api {path.split('?')[0]} code={j.get('code')} {msg}", flush=True)
            if r.status_code == 401:
                _tok["v"] = None
        except Exception as e:
            print(f"api exc {type(e).__name__}", flush=True)
        time.sleep(min(60, 2 * (2 ** att)))
    return None


def iter_children(parent):
    last = 0
    while True:
        j = pan("GET", f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
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


def ensure_folder(parent, name, cache):
    key = (parent, name)
    if key in cache:
        return cache[key]
    fid = find_child_folder(parent, name)
    if fid is None:
        if DRY:
            fid = -1
        else:
            j = pan("POST", "/upload/v1/file/mkdir", {"name": name, "parentID": parent})
            fid = ((j or {}).get("data") or {}).get("dirID")
            if not fid:
                sys.exit(f"mkdir failed: {name}")
            print(f"mkdir {name} -> {fid}", flush=True)
    cache[key] = fid
    return fid


def classify(name):
    tok = str(name).split()[0] if str(name).split() else str(name)
    bid = ("zi" + tok) if re.match(r"^\d{3}-", tok) else tok
    for pat, dest in RULES:
        if pat.match(bid):
            return dest
    return None


def main():
    cache = {}
    # resolve dest root
    cur = 0
    for seg in ROOT_PATH:
        cur = ensure_folder(cur, seg, cache)
    root = cur
    print(f"dest root -> {root}", flush=True)

    # collect movable books from source paths (flat: books sit at first level there)
    moves = {}   # (lib, sec) -> list of fileIds
    stats = {"planned": 0, "left": 0}
    left_rows = []
    for segs in SRC:
        cur = 0
        not_found = False
        for seg in segs:
            cur = find_child_folder(cur, seg)
            if cur is None:
                print(f"WARN src path not found, skipping: {'/'.join(segs)} (missing segment {seg!r})", flush=True)
                not_found = True
                break
        if not_found:
            continue
        for it in iter_children(cur):
            if it.get("type") != 1:
                continue
            dest = classify(it.get("filename"))
            if dest is None:
                stats["left"] += 1
                if len(left_rows) < 5000:
                    left_rows.append((it.get("filename"),))
                continue
            moves.setdefault(dest, []).append(it.get("fileId"))
            stats["planned"] += 1
            if LIMIT and stats["planned"] >= LIMIT:
                break
        if LIMIT and stats["planned"] >= LIMIT:
            break
    print(f"planned={stats['planned']} left-unknown={stats['left']}", flush=True)
    for (lib, sec), ids in moves.items():
        print(f"  {lib}/{sec or '-'}: {len(ids)}", flush=True)

    os.makedirs("out", exist_ok=True)
    with open("out/left_unknown.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["folder_name"])
        w.writerows(left_rows)

    if DRY:
        print("DRY run; no moves executed", flush=True)
        return

    moved = 0
    for (lib, sec), ids in moves.items():
        lib_id = ensure_folder(root, lib, cache)
        target = ensure_folder(lib_id, sec, cache) if sec else lib_id
        for i in range(0, len(ids), 90):
            batch = ids[i:i + 90]
            j = pan("POST", "/api/v1/file/move", {"fileIDs": batch, "toParentFileID": target})
            if j is None:
                print(f"move batch FAILED at {lib}/{sec} offset {i}", flush=True)
                continue
            moved += len(batch)
            if moved % 900 == 0:
                print(f"moved {moved}/{stats['planned']}", flush=True)
    print(f"DONE reorg moved={moved} left-unknown={stats['left']}", flush=True)


if __name__ == "__main__":
    main()
