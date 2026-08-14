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
import json as _json
import random as _random
from collections import OrderedDict

import requests

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
# One credential pair only, and it must be the pair that owns the book folders:
# PAN_CLIENT_ID/PAN_CLIENT_SECRET, the same pair ocr_ndl.py has been fetching
# these very pages with. There used to be a fallback to CTEXT_PAN_CID/SEC --
# which ctext_harvest.yml wires as a DIFFERENT pan account's PAN_CID/PAN_SEC.
# A fallback to another account's token is worse than no fallback: the token
# request succeeds, every folder listing comes back empty because the folders
# belong to someone else, and the shard reports a clean "0 new". Missing
# credentials must read as missing, not as an empty library.
CID = os.environ.get("PAN_CLIENT_ID", "")
SEC = os.environ.get("PAN_CLIENT_SECRET", "")
TIMEOUT = int(os.environ.get("PAN_TIMEOUT", "30"))
MAX_DIRS_CACHED = int(os.environ.get("PAN_MAX_DIRS", "64"))
LIST_PAGES_MAX = int(os.environ.get("PAN_LIST_PAGES", "40"))   # 100 files each
# 2026-08-15 新增:123 官方 5 QPS。此前本模块零限速零重试,20 并发 job 会互相限死,
#   失败又被记成"页不存在" —— 铺量前不修这个,跑多少并发都是在制造假缺页数据。
RETRY   = int(os.environ.get("PAN_RETRY", "4"))
BACKOFF = float(os.environ.get("PAN_BACKOFF", "0.6"))
MIN_GAP = float(os.environ.get("PAN_MIN_GAP", "0.25"))   # 4 QPS,在官方 5 QPS 下留余量
_last_call = [0.0]

