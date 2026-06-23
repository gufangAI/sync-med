# -*- coding: utf-8 -*-
# CC-down: OCR worker - GitHub Actions + RapidOCR (onnxruntime PP-OCR, free cloud concurrency).
# R2 med-book images (book/{id}/page_NNNN.webp) -> RapidOCR -> text -> R2 _ocr/{id}/page_NNNN.txt (SueAI fuel).
# RapidOCR(onnxruntime) avoids paddle's AVX512 SIGILL on runners + ships models in the wheel (no baidu CDN).
import os, io, re, json, boto3, numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")
engine = RapidOCR()
allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())


def reqof(g):
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


# Zero-LIST: read the page-count manifest (id -> page_count) and build keys directly;
# never list_objects over the whole bucket (full bucket scans were the cost spike).
PAGES = json.loads(s3.get_object(Bucket=BUCKET, Key=os.environ.get("PAGES_KEY", "_cc/med_pages.json"))["Body"].read().decode("utf-8"))
imgs = []
for bid, pc in PAGES.items():
    if reqof(bid) not in allow or int(pc) <= 0:
        continue
    imgs += [f"book/{bid}/page_{n:04d}.webp" for n in range(1, int(pc) + 1)]
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
        im = np.array(Image.open(io.BytesIO(b)).convert("RGB"))[:, :, ::-1]  # PIL decodes webp->RGB; RapidOCR wants BGR (cv2)
        res, _ = engine(im)
        txt = "\n".join(l[1] for l in (res or []))   # res = [[box, text, score], ...]
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        done += 1
        if done % 20 == 0:
            print(f"done {done}/{len(mine)}", flush=True)
    except Exception as e:
        print("ERR", k, str(e)[:50], flush=True)
print(f"=== shard {SHARD} OCR {done} done ===", flush=True)
