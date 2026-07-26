# coding: utf-8
# Fetch page images from the 123 pan for the OCR line (2026-07-27).
#
# Background. On 2026-07-17 book/ imagery was migrated off R2 and the objects
# deleted. sync.py adapted the same week -- its manifest rebuild started
# carrying "pdid" (the 123 folder id) so it could pull pages from the pan. ocr.py
# never did: it kept reading R2, got NoSuchKey on every page, and on 2026-07-22
# a stop-gap was added that HEADs the first key of the shard and exits the whole
# shard when it 404s. That stopped the Class B bleed (5.66M requests ~ $2.08 on
# 07-21) but it also means the OCR line has produced nothing since. Its own
# comment said what should happen next: R2 direct reads are retired, use 123.
#
# Why this is not just a copy of sync.py's fetch_page_from_123. That helper
# walks the folder listing on EVERY page fetch -- fine when you want one page,
# ruinous for OCR, which walks a book page by page: a 300-page book would become
# 300 full folder scans. Here the listing is done once per folder and kept as a
# filename -> fileId index for the life of the process. A shard touches many
# books, so the index is capped and evicted oldest-first rather than growing
# without bound.
#
# Zero-LIST discipline still applies in spirit: one listing per folder, never a
# per-page scan, and never a scan of anything wider than the book's own folder.
import os
import time
from collections import OrderedDict

import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
CID = os.environ.get("PAN_CLIENT_ID") or os.environ.get("CTEXT_PAN_CID", "")
SEC = os.environ.get("PAN_CLIENT_SECRET") or os.environ.get("CTEXT_PAN_SEC", "")
TIMEOUT = int(os.environ.get("PAN_TIMEOUT", "30"))
MAX_DIRS_CACHED = int(os.environ.get("PAN_MAX_DIRS", "64"))
LIST_PAGES_MAX = int(os.environ.get("PAN_LIST_PAGES", "40"))   # 100 files each

_tok = {"v": None, "exp": 0.0}
_dirs = OrderedDict()          # pan_dir_id -> {filename: fileId}


class PanUnavailable(Exception):
    """Credentials missing or token refused -- caller should fall back, not crash."""


def token():
    """Access token, cached until shortly before it expires."""
    now = time.time()
    if _tok["v"] and now < _tok["exp"]:
        return _tok["v"]
    if not (CID and SEC):
        raise PanUnavailable("no PAN_CLIENT_ID / PAN_CLIENT_SECRET in env")
    r = requests.post(PAN + "/api/v1/access_token",
                      headers={"Platform": "open_platform"},
                      json={"clientID": CID, "clientSecret": SEC}, timeout=TIMEOUT)
    d = (r.json() or {}).get("data") or {}
    tv = d.get("accessToken")
    if not tv:
        raise PanUnavailable("token refused: %s" % str(r.text)[:120])
    _tok["v"] = tv
    _tok["exp"] = now + 3000        # tokens last ~1h; refresh early
    return tv


def _headers():
    return {"Platform": "open_platform", "Authorization": "Bearer " + token()}


def dir_index(pan_dir_id):
    """filename -> fileId for one folder. Listed once, then cached.

    This is the whole point of the module: OCR asks for page after page out of
    the same folder, so the listing must not be repeated per page."""
    if not pan_dir_id:
        return {}
    key = str(pan_dir_id)
    if key in _dirs:
        _dirs.move_to_end(key)
        return _dirs[key]
    idx, last_id = {}, 0
    for _ in range(LIST_PAGES_MAX):
        r = requests.get(PAN + "/api/v2/file/list",
                         params={"parentFileId": pan_dir_id, "limit": 100,
                                 "lastFileId": last_id},
                         headers=_headers(), timeout=TIMEOUT)
        d = (r.json() or {}).get("data") or {}
        fl = d.get("fileList") or []
        for f in fl:
            fn = f.get("filename")
            fid = f.get("fileId") or f.get("fileID")
            if fn and fid:
                idx[fn] = fid
        last_id = d.get("lastFileId")
        if last_id in (None, -1) or not fl:
            break
    _dirs[key] = idx
    while len(_dirs) > MAX_DIRS_CACHED:
        _dirs.popitem(last=False)       # evict oldest folder, not this one
    return idx


def fetch_page(pan_dir_id, page_no):
    """Bytes of page_NNNN.webp, or None when the folder or the page is absent.

    Returns None rather than raising for a missing page: a book can legitimately
    have fewer pages on the pan than the manifest claims, and one gap must not
    take down the shard."""
    idx = dir_index(pan_dir_id)
    if not idx:
        return None
    fid = idx.get("page_%04d.webp" % int(page_no))
    if not fid:
        return None
    r = requests.get(PAN + "/api/v1/file/download_info",
                     params={"fileId": fid}, headers=_headers(), timeout=TIMEOUT)
    url = ((r.json() or {}).get("data") or {}).get("downloadUrl")
    if not url:
        return None
    r = requests.get(url, timeout=max(TIMEOUT, 60))
    return r.content if r.status_code == 200 else None


def available():
    """True when credentials are present. Cheap check the caller can do once
    before deciding whether the pan path is worth attempting at all."""
    return bool(CID and SEC)