def _throttle():
    """进程内最小调用间隔。跨 job 的并发限流靠调低 job 数,这里只保证单 job 不自伤。"""
    gap = time.time() - _last_call[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_call[0] = time.time()

_tok = {"v": None, "exp": 0.0}
TOK_JITTER = float(os.environ.get("PAN_TOK_JITTER", "8"))
TOK_KEY    = os.environ.get("PAN_TOK_KEY", "_cc/_pan_token.json")
_rand = _random.Random()

def _s3():
    """R2 客户端。缺任一凭据就返回 None —— 共享缓存是加分项,没有它也得能跑。"""
    ep, ak, sk, bk = (os.environ.get(k) for k in ("S_EP", "S_AK", "S_SK", "S_BUCKET"))
    if not (ep and ak and sk and bk):
        return None, None
    try:
        import boto3
        from botocore.config import Config as _C
        return boto3.client("s3", endpoint_url=ep, aws_access_key_id=ak,
                            aws_secret_access_key=sk,
                            config=_C(signature_version="s3v4",
                                      retries={"max_attempts": 2})), bk
    except Exception:
        return None, None

def _shared_token_get():
    c, bk = _s3()
    if not c:
        return None
    try:
        d = _json.loads(c.get_object(Bucket=bk, Key=TOK_KEY)["Body"].read().decode())
        if d.get("v") and time.time() < float(d.get("exp", 0)) - 120:
            return d["v"], float(d["exp"])
    except Exception:
        pass
    return None

def _shared_token_put(v, exp):
    c, bk = _s3()
    if not c:
        return
    try:
        c.put_object(Bucket=bk, Key=TOK_KEY,
                     Body=_json.dumps({"v": v, "exp": exp}).encode())
    except Exception:
        pass
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
    # 2026-08-15 加跨进程共享缓存。实证:并发 job 各自申请 token,123 返回
    #   `401 tokens number has exceeded the limit` —— 那不是 QPS 限流,是**账号能同时
    #   持有的 token 数量超限**。此前本模块只有进程内 _tok,20 个 Actions job = 20 个
    #   token 申请,必超。前台 pan123.js 20 天前就是这么解决的(isolate 内存 → KV → 才申请,
    #   注释原文「access_token 本身也占 QPS 配额」),OCR 这条线一直没跟上。
    #   这里用 R2 当共享存储(已知 key GET/PUT,不违反零 LIST)。
    shared = _shared_token_get()
    if shared:
        _tok["v"], _tok["exp"] = shared[0], shared[1]
        return shared[0]
    # 惊群:所有 job 同时启动会同时 miss。抖动一下,让先到的那个写回缓存,其余读到它。
    time.sleep(_rand.uniform(0, TOK_JITTER))
    shared = _shared_token_get()
    if shared:
        _tok["v"], _tok["exp"] = shared[0], shared[1]
        return shared[0]
    r = requests.post(PAN + "/api/v1/access_token",
                      headers={"Platform": "open_platform"},
                      json={"clientID": CID, "clientSecret": SEC}, timeout=TIMEOUT)
    d = (r.json() or {}).get("data") or {}
    tv = d.get("accessToken")
    if not tv:
        raise PanUnavailable("token refused: %s" % str(r.text)[:120])
    _tok["v"] = tv
    _tok["exp"] = now + 3000        # tokens last ~1h; refresh early
    _shared_token_put(tv, _tok["exp"])
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
    idx, last_id, first = {}, 0, None
    for _ in range(LIST_PAGES_MAX):
        r = requests.get(PAN + "/api/v2/file/list",
                         params={"parentFileId": pan_dir_id, "limit": 100,
                                 "lastFileId": last_id},
                         headers=_headers(), timeout=TIMEOUT)
        j = r.json() or {}
        if first is None:
            first = j
        d = j.get("data") or {}
        fl = d.get("fileList") or []
        for f in fl:
            fn = f.get("filename")
            fid = f.get("fileId") or f.get("fileID")
            if fn and fid:
                idx[fn] = fid
        last_id = d.get("lastFileId")
        if last_id in (None, -1) or not fl:
            break
    if not idx:
        # An empty folder and a refused listing are indistinguishable to the
        # caller -- both just return no page. Say which one it was, with the
        # API's own code/message, or the next person debugging a zero-output
        # run has nothing to go on.
        print("pan dir_index empty: folder=%s api code=%s msg=%s" % (
            key, (first or {}).get("code"), str((first or {}).get("message"))[:120]), flush=True)
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
    # 2026-08-15 补重试与限速。原实现:download_info 拿不到 downloadUrl 就直接
    #   return None —— 于是**「被限流」和「这一页真的不存在」返回值完全一样**，
    #   全被上游记成 miss_nopage。实测坐实:同一本同一页，间隔 2 秒重试 3/3 全成功
    #   (zi238-0001-23 / zi306-0226-15 / zi301-0119-02 各 ✅906KB|✅639KB|✅2870KB)；
    #   而快打时 15 本只成 9-12 本。所以此前所有"缺页率"数字都掺着限流噪声。
    #   123 官方 5 QPS，这里限到 4 QPS 留余量，并对失败做指数退避。
    url = None
    for attempt in range(RETRY):
        _throttle()
        try:
            r = requests.get(PAN + "/api/v1/file/download_info",
                             params={"fileId": fid}, headers=_headers(), timeout=TIMEOUT)
            j = r.json() or {}
        except Exception as e:
            j = {"code": "EXC", "message": str(e)[:80]}
        url = ((j.get("data") or {}) if isinstance(j.get("data"), dict) else {}).get("downloadUrl")
        if url:
            break
        if attempt == RETRY - 1:
            # 说清是哪一种失败,否则下一个人还是只能看到一个"页不存在"
            print("pan download_info 失败: fileId=%s code=%s msg=%s (已重试 %d 次)" % (
                fid, j.get("code"), str(j.get("message"))[:80], RETRY), flush=True)
        time.sleep(BACKOFF * (2 ** attempt))
    if not url:
        return None
    for attempt in range(RETRY):
        try:
            r = requests.get(url, timeout=max(TIMEOUT, 60))
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        time.sleep(BACKOFF * (2 ** attempt))
    return None


def available():
    """True when credentials are present. Cheap check the caller can do once
    before deciding whether the pan path is worth attempting at all."""
    return bool(CID and SEC)
