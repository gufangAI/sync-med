# -*- coding: utf-8 -*-
# 老OCR层重刷·小批验证(平台CTO 2026-07-25,赛马冠军=讯飞HunyuanOCR+v2提示词98.2%真值)
# 只处理 2026-07-19 前写入的老层页(RapidOCR粗校,列序反/简化/馆章):
#   备份原文本 -> _ocr_legacy_backup/{bid}/page.txt(零删除) -> 讯飞v2重刷 -> 质量闸
#   -> 覆写 _ocr/(带 Metadata engine/pver 版本印,根治"分不清哪页哪引擎")
# 失败页保留老层不动;重刷过的页时间戳变新=天然幂等跳过;显示层fulltext.js按时间戳自动不再倒序。
# 默认30本小批(强制含创始人点名 zi021-0001-01),全量须创始人批准后另跑。
import os, re, json, base64, sys, time, threading, requests, boto3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ocr_quality

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
PAN_CID = os.environ["PAN_CLIENT_ID"]; PAN_SEC = os.environ["PAN_CLIENT_SECRET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
XF_BASE = os.environ.get("XF_BASE", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_MODEL", "xophunyuanocr")
WORKERS = int(os.environ.get("WORKERS", "8"))
N_BOOKS = int(os.environ.get("N_BOOKS", "30"))
CUTOFF = datetime(2026, 7, 19, tzinfo=timezone.utc)   # 此前写入=老层
PVER = "rtl_trad_v2"

PROMPT_V2 = ("这是竖排繁体古籍书页,阅读顺序:列从右到左,每列从上到下。"
             "严格按此顺序输出全部正文文字;保持繁体原字形,禁止转为简体;"
             "跳过藏书印、馆藏章、水印文字(如「国立公文書館」「National Archives of Japan」);"
             "只输出正文,不要任何解释。")

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")

def parse_keys(raw):
    raw = (raw or "").strip()
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [k.strip() for k in v if ":" in str(k)]
    except Exception:
        pass
    return [p.strip() for p in re.split(r"[\s,]+", raw) if ":" in p]

KEYS = parse_keys(os.environ.get("XF_KEYS", ""))
if not KEYS:
    raise SystemExit("no XF_KEYS")
KEY = KEYS[SHARD % len(KEYS)]
sess = threading.local()

def http():
    if not hasattr(sess, "s"):
        s = requests.Session(); s.trust_env = False
        sess.s = s
    return sess.s

def d1_query(sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 fail: {str(j.get('errors',''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []

PAN = "https://open-api.123pan.com"
_pan_lock = threading.Lock()
_tok = {"v": None}
_dir_cache = {}

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
        time.sleep(2 * (attempt + 1))
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
                    {"type": "text", "text": PROMPT_V2},
                    {"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b64}}]}]},
                timeout=120, verify=False)
            if r.status_code == 200:
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            pass
        time.sleep(3)
    return None

# ── 选书:强制含创始人点名素問,其余按book_id序补足N_BOOKS ──
MUST = ["zi021-0001-01"]
rows = d1_query(
    "SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
    "WHERE frontend_visible=1 AND upload_status='done' AND page_count>0 "
    "AND webp_prefix LIKE 'book/%' AND pan_dir_id IS NOT NULL ORDER BY book_id LIMIT ?",
    [N_BOOKS * 3])
books, seen = [], set()
for bid in MUST:
    r2 = d1_query("SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 WHERE book_id=? AND pan_dir_id IS NOT NULL", [bid])
    if r2:
        books.append(r2[0]); seen.add(bid)
for r in rows:
    if len(books) >= N_BOOKS:
        break
    if r["book_id"] not in seen:
        books.append(r); seen.add(r["book_id"])
print(f"重刷候选 {len(books)} 本(强制含 {MUST})", flush=True)

# ── 逐书筛老层页(head探测LastModified<CUTOFF;已重刷的时间戳新=自动跳过=幂等) ──
tasks = []
for r in books:
    bid, pc, pdid = r["book_id"], int(r["page_count"]), r["pan_dir_id"]
    for p in range(1, pc + 1):
        tasks.append((bid, p, pdid))
mine = [t for i, t in enumerate(tasks) if i % TOTAL == SHARD]
print(f"shard {SHARD}/{TOTAL} 分到 {len(mine)}/{len(tasks)} 页", flush=True)

lock = threading.Lock()
cnt = {"reflashed": 0, "kept_new": 0, "no_legacy": 0, "err": 0, "rej": 0, "backup": 0}

def work(item):
    bid, p, pdid = item
    pstr = str(p).zfill(4)
    key = f"_ocr/{bid}/page_{pstr}.txt"
    try:
        try:
            h = s3.head_object(Bucket=BUCKET, Key=key)
        except Exception:
            with lock: cnt["no_legacy"] += 1   # 无文字层页,小批不新增,只治存量
            return
        if h["LastModified"] >= CUTOFF:
            with lock: cnt["kept_new"] += 1    # 新层(NDL/讯飞v2)不碰
            return
        old = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        # 备份(零删除):保留原写入时间在metadata
        s3.put_object(Bucket=BUCKET, Key=f"_ocr_legacy_backup/{bid}/page_{pstr}.txt", Body=old,
                      ContentType="text/plain; charset=utf-8",
                      Metadata={"orig-modified": h["LastModified"].isoformat()})
        with lock: cnt["backup"] += 1
        img = fetch_page_from_123(pdid, pstr)
        if not img:
            with lock: cnt["err"] += 1
            return
        txt = ocr_page(base64.b64encode(img).decode())
        if txt is None:
            with lock: cnt["err"] += 1        # 讯飞失败=老层原样保留
            return
        if ocr_quality.verdict(txt) == "reject":
            with lock: cnt["rej"] += 1        # 质量闸拦=老层原样保留,不用垃圾换垃圾
            return
        s3.put_object(Bucket=BUCKET, Key=key, Body=txt.encode("utf-8"),
                      ContentType="text/plain; charset=utf-8",
                      Metadata={"engine": "hunyuanocr", "pver": PVER})
        with lock:
            cnt["reflashed"] += 1
            if cnt["reflashed"] % 25 == 0:
                print(f"reflashed {cnt['reflashed']}", flush=True)
    except Exception as e:
        with lock: cnt["err"] += 1
        print("ERR", bid, p, str(e)[:60], flush=True)

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, mine))

s3.put_object(Bucket=BUCKET, Key=f"_ledger/reflash_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, **cnt}).encode())
print(f"=== shard {SHARD} 重刷完成 {json.dumps(cnt, ensure_ascii=False)} ===", flush=True)
