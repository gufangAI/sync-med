# India / Ayurveda-Unani-Siddha traditional-medicine text corpus ingest from Internet Archive.
# Fetches each item's OCR full text ({id}_djvu.txt) and uploads plain text to R2 (india/<id>.txt).
# Text pattern (like ctext_harvest): light, idempotent (head_object skip), sharded, per-run capped,
# cron-resumable. GitHub runners reach archive.org directly (no proxy). IA politeness: >=1s sleep per call.
# Worklist = scripts/worklist_india.txt (one IA identifier per line, ASCII). AI fuel corpus for the
# East-Asian + South-Asian herbal-medicine expansion (founder 2026-07-10: "collect all, others don't have it").
import os, io, json, time, sys
import urllib.request, urllib.error
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
BUCKET = os.environ.get("S_BUCKET", "")
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
CAP = int(os.environ.get("CAP", "400"))          # max items fetched per run (keeps a shard under the Actions time budget)
IA_SLEEP = float(os.environ.get("IA_SLEEP", "1.2"))
MIN_LEN = int(os.environ.get("MIN_LEN", "500"))  # skip near-empty OCR (image-only scans)
WL = os.environ.get("WORKLIST", os.path.join(os.path.dirname(os.path.abspath(__file__)), "worklist_india.txt"))
UA = "gufang-india-text-ingest/1.0 (contact: hosonzuo@gmail.com; educational/archival public-domain traditional-medicine corpus)"

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK,
                  region_name="auto", config=Config(connect_timeout=15, read_timeout=120, retries={"max_attempts": 3}))

def ia_get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    r = urllib.request.urlopen(req, timeout=timeout)
    data = r.read()
    time.sleep(IA_SLEEP)
    return r.status, data

def exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key); return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"): return False
        raise

def main():
    ids = [x.strip() for x in open(WL, encoding="utf-8").read().splitlines() if x.strip()]
    mine = [i for n, i in enumerate(ids) if n % TOTAL == SHARD]
    print(f"worklist={len(ids)} shard {SHARD}/{TOTAL} mine={len(mine)} cap={CAP}", flush=True)
    done = skip = empty = err = 0
    ledger = []
    for ident in mine:
        if done >= CAP:
            print(f"cap {CAP} reached, cron will resume rest", flush=True); break
        key = f"india/{ident}.txt"
        try:
            if exists(key):
                skip += 1; continue
        except Exception as e:
            print(f"ERR head {ident}: {str(e)[:60]}", flush=True); err += 1; continue
        url = f"https://archive.org/download/{ident}/{ident}_djvu.txt"
        try:
            status, data = ia_get(url)
            if status == 200 and len(data) >= MIN_LEN:
                s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="text/plain; charset=utf-8")
                done += 1
                if done % 25 == 0:
                    print(f"  progress up={done} skip={skip} empty={empty} err={err}", flush=True)
            else:
                empty += 1   # image-only or too short; no OCR text
        except urllib.error.HTTPError as e:
            if e.code == 404: empty += 1     # no djvu.txt = image-only item
            else: err += 1; print(f"ERR {ident} HTTP{e.code}", flush=True)
            time.sleep(IA_SLEEP)
        except Exception as e:
            err += 1; print(f"ERR {ident}: {str(e)[:60]}", flush=True); time.sleep(IA_SLEEP)
        ledger.append({"id": ident, "ok": exists(key)})
    lk = f"_ledger/india_shard_{SHARD}.json"
    try:
        s3.put_object(Bucket=BUCKET, Key=lk, Body=json.dumps({"shard": SHARD, "up": done, "skip": skip, "empty": empty, "err": err}, ensure_ascii=False).encode("utf-8"))
    except Exception: pass
    print(f"=== shard {SHARD} done: uploaded={done} skipped={skip} empty/imageonly={empty} err={err} -> {lk} ===", flush=True)

if __name__ == "__main__":
    main()
