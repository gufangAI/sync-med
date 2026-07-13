# tidy 123 drive root: move loose files matching given patterns into a target folder.
# move-only (zero delete). Default: _ctext_*.txt -> folder named by TARGET_NAME.
import os, sys, re, time
import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
PATTERN = os.environ.get("PATTERN", r"^_ctext_.*\.txt$")
TARGET_NAME = os.environ.get("TARGET_NAME", "R2_text_corpus_backup")
DRY = os.environ.get("DRY", "") == "1"

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
            print(f"api {path} -> code={j.get('code')} msg={str(j.get('message'))[:80]}", flush=True)
            if r.status_code == 401:
                _tok["v"] = None
        except Exception as e:
            print(f"api {path} exc {type(e).__name__}", flush=True)
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


def locate():
    # SEARCH mode: find files by keyword across the whole drive, print their parent folders
    kw = os.environ.get("SEARCH_KW", "_ctext_")
    hits = []
    last = 0
    while True:
        j = pan("GET", f"/api/v2/file/list?parentFileId=0&limit=100&lastFileId={last}&searchData={kw}&searchMode=0")
        if not j:
            break
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True):
                continue
            hits.append(it)
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            break
    print(f"search hits: {len(hits)}", flush=True)
    parents = {}
    for it in hits[:300]:
        parents.setdefault(it.get("parentFileId"), []).append(it.get("filename"))
    for pid, names in parents.items():
        info = pan("GET", f"/api/v1/file/detail?fileID={pid}") if pid else None
        pname = ((info or {}).get("data") or {}).get("filename", "?")
        print(f"parent {pid} ({pname}): {len(names)} files, e.g. {names[:3]}", flush=True)


def scan_root_all():
    # TRASH mode: list EVERYTHING at root including trashed items (previous scans skipped trashed)
    n_live_d = n_live_f = 0
    trashed = []
    last = 0
    while True:
        j = pan("GET", f"/api/v2/file/list?parentFileId=0&limit=100&lastFileId={last}")
        if not j:
            break
        d = j.get("data") or {}
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True):
                trashed.append((it.get("filename"), it.get("type")))
            elif it.get("type") == 1:
                n_live_d += 1
            else:
                n_live_f += 1
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            break
    print(f"root live: dirs={n_live_d} files={n_live_f}  trashed-at-root: {len(trashed)}", flush=True)
    for name, t in trashed[:40]:
        print(f"  [trash] {'D' if t==1 else 'F'} {name}", flush=True)


def main():
    if os.environ.get("TRASH", "") == "1":
        scan_root_all()
        return
    if os.environ.get("SEARCH", "") == "1":
        locate()
        return
    pat = re.compile(PATTERN)
    # find or create target folder at root
    target = None
    loose = []
    for it in iter_children(0):
        if it.get("type") == 1 and it.get("filename") == TARGET_NAME:
            target = it.get("fileId")
        elif it.get("type") != 1 and pat.match(str(it.get("filename", ""))):
            loose.append((it.get("filename"), it.get("fileId")))
    print(f"loose files matching: {len(loose)}  target folder: {target}", flush=True)
    if not loose:
        print("nothing to tidy; exit", flush=True)
        return
    if DRY:
        for n, _ in loose[:20]:
            print("  would move:", n, flush=True)
        return
    if target is None:
        j = pan("POST", "/upload/v1/file/mkdir", {"name": TARGET_NAME, "parentID": 0})
        target = ((j or {}).get("data") or {}).get("dirID")
        if not target:
            sys.exit("mkdir failed")
        print(f"created folder {TARGET_NAME} -> {target}", flush=True)
    moved = 0
    ids = [f for _, f in loose]
    for i in range(0, len(ids), 90):
        batch = ids[i:i + 90]
        j = pan("POST", "/api/v1/file/move", {"fileIDs": batch, "toParentFileID": target})
        if j is not None:
            moved += len(batch)
        print(f"moved {moved}/{len(ids)}", flush=True)
    print(f"DONE tidy moved={moved} target={target}", flush=True)


if __name__ == "__main__":
    main()
