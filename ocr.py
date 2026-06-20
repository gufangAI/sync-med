# -*- coding: utf-8 -*-
# CC-down: OCR worker - GitHub Actions + PaddleOCR (open-source CN OCR, free cloud concurrency).
# R2 med-book images (book/{id}/page_NNNN.webp) -> PaddleOCR -> text -> R2 _ocr/{id}/page_NNNN.txt (SueAI fuel).
# Runs on gufangAI enterprise runners, bypasses the China-only iFlytek API.
import os, io, re, boto3, numpy as np
from PIL import Image
from paddleocr import PaddleOCR

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")
ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())


def reqof(g):
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


imgs = []; tok = None
while True:
    kw = dict(Bucket=BUCKET, Prefix="book/", MaxKeys=1000)
    if tok:
        kw["ContinuationToken"] = tok
    r = s3.list_objects_v2(**kw)
    for o in r.get("Contents", []):
        k = o["Key"]
        if re.search(r"/page_\d+\.webp$", k) and reqof(k) in allow:
            imgs.append(k)
    if r.get("IsTruncated"):
        tok = r.get("NextContinuationToken")
    else:
        break
imgs.sort()
mine = [k for i, k in enumerate(imgs) if i % TOTAL == SHARD]
print(f"shard {SHARD}/{TOTAL} imgs {len(mine)}/{len(imgs)}", flush=True)
done = 0
for k in mine:
    txtkey = "_ocr/" + k[len("book/"):].rsplit(".", 1)[0] + ".txt"
    try:
        s3.head_object(Bucket=BUCKET, Key=txtkey); continue   # already OCR'd, skip (idempotent)
    except Exception:
        pass
    try:
        b = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        im = np.array(Image.open(io.BytesIO(b)).convert("RGB"))
        res = ocr.ocr(im, cls=True)
        txt = "\n".join(l[1][0] for pg in (res or []) if pg for l in pg)
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        done += 1
        if done % 20 == 0:
            print(f"done {done}/{len(mine)}", flush=True)
    except Exception as e:
        print("ERR", k, str(e)[:50], flush=True)
print(f"=== shard {SHARD} OCR {done} done ===", flush=True)
