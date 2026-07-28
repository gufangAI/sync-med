# coding: utf-8
# Hold the freshly bridged OCR books out of ingest until their accuracy is known.
#
# 200 books of our own OCR just entered the manifest. The comparison gate found
# zero of them share a title with the 1046 proofread editions we hold, so the
# one method that could have scored them does not apply: they are exactly the
# material nobody else has digitised, which is what makes them valuable and also
# what leaves them unverified.
#
# Ingest runs at :40 past every sixth hour and would embed them as they are. The
# precedent is not hypothetical -- tcm-rag-768 holds 1.828M vectors of degraded
# scans and returns zero usable hits on any query, because bad text does not sit
# quietly beside good text, it outranks it. Unverified is not a reason to
# discard, but it is a reason not to publish.
#
# So: park, do not delete. The text objects under clean_text/ocr/ are untouched
# and every held entry is written into the manifest under _held_pending_qc with
# its reason, so putting them back is a move between two lists rather than a
# re-run of the bridge.
import io
import json
import os
import sys
import time

import boto3
from botocore.config import Config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
MANIFEST_KEY = "clean_text/_manifest.json"
SOURCE = "ocr"
DRY = os.environ.get("DRY_RUN", "") == "1"


def s3_client():
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto", config=Config(retries={"max_attempts": 5}))


def main():
    s3 = s3_client()
    m = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = m.get("books") or []
    held_before = m.get("_held_pending_qc") or []

    keep = [b for b in books if b.get("source") != SOURCE]
    hold = [b for b in books if b.get("source") == SOURCE]
    print("manifest %d 本 -> 在册 %d 本,暂缓 %d 本(原已暂缓 %d)"
          % (len(books), len(keep), len(hold), len(held_before)), flush=True)

    if not hold:
        print("没有待暂缓的 OCR 条目", flush=True)
        return
    if DRY:
        print("[dry] 不写入", flush=True)
        return

    m["books"] = keep
    m["count"] = len(keep)
    m["_held_pending_qc"] = held_before + hold
    m["_held_reason"] = (
        "2026-07-28:ocr-to-rag 桥接进来的自家 OCR 书,暂缓入库等质量核验。"
        "原因:ocr-quality 比对发现这批与已校对的 1046 本(殆知阁/维基/Kanripo/四库)"
        "书目零重叠 —— 是独家内容,恰恰因此没有任何现成对照能证明它识别得对。"
        "而未经核验的 OCR 灌进去不是中性的:tcm-rag-768 那 182.8 万条退化扫描"
        "对任意查询恒返 0 条可用出处,坏文本会把好文本压下去。"
        "【零删除】clean_text/ocr/*.txt 原文一个没动,本条目录也原样保留;"
        "质量核验通过后从 _held_pending_qc 挪回 books 即可,不必重跑桥接。"
        "核验方式待定:同名对照走不通,改走退化检测+CJK占比+人工/AI 抽样判读。")
    m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    s3.put_object(Bucket=BUCKET, Key=MANIFEST_KEY,
                  Body=json.dumps(m, ensure_ascii=False, indent=1).encode("utf-8"),
                  ContentType="application/json; charset=utf-8")
    print("已暂缓 %d 本;manifest 在册回到 %d 本(原文与条目全部保留)"
          % (len(hold), len(keep)), flush=True)


main()
