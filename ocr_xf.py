# -*- coding: utf-8 -*-
# CC-down: cloud OCR worker - GitHub Actions + iFlytek MaaS HunyuanOCR (free vision OCR).
# Replaces the RapidOCR line: R2 med-book images (book/{id}/page_NNNN.webp) -> HunyuanOCR
# -> text -> R2 _ocr/{id}/page_NNNN.txt (SueAI fuel). Higher quality than RapidOCR, free.
# Each shard binds one account key from the pool (per-account concurrency ~20), threaded within.
import os, io, re, json, base64, sys, threading, requests, boto3
from concurrent.futures import ThreadPoolExecutor
from botocore.exceptions import ClientError

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
XF_BASE = os.environ.get("XF_BASE", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_MODEL", "xophunyuanocr")
WORKERS = int(os.environ.get("WORKERS", "14"))
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


def _rebuild_pages_from_d1():
    # 同 sync.py 已验证的 2026-07-05 自愈模式: PAGES_KEY 缓存丢失时直接查 D1 现场重建。
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise SystemExit("PAGES_KEY 读不到 + 缺 CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN，无法从 D1 兜底")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    sql = ("SELECT book_id, page_count FROM books_assets_v2 "
           "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
    r = requests.post(url, headers={"Authorization": "Bearer " + tok}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise SystemExit(f"D1 查询失败: {str(j.get('errors', ''))[:200]}")
    rows = (j.get("result") or [{}])[0].get("results") or []
    return {row["book_id"]: int(row["page_count"]) for row in rows if row.get("book_id") and row.get("page_count")}


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


PAGES_KEY = os.environ.get("PAGES_KEY", "_cc/med_pages.json")
try:
    PAGES = json.loads(s3.get_object(Bucket=BUCKET, Key=PAGES_KEY)["Body"].read().decode("utf-8"))
except Exception as e:
    print(f"WARNING: PAGES_KEY {PAGES_KEY} unreadable ({e}) -> rebuilding from D1...", flush=True)
    PAGES = _rebuild_pages_from_d1()
    print(f"D1 rebuild ok: {len(PAGES)} books", flush=True)
    try:
        s3.put_object(Bucket=BUCKET, Key=PAGES_KEY, Body=json.dumps(PAGES, ensure_ascii=False).encode("utf-8"))
        print(f"cached back to R2: {PAGES_KEY}", flush=True)
    except Exception as e2:
        print(f"WARNING: cache-back failed ({e2}) -> next run will rebuild again, not fatal", flush=True)

# 同 ocr.py 2026-07-14 已定的做法: ALLOW_KEY 对象缺失时不当致命错误崩溃，退化为不设白名单限制
# (allow=None -> 处理 PAGES 清单里的全部书)，而不是重建/猜测一份内容再写回。
allow = None
try:
    allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())
    print(f"allow-list loaded: {len(allow)} reqs", flush=True)
except Exception as e:
    print(f"WARN allow-list unavailable ({str(e)[:80]}) -> no whitelist filter this run, processing all of PAGES manifest", flush=True)

imgs = []
for bid, pc in PAGES.items():
    if (allow is not None and reqof(bid) not in allow) or int(pc) <= 0:
        continue
    imgs += [f"book/{bid}/page_{n:04d}.webp" for n in range(1, int(pc) + 1)]
imgs.sort()
mine = [k for i, k in enumerate(imgs) if i % TOTAL == SHARD]
_pilot = os.environ.get("PILOT", "").strip()
if _pilot:
    mine = mine[:int(_pilot)]   # small first-batch trial before full run
print(f"shard {SHARD}/{TOTAL} key#{SHARD % len(KEYS)} imgs {len(mine)}/{len(imgs)} pilot={_pilot or 'no'}", flush=True)

lock = threading.Lock(); cnt = {"done": 0, "skip": 0, "err": 0}

# 2026-07-19修复:原每页一次s3.head_object()查重,改GitHub缓存本地ledger.json记账
# (同ocr_ndl.py/ocr.py方法),避免每次跑对全量候选重复敲R2。线程安全:复用现成lock。
LEDGER = "ledger.json"
ledger = set()
if os.path.exists(LEDGER):
    try:
        ledger = set(json.load(open(LEDGER, encoding="utf-8")))
    except Exception:
        ledger = set()
print(f"ledger已有 {len(ledger)} 条记录", flush=True)


def work(k):
    txtkey = "_ocr/" + k[len("book/"):].rsplit(".", 1)[0] + ".txt"
    with lock:
        if txtkey in ledger:
            cnt["skip"] += 1
            return
    try:
        b = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        txt = ocr_page(base64.b64encode(b).decode())
        if txt is None:
            with lock: cnt["err"] += 1
            return
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        with lock:
            ledger.add(txtkey)
            cnt["done"] += 1
            if cnt["done"] % 20 == 0:
                print(f"done {cnt['done']} / mine {len(mine)}", flush=True)
    except Exception as e:
        with lock: cnt["err"] += 1
        print("ERR", k, str(e)[:50], flush=True)


# 2026-07-22 灭雷:book/ 影像已于 2026-07-17 迁 123、R2 里已删空,直读全 NoSuchKey。
# 本 ocr_xf.py 无 cron 不自动跑,但一旦有人手动触发就会逐页 GET 全 404 烧 Class B
# (与 ocr.py/sync.py 同一类漏网)。进线程池前先 HEAD 探测本 shard 首个 key,R2 已空则整
# shard 跳过(同 ocr.py 已验证的止血法)。若要真正启用本讯飞高质量 OCR 线,改走 123 取图
# (参照 ocr_ndl.py 的 fetch_page_from_123),而非从 R2 直读。
if mine:
    try:
        s3.head_object(Bucket=BUCKET, Key=mine[0])
    except ClientError as _he:
        if _he.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            print(f"=== shard {SHARD} R2 book/ 已空(已迁123),整 shard 跳过,不烧 Class B ===", flush=True)
            json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
            sys.exit(0)
        raise

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, mine))

json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocrxf_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine),
                               "ocrd": cnt["skip"] + cnt["done"], "new": cnt["done"], "err": cnt["err"]}).encode())
print(f"=== shard {SHARD} XF-OCR {cnt['done']} new, {cnt['skip']+cnt['done']}/{len(mine)} done, err {cnt['err']} ===", flush=True)
