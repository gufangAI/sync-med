# coding: utf-8
# Move OCR books that clear the degeneracy check back into the ingest list.
#
# 200 bridged books are parked in _held_pending_qc because no proofread edition
# of them exists to compare against. Sampling showed 23% are broken -- pages of
# 81 repeated characters, pages more kana than Chinese, books averaging 21
# distinct characters per thousand where the proofread corpus averages 97.
#
# Parking is the right call for those. It is the wrong call for the other 77%,
# whose statistics sit inside the proofread corpus's own envelope. Holding good
# text hostage to unverifiable neighbours costs us the exclusive material that
# is the entire point of doing our own OCR.
#
# So each book is measured individually and moved on its own merits. What this
# check can and cannot say is recorded with the result, because the distinction
# matters and will be forgotten otherwise:
#
#   it CAN say a book is broken -- collapse is visible in the text's own statistics
#   it CANNOT say a book is accurate -- that needs a parallel edition or a human
#
# So released books carry `qc: "degeneracy-pass"`, not `qc: "verified"`. When a
# real accuracy check exists (ctext cross-reference, human sampling), it upgrades
# the label. Nothing is deleted: failures stay in the held list with their reason.
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

# The check itself lives in ocr_degeneracy.py. It used to live here, and two
# other scripts each kept their own copy of it with different thresholds; the
# numbers below were the corrected ones, so they became the shared ones.
import ocr_degeneracy as deg

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
CLEAN = "clean_text/"
MANIFEST_KEY = CLEAN + "_manifest.json"
TRUTH_SOURCES = ("daizhige", "wiki", "wiki_flat", "kanripo", "siku")

WORKERS = int(os.environ.get("WORKERS", "12"))
DRY = os.environ.get("DRY_RUN", "") == "1"


def s3_client():
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto", config=Config(retries={"max_attempts": 5}))


def get_text(s3, key):
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception:
        return None


# profile() and verdict() moved to ocr_degeneracy.py on 2026-07-28, unchanged.
# They were copied into ocr_quality_gate.py and diagnose_bad_ocr.py, the copies
# drifted apart, and the corrected thresholds here never reached the other two.
# The thresholds and the evidence behind each of them are now in that module.
profile = deg.profile
verdict = deg.verdict


def main():
    s3 = s3_client()
    m = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = m.get("books") or []
    held = m.get("_held_pending_qc") or []
    targets = [b for b in held if b.get("source") == "ocr"]
    print("在册 %d 本 | 暂缓 %d 本(其中 OCR %d 本)" % (len(books), len(held), len(targets)),
          flush=True)
    if not targets:
        print("暂缓区没有待放行的 OCR 书", flush=True)
        return

    # Baseline from text somebody else proofread -- the thresholds come from our
    # own good corpus rather than from a guess.
    base = []
    for b in [x for x in books if x.get("source") in TRUTH_SOURCES][:25]:
        p = profile(get_text(s3, "%s%s/%s.txt" % (CLEAN, b["source"], b["key"])) or "")
        if p:
            base.append(p)
    bl = deg.baseline_of(base)
    if not base:
        # Say so out loud: the fallback baseline is far stricter than the measured
        # one (distinct 300 x 0.30 = 90 against a real floor of 30), so a silent
        # baseline outage looks exactly like a batch of suddenly-bad books.
        print("WARN 取不到已校对基线,改用保守默认值 —— 本轮判定会明显偏严,"
              "大批判退时先怀疑基线而不是书", flush=True)
    print("基线(%d 本已校对): CJK %.3f | 最高频 %.4f | 千字异字 %.1f"
          % (len(base), bl["cjk"], bl["top1"], bl["distinct"]), flush=True)

    def judge(b):
        p = profile(get_text(s3, "%socr/%s.txt" % (CLEAN, b["key"])) or "")
        if not p:
            return b, ["文本过短或取不到"]
        return b, verdict(p, bl)

    passed, failed = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for b, why in ex.map(judge, targets):
            if why:
                b = dict(b, _qc_fail="/".join(why))
                failed.append(b)
            else:
                # Not "verified" -- only "nothing broken found". The distinction
                # is the whole reason this label is spelled out.
                b = dict(b, qc="degeneracy-pass", qc_date=time.strftime("%Y-%m-%d"))
                passed.append(b)

    print("\n通过退化检测 %d 本 | 判为退化 %d 本 (%.0f%%)"
          % (len(passed), len(failed), 100.0 * len(failed) / max(1, len(targets))), flush=True)
    for b in failed[:12]:
        print("   ✗ %s  %s" % (b["key"], b["_qc_fail"]), flush=True)

    if DRY:
        print("[dry] 不写入", flush=True)
        return
    if not passed:
        print("没有可放行的书", flush=True)
        return

    other_held = [b for b in held if b.get("source") != "ocr"]
    m["books"] = books + passed
    m["count"] = len(m["books"])
    m["_held_pending_qc"] = other_held + failed
    m["_qc_note"] = (
        "qc=degeneracy-pass 的含义:该书未检出崩溃型退化(整页重复字/假名页/字种过少),"
        "统计特征落在已校对语料的正常区间内。**这不等于'已验证准确'** —— "
        "退化检测能证伪、不能证真;要给出准确率百分比,需 ctext 等外部校勘源比对或人工抽样。"
        "有了真准确率后把标签升级为 qc=verified。"
        "_held_pending_qc 里带 _qc_fail 的是判为退化的,原文一律保留,重 OCR 后可重验。")
    m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    s3.put_object(Bucket=BUCKET, Key=MANIFEST_KEY,
                  Body=json.dumps(m, ensure_ascii=False, indent=1).encode("utf-8"),
                  ContentType="application/json; charset=utf-8")
    print("已放行 %d 本 -> 在册 %d 本;暂缓区留 %d 本(附失败原因)"
          % (len(passed), len(m["books"]), len(m["_held_pending_qc"])), flush=True)


main()
