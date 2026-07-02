# -*- coding: utf-8 -*-
# CC-down: cloud OCR worker - GitHub Actions + iFlytek MaaS HunyuanOCR (free vision OCR).
# Replaces the RapidOCR line: R2 med-book images (book/{id}/page_NNNN.webp) -> HunyuanOCR
# -> text -> R2 _ocr/{id}/page_NNNN.txt (SueAI fuel). Higher quality than RapidOCR, free.
# Each shard binds one account key from the pool (per-account concurrency ~20), threaded within.
import os, io, re, json, base64, threading, requests, boto3
from concurrent.futures import ThreadPoolExecutor

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


allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())
PAGES = json.loads(s3.get_object(Bucket=BUCKET, Key=os.environ.get("PAGES_KEY", "_cc/med_pages.json"))["Body"].read().decode("utf-8"))
imgs = []
for bid, pc in PAGES.items():
    if reqof(bid) not in allow or int(pc) <= 0:
        continue
    imgs += [f"book/{bid}/page_{n:04d}.webp" for n in range(1, int(pc) + 1)]
imgs.sort()
mine = [k for i, k in enumerate(imgs) if i % TOTAL == SHARD]
_pilot = os.environ.get("PILOT", "").strip()
if _pilot:
    mine = mine[:int(_pilot)]   # small first-batch trial before full run
print(f"shard {SHARD}/{TOTAL} key#{SHARD % len(KEYS)} imgs {len(mine)}/{len(imgs)} pilot={_pilot or 'no'}", flush=True)

lock = threading.Lock(); cnt = {"done": 0, "skip": 0, "err": 0}


def work(k):
    txtkey = "_ocr/" + k[len("book/"):].rsplit(".", 1)[0] + ".txt"
    try:
        s3.head_object(Bucket=BUCKET, Key=txtkey)
        with lock: cnt["skip"] += 1
        return
    except Exception:
        pass
    try:
        b = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        txt = ocr_page(base64.b64encode(b).decode())
        if txt is None:
            with lock: cnt["err"] += 1
            return
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        with lock:
            cnt["done"] += 1
            if cnt["done"] % 20 == 0:
                print(f"done {cnt['done']} / mine {len(mine)}", flush=True)
    except Exception as e:
        with lock: cnt["err"] += 1
        print("ERR", k, str(e)[:50], flush=True)


with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, mine))

s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocrxf_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine),
                               "ocrd": cnt["skip"] + cnt["done"], "new": cnt["done"], "err": cnt["err"]}).encode())
print(f"=== shard {SHARD} XF-OCR {cnt['done']} new, {cnt['skip']+cnt['done']}/{len(mine)} done, err {cnt['err']} ===", flush=True)
