# -*- coding: utf-8 -*-
# CC-down: cloud OCR worker - GitHub Actions + iFlytek MaaS HunyuanOCR (free vision OCR).
# 2026-07-25 创始人令「别抓R2,走123 API取图」:book/ 影像已于 07-17 迁 123、R2 已空,
# 本线由「R2 manifest + s3.get_object」整体切为「D1 拉书目(含 pan_dir_id) + 123 open API 拉图」
# (与 ocr_ndl.py 同一取图路径;每书目录缓存省 file/list 调用,限流退避温和并发)。
# 识别结果落点不变: R2 _ocr/{id}/page_NNNN.txt (SueAI fuel), 质量闸 reject 落 _ocr_rejected/。
import os, io, re, json, base64, sys, time, threading, requests, boto3
from concurrent.futures import ThreadPoolExecutor
import ocr_quality   # OCR 质量闸:LLM 视觉 OCR 幻觉/乱码检测(纯规则,治讯飞幻觉静默入库)

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
PAN_CID = os.environ["PAN_CLIENT_ID"]; PAN_SEC = os.environ["PAN_CLIENT_SECRET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
XF_BASE = os.environ.get("XF_BASE", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_MODEL", "xophunyuanocr")
WORKERS = int(os.environ.get("WORKERS", "10"))   # OCR调用延迟摊薄后,123 API 有效QPS很低,温和口径
# OCR instruction (unicode-escaped to keep this public-repo source free of literal CJK):
# decodes to "recognize all text in the image, output text only"
PROMPT = "\u8bc6\u522b\u56fe\u4e2d\u6240\u6709\u6587\u5b57\uff0c\u53ea\u8f93\u51fa\u6587\u5b57"

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")


def parse_keys(raw):
    raw = (raw or "").strip()
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [k.strip() for k in v if ":" in str(k)]
    except Exception:
        pass
    parts = re.split(r"[\s,]+", raw)
    return [p.strip() for p in parts if ":" in p]


KEYS = parse_keys(os.environ.get("XF_KEYS", ""))
if not KEYS:
    raise SystemExit("no XF_KEYS")
KEY = KEYS[SHARD % len(KEYS)]   # this shard binds one account
sess = threading.local()


def http():
    if not hasattr(sess, "s"):
        s = requests.Session(); s.trust_env = False
        sess.s = s
    return sess.s


def reqof(g):
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def d1_query(sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 query failed: {str(j.get('errors', ''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []


# ── 123 open API 取图(与 ocr_ndl.py 同路径;多线程加锁 + 每书目录缓存 + 限流退避) ──
PAN = "https://open-api.123pan.com"
_pan_lock = threading.Lock()
_tok = {"v": None}
_dir_cache = {}   # pan_dir_id -> {filename: fileId} 一书一次全量列目,省 file/list 调用


def pan_token():
    with _pan_lock:
        if _tok["v"]:
            return _tok["v"]
        r = requests.post(PAN + "/api/v1/access_token",
                          headers={"Platform": "open_platform", "Content-Type": "application/json"},
                          json={"clientID": PAN_CID, "clientSecret": PAN_SEC}, timeout=30)
        _tok["v"] = (r.json().get("data") or {}).get("accessToken")
        if not _tok["v"]:
            raise SystemExit("123 token fail: " + r.text[:200])
        return _tok["v"]


def _pan_get(path, params):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + pan_token()}
    for attempt in range(4):
        try:
            r = http().get(PAN + path, params=params, headers=h, timeout=30)
            j = r.json()
            if r.status_code == 200 and j.get("code") == 0:
                return j.get("data") or {}
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))   # 限流/瞬断退避
    return None


def dir_listing(pan_dir_id):
    with _pan_lock:
        if pan_dir_id in _dir_cache:
            return _dir_cache[pan_dir_id]
    m, last_id = {}, 0
    for _ in range(80):
        d = _pan_get("/api/v2/file/list", {"parentFileId": pan_dir_id, "limit": 100, "lastFileId": last_id})
        if d is None:
            break
        fl = d.get("fileList") or []
        for f in fl:
            fid = f.get("fileId") or f.get("fileID")
            if f.get("filename") and fid:
                m[f["filename"]] = fid
        last_id = d.get("lastFileId")
        if last_id in (None, -1) or not fl:
            break
    with _pan_lock:
        _dir_cache[pan_dir_id] = m
    return m


def fetch_page_from_123(pan_dir_id, page_str):
    fid = dir_listing(pan_dir_id).get(f"page_{page_str}.webp")
    if not fid:
        return None
    d = _pan_get("/api/v1/file/download_info", {"fileId": fid})
    url = (d or {}).get("downloadUrl")
    if not url:
        return None
    for attempt in range(3):
        try:
            r = http().get(url, timeout=60)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def ocr_page(b64):
    for _ in range(3):
        try:
            r = http().post(XF_BASE + "/chat/completions",
                headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
                json={"model": XF_MODEL, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b64}}]}]},
                timeout=120, verify=False)
            if r.status_code == 200:
                c = r.json()["choices"][0]["message"]["content"]
                return (c or "").strip()
        except Exception:
            pass
    return None


