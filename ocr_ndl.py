# -*- coding: utf-8 -*-
# GitHub Actions + NDLOCR-Lite(国立国会図書館官方OCR,CC BY 4.0,CPU免GPU)
# 页图 <- 阅读器真实公开API(穿透123兜底,不直读R2——R2的book/前缀影像已于2026-07-17迁123,直读会全部NoSuchKey)
# 识别结果 -> R2 _ocr/{book_id}/page_NNNN.txt(与RapidOCR那条ocr.py同一落点,阅读器fulltext.js两边通吃)
import os, io, json, time, subprocess, sys, boto3, requests

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
PILOT = os.environ.get("PILOT", "").strip()
READER_BASE = os.environ.get("READER_BASE", "https://www.gufangai.com")

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")

# D1 里拉候选书目:已上线影像、非宮内庁(合规待批,先排除)、按book_id分片
def d1_query(sql):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1查询失败: {str(j.get('errors',''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []

rows = d1_query(
    "SELECT book_id, page_count FROM books_assets_v2 "
    "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0 "
    "AND webp_prefix LIKE 'book/%' AND book_title NOT LIKE '%宮內廳%'"
)
books = {r["book_id"]: int(r["page_count"]) for r in rows if r.get("book_id") and r.get("page_count")}
print(f"候选书目 {len(books)} 本(已排除宮内厅合规待批那批)", flush=True)

pages = []
for bid, pc in books.items():
    pages += [(bid, n) for n in range(1, pc + 1)]
pages.sort()
mine = [p for i, p in enumerate(pages) if i % TOTAL == SHARD]
if PILOT:
    mine = mine[:int(PILOT)]
print(f"shard {SHARD}/{TOTAL} 分到 {len(mine)}/{len(pages)} 页  pilot={PILOT or '无'}", flush=True)

OCR_SRC = "ndlocr-lite/src"
TMP = "/tmp/ndl_work"
os.makedirs(TMP, exist_ok=True)

done, skip, err = 0, 0, 0
for bid, p in mine:
    pstr = str(p).zfill(4)
    txtkey = f"_ocr/{bid}/page_{pstr}.txt"
    try:
        s3.head_object(Bucket=BUCKET, Key=txtkey)
        skip += 1
        continue
    except Exception:
        pass

    img_path = f"{TMP}/page_{pstr}.webp"
    try:
        resp = requests.get(f"{READER_BASE}/api/reader/{bid}/page", params={"p": p},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if resp.status_code != 200 or not resp.content:
            print(f"ERR拉图 {bid} p{p} status={resp.status_code}", flush=True)
            err += 1
            continue
        with open(img_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        print(f"ERR拉图异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        err += 1
        continue

    try:
        r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", img_path, "--output", TMP, "--json-only"],
                            cwd=OCR_SRC, capture_output=True, text=True, timeout=90)
        jf = f"{TMP}/page_{pstr}.json"
        if r.returncode != 0 or not os.path.exists(jf):
            print(f"ERR识别 {bid} p{p} :: {r.stderr[-150:]}", flush=True)
            err += 1
            continue
        data = json.load(open(jf, encoding="utf-8"))
        lines = [b.get("text") for pb in data.get("contents", []) for b in pb if b.get("text")]
        text = "\n".join(lines)
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
        done += 1
        if done % 20 == 0:
            print(f"进度 {done}/{len(mine)}", flush=True)
    except Exception as e:
        print(f"ERR处理异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        err += 1
    finally:
        for f in (img_path, f"{TMP}/page_{pstr}.json"):
            try:
                os.remove(f)
            except Exception:
                pass

s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocr_ndl_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine), "done": done, "skip": skip, "err": err}).encode())
print(f"=== shard {SHARD} 完成 done={done} skip={skip} err={err} / {len(mine)} ===", flush=True)
