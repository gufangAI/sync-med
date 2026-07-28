# coding: utf-8
# Why did 34% of our OCR fail the degeneracy check?
#
# The answer decides where to spend effort. Three causes need three different
# responses, and they are indistinguishable from the summary statistics alone:
#
#   the book is not Chinese prose  -> kana-heavy Japanese kampo, or plate pages
#                                     and colophons that are mostly image.
#                                     Nothing is broken; route these differently.
#   the scan is poor               -> re-scan or re-OCR at higher settings.
#   the engine mishandled it       -> dense multi-column layouts, marginalia.
#                                     Fix parameters, not hardware.
#
# Only the second and third are helped by more OCR throughput. If most failures
# are the first, then tripling capacity triples the production of text nobody
# can use -- which is why this runs before any decision about adding accounts.
#
# Reads the actual text of failed books and classifies by what is in it.
import io
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
CLEAN = "clean_text/"
MANIFEST_KEY = CLEAN + "_manifest.json"
WORKERS = int(os.environ.get("WORKERS", "12"))
SHOW = int(os.environ.get("SHOW", "8"))

CJK = re.compile(r"[一-鿿]")
KANA = re.compile(r"[぀-ゟ゠-ヿ]")          # hiragana + katakana
LATIN = re.compile(r"[A-Za-z]")
DIGIT = re.compile(r"[0-9０-９]")


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


def classify(t):
    body = re.sub(r"\s", "", t or "")
    n = max(1, len(body))
    cjk = len(CJK.findall(body)) / n
    kana = len(KANA.findall(body)) / n
    latin = len(LATIN.findall(body)) / n
    digit = len(DIGIT.findall(body)) / n

    s = "".join(CJK.findall(body))
    rep = run = 1
    for i in range(1, len(s)):
        run = run + 1 if s[i] == s[i - 1] else 1
        rep = max(rep, run)
    distinct = (len(set(s)) / (len(s) / 1000.0)) if len(s) > 200 else 0.0

    # Order matters: the most actionable explanation wins.
    if kana > 0.08:
        kind = "日文汉方(含假名)"
    elif latin + digit > 0.25:
        kind = "拉丁/数字为主(疑图版·目录·索引)"
    elif rep >= 20:
        kind = "识别崩溃(整段重复)"
    elif distinct and distinct < 60:
        kind = "字种极少(疑空白页·牌记)"
    elif cjk < 0.80:
        kind = "杂符号多(疑版式噪声)"
    else:
        kind = "其它"
    return {"kind": kind, "cjk": cjk, "kana": kana, "latin": latin,
            "digit": digit, "rep": rep, "distinct": distinct,
            "head": re.sub(r"\s+", " ", (t or "").strip())[:60]}


def main():
    s3 = s3_client()
    m = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    held = m.get("_held_pending_qc") or []
    bad = [b for b in held if b.get("source") == "ocr" and b.get("_qc_fail")]
    print("暂缓区判为退化的 OCR 书:%d 本,逐本读真实内容分类\n" % len(bad), flush=True)
    if not bad:
        print("没有待诊断的书", flush=True)
        return

    def one(b):
        t = get_text(s3, "%socr/%s.txt" % (CLEAN, b["key"]))
        return (b, classify(t)) if t else None

    rows = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(one, bad):
            if r:
                rows.append(r)

    kinds = Counter(c["kind"] for _, c in rows)
    print("   ══ 失败原因分布 ══", flush=True)
    for k, v in kinds.most_common():
        print("   %-28s %3d 本 (%.0f%%)" % (k, v, 100.0 * v / len(rows)), flush=True)

    print("\n   ══ 每类抽样 ══", flush=True)
    seen = Counter()
    for b, c in sorted(rows, key=lambda x: x[1]["kind"]):
        if seen[c["kind"]] >= 2:
            continue
        seen[c["kind"]] += 1
        print("   [%s] %s" % (c["kind"], b["key"]), flush=True)
        print("      汉字%.2f 假名%.2f 拉丁%.2f 数字%.2f 连重%d 千字异字%.0f"
              % (c["cjk"], c["kana"], c["latin"], c["digit"], c["rep"], c["distinct"]),
              flush=True)
        print("      开头: %s" % c["head"], flush=True)

    # What to do about it -- the point of the exercise.
    jp = kinds.get("日文汉方(含假名)", 0)
    plate = kinds.get("拉丁/数字为主(疑图版·目录·索引)", 0) + kinds.get("字种极少(疑空白页·牌记)", 0)
    engine = kinds.get("识别崩溃(整段重复)", 0) + kinds.get("杂符号多(疑版式噪声)", 0)
    print("\n   ══ 结论 ══", flush=True)
    print("   书本身不是汉文正文(日文/图版/牌记): %d 本 (%.0f%%) —— 加机器无用,应分流"
          % (jp + plate, 100.0 * (jp + plate) / len(rows)), flush=True)
    print("   识别质量问题(崩溃/版式噪声):       %d 本 (%.0f%%) —— 调参或重 OCR 才有用"
          % (engine, 100.0 * engine / len(rows)), flush=True)
    if jp + plate > engine:
        print("   → 主因是【书的性质】不是【算力不足】。扩 OCR 产能不会改善这批。", flush=True)
    else:
        print("   → 主因是【识别质量】。值得调参重跑,扩产能也才有意义。", flush=True)


main()
