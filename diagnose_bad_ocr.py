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
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import boto3
import requests
from botocore.config import Config

# Shared with ocr_quality_gate.py and release_passed_ocr.py. This file used to
# carry its own copy of the collapse thresholds (repeat-run 20, distinct-per-1000
# 60), so a book could be filed here under a cause it was never rejected for.
import ocr_degeneracy as deg

# 2026-07-28:门槛早就共享了,字符表却还各留一份 —— 于是"用同一套门槛"这句话是假的,
# 因为两边喂给门槛的四个数是拿不同的字算出来的。字符表也收敛掉。
import cjk_charset

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
CLEAN = "clean_text/"
MANIFEST_KEY = CLEAN + "_manifest.json"
WORKERS = int(os.environ.get("WORKERS", "12"))
CF_ACC = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB = os.environ.get("D1_DATABASE_ID", "")
D1_TOK = os.environ.get("D1_API_TOKEN", "")


def d1_query(sql, params=None):
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query"
           % (CF_ACC, D1_DB))
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError("D1 failed: " + str(j.get("errors"))[:200])
    return (j.get("result") or [{}])[0].get("results") or []
SHOW = int(os.environ.get("SHOW", "8"))

# 这两张表必须和 ocr_degeneracy.profile() 用的【完全一样】,否则本脚本会把书归到
# 一个它从来没因为那条判据被判退过的原因下 —— 而本脚本存在的全部意义就是
# "为什么这 34% 挂了",归错因等于把决策(要不要加 OCR 账号)建在错的分布上。
CJK = cjk_charset.HAN
# 假名表同样从共享表取,并且【顺带补齐了】:原来只有 BMP 的平/片假名两段,
# 和刻本大量用的変体仮名(U+1B000 起)一个都不认,于是最该被归进
# 「日文汉方(含假名)」的那一批和刻本,kana 占比量出来接近 0,归不进这一类。
KANA = cjk_charset.KANA
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
    rep = deg.longest_run(s)
    distinct = (len(set(s)) / (len(s) / 1000.0)) if len(s) > 200 else 0.0

    # Order matters: the most actionable explanation wins.
    #
    # The collapse thresholds are ocr_degeneracy.py's, not this file's. They have
    # to be the same numbers that rejected the book in the first place: a book
    # held for "字种过少" and then diagnosed here as "其它" tells whoever reads
    # the report that the pipeline is inconsistent, not what is wrong with the
    # book. No baseline is available here (this script never reads the proofread
    # corpus), so the absolute floors apply.
    if kana > 0.08:
        kind = "日文汉方(含假名)"
    elif latin + digit > 0.25:
        kind = "拉丁/数字为主(疑图版·目录·索引)"
    elif rep >= deg.REP_MAX:
        kind = "识别崩溃(整段重复)"
    elif distinct and distinct < deg.distinct_floor():
        kind = "字种极少(疑空白页·牌记)"
    elif cjk < deg.CJK_MIN:
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

    # Write the Japanese-language books into the ledger so OCR stops re-reading them.
    #
    # These are kampo works from the Mitsui collection: the text is largely kana
    # with Japanese kanbun reading marks (返り点, 送り仮名), which OCR transcribes
    # faithfully -- nothing is malfunctioning, the book simply is not Chinese prose.
    # Left alone, every future OCR round spends its pages on them again, and every
    # quality gate rejects them again, forever.
    #
    # There is no language column in books_assets_v2 and no reliable signal in the
    # title (譯文筌蹄初編 looks like any Chinese title), so the judgement has to come
    # from the text -- which means it has to be recorded once rather than recomputed.
    # ocr_jobs is where OCR already keeps its per-book state, so the exclusion lives
    # beside the work it excludes.
    jp_ids = [b["key"] for b, c in rows if c["kind"] == "日文汉方(含假名)"]
    if not jp_ids:
        return
    if os.environ.get("MARK_SKIP", "1") != "1":
        print("\n   (MARK_SKIP=0,只报告不写台账)", flush=True)
        return
    now = int(time.time())
    ok = 0
    for bid in jp_ids:
        try:
            d1_query(
                "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
                "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
                "created_at, started_at, finished_at, updated_at) "
                "VALUES (?, '_lang_skip', ?, 0, 'skip_japanese', 'ocr-diagnose', "
                "0, 0, 0, 0, 0, ?, ?, ?, ?, ?)",
                [bid, os.environ.get("GITHUB_RUN_ID", "local"),
                 "kana-heavy kampo, not Chinese prose", now, now, now, now])
            ok += 1
        except Exception as e:
            print("   WARN 标记失败 %s: %s" % (bid, str(e)[:100]), flush=True)
    print("\n   已把 %d 本日文汉方书写进台账(status=skip_japanese),"
          "OCR 选书时会跳过,不再重复消耗算力" % ok, flush=True)


main()
