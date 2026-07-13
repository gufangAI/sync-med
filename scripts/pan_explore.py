# explore 123 drive structure: walk top levels, count children per folder,
# sample file names, and count DIR_A / DIR_B backup archives.
import os, sys, time, collections
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
DIR_A = os.environ.get("PAN_DIR_A")
DIR_B = os.environ.get("PAN_DIR_B")

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


def children(parent, cap=200000):
    out = []
    last = 0
    while len(out) < cap:
        j = pan(f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            break
        d = j.get("data") or {}
        fl = d.get("fileList") or []
        for it in fl:
            if it.get("trashed") in (1, True):
                continue
            out.append(it)
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            break
    return out


def count_children(parent):
    n_dir = n_file = 0
    last = 0
    while True:
        j = pan(f"/api/v2/file/list?parentFileId={parent}&limit=100&lastFileId={last}")
        if not j:
            break
        d = j.get("data") or {}
        fl = d.get("fileList") or []
        for it in fl:
            if it.get("trashed") in (1, True):
                continue
            if it.get("type") == 1:
                n_dir += 1
            else:
                n_file += 1
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            break
    return n_dir, n_file


def show_tree(parent, name, depth, max_depth):
    pad = "  " * depth
    kids = children(parent, cap=100000)
    dirs = [k for k in kids if k.get("type") == 1]
    files = [k for k in kids if k.get("type") != 1]
    print(f"{pad}{name}/  [dirs={len(dirs)} files={len(files)}]", flush=True)
    if files[:3]:
        ex = ", ".join(f.get("filename", "?") for f in files[:3])
        print(f"{pad}  sample files: {ex}", flush=True)
    if depth < max_depth:
        big = 0
        for d in dirs[:40]:
            show_tree(d.get("fileId"), d.get("filename"), depth + 1, max_depth)
            big += 1
        if len(dirs) > big:
            print(f"{pad}  ... and {len(dirs) - big} more dirs", flush=True)


def main():
    print("== drive root ==", flush=True)
    show_tree(0, "<root>", 0, 2)

    for label, fid in (("DIR_A", DIR_A), ("DIR_B", DIR_B)):
        if not fid:
            continue
        kids = children(fid)
        dirs = sum(1 for k in kids if k.get("type") == 1)
        files = [k for k in kids if k.get("type") != 1]
        ext = collections.Counter((f.get("filename", "").rsplit(".", 1)[-1] or "?").lower() for f in files)
        print(f"== {label} (id={fid}): dirs={dirs} files={len(files)} ext={dict(ext.most_common(5))}", flush=True)
        for f in files[:5]:
            print(f"  sample: {f.get('filename')}", flush=True)


if __name__ == "__main__":
    main()