# 同 ocr.py 2026-07-14 已定的做法: ALLOW_KEY 对象缺失时不当致命错误崩溃，退化为不设白名单限制
allow = None
try:
    allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())
    print(f"allow-list loaded: {len(allow)} reqs", flush=True)
except Exception as e:
    print(f"WARN allow-list unavailable ({str(e)[:80]}) -> no whitelist filter this run", flush=True)

rows = d1_query(
    "SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
    "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0 "
    "AND webp_prefix LIKE 'book/%' AND pan_dir_id IS NOT NULL"
)
pages = []
for r in rows:
    bid, pc, pdid = r.get("book_id"), int(r.get("page_count") or 0), r.get("pan_dir_id")
    if not (bid and pc and pdid):
        continue
    if allow is not None and reqof(bid) not in allow:
        continue
    pages += [(bid, n, pdid) for n in range(1, pc + 1)]
pages.sort()
mine = [p for i, p in enumerate(pages) if i % TOTAL == SHARD]
_pilot = os.environ.get("PILOT", "").strip()
if _pilot:
    mine = mine[:int(_pilot)]   # small first-batch trial before full run
print(f"shard {SHARD}/{TOTAL} key#{SHARD % len(KEYS)} pages {len(mine)}/{len(pages)} pilot={_pilot or 'no'}", flush=True)

lock = threading.Lock(); cnt = {"done": 0, "skip": 0, "err": 0, "rej": 0}

LEDGER = "ledger.json"
ledger = set()
if os.path.exists(LEDGER):
    try:
        ledger = set(json.load(open(LEDGER, encoding="utf-8")))
    except Exception:
        ledger = set()
print(f"ledger已有 {len(ledger)} 条记录", flush=True)


def work(item):
    bid, p, pdid = item
    pstr = str(p).zfill(4)
    txtkey = f"_ocr/{bid}/page_{pstr}.txt"
    with lock:
        if txtkey in ledger:
            cnt["skip"] += 1
            return
    try:
        b = fetch_page_from_123(pdid, pstr)
        if not b:
            with lock: cnt["err"] += 1
            return
        txt = ocr_page(base64.b64encode(b).decode())
        if txt is None:
            with lock: cnt["err"] += 1
            return
        # OCR 质量闸:讯飞 LLM 视觉 OCR 在空白/密排页会幻觉出短语刷屏/整行复读/乱码。
        # reject 的不进 _ocr/ 燃料池,改落 _ocr_rejected/ 标记(供换引擎退回重跑),记账避免每轮重烧。
        if ocr_quality.verdict(txt) == "reject":
            rejkey = f"_ocr_rejected/{bid}/page_{pstr}.txt"
            try:
                s3.put_object(Bucket=BUCKET, Key=rejkey, Body=txt.encode("utf-8"))
            except Exception:
                pass
            with lock:
                ledger.add(txtkey); cnt["rej"] += 1
            return
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        with lock:
            ledger.add(txtkey)
            cnt["done"] += 1
            if cnt["done"] % 20 == 0:
                print(f"done {cnt['done']} / mine {len(mine)}", flush=True)
    except Exception as e:
        with lock: cnt["err"] += 1
        print("ERR", bid, p, str(e)[:50], flush=True)


with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, mine))

json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocrxf_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine),
                               "ocrd": cnt["skip"] + cnt["done"], "new": cnt["done"],
                               "err": cnt["err"], "rej": cnt["rej"]}).encode())
print(f"=== shard {SHARD} XF-OCR {cnt['done']} new, {cnt['skip']+cnt['done']}/{len(mine)} done, "
      f"err {cnt['err']}, 质量闸拦截 {cnt['rej']} ===", flush=True)
