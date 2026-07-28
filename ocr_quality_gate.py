# coding: utf-8
# Check OCR text against a known-good edition of the same work.
#
# The bridge just found 475 books of our own OCR output and is about to feed
# roughly 12 million characters into the RAG corpus. Nobody has checked whether
# those characters are right. That question is not academic: the old tcm-rag-768
# index holds 1.828 million vectors of degraded OCR -- pages that came back as
# 700 repetitions of 無, or entirely in Japanese kana -- and it scores zero
# usable hits on every query. Feeding in bad OCR does not merely add nothing, it
# outranks the good text and buries it.
#
# The founder's point: we already hold known-good editions. The manifest carries
# 788 books from daizhige, 194 from wikisource, 100 from kanripo -- all typed or
# proofread digital text, not OCR. Where a work exists on both sides, the
# digital edition is ground truth and our OCR can be scored against it directly.
#
# Scoring is character-bigram overlap (Dice) rather than edit distance:
#   * classical editions differ legitimately in punctuation, 異體字, and
#     line breaks, so an exact diff would flag correct OCR as wrong;
#   * OCR failure modes here are not subtle typos but collapse -- repeated
#     characters, kana, empty pages -- which destroy bigram overlap outright.
#
# Reported per book so a bad batch can be traced to its source, and one summary
# line so the number lands somewhere a person will see it.
import io
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import boto3
import requests
from botocore.config import Config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BUCKET = "guyaofang-lib"
CLEAN = "clean_text/"
MANIFEST_KEY = CLEAN + "_manifest.json"

CF_ACC = os.environ["CF_ACCOUNT_ID"]
D1_DB = os.environ["D1_DATABASE_ID"]
D1_TOK = os.environ["D1_API_TOKEN"]

SAMPLE = int(os.environ.get("SAMPLE", "40"))
WORKERS = int(os.environ.get("WORKERS", "8"))
# Below this the OCR of that work disagrees with the printed edition so much
# that it is not the same text any more.
BAD_BELOW = float(os.environ.get("BAD_BELOW", "0.45"))

TRUTH_SOURCES = ("daizhige", "wiki", "wiki_flat", "kanripo", "siku")


def s3_client():
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto", config=Config(retries={"max_attempts": 5}))


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


def get_text(s3, key):
    try:
        return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
    except Exception:
        return None


CJK = re.compile(r"[一-鿿]")


def norm(t):
    """CJK only: punctuation and layout differ between editions and are not errors."""
    return "".join(CJK.findall(t or ""))


def bigrams(s):
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def dice(a, b):
    if not a or not b:
        return 0.0
    ca, cb = bigrams(a), bigrams(b)
    inter = sum((ca & cb).values())
    return 2.0 * inter / (sum(ca.values()) + sum(cb.values()))


def degenerate(s):
    """The failure mode that filled the old index: a page that is one character
    repeated, or has almost no distinct characters at all."""
    if len(s) < 50:
        return True
    top = Counter(s).most_common(1)[0][1]
    return top / len(s) > 0.30 or len(set(s)) < 40


def title_key(t):
    """Match works across editions: strip volume/edition marks and punctuation."""
    t = re.sub(r"[（(【\[].*?[）)】\]]", "", str(t or ""))
    t = re.sub(r"(四庫全書本|四库全书本|卷[一二三四五六七八九十百零〇\d]+|"
               r"[上中下]冊|第[一二三四五六七八九十\d]+[册冊卷]|校注|注釋|注释|全本|影印本)", "", t)
    return "".join(CJK.findall(t))


def intrinsic(s3, ocr_books, truth):
    """Judge OCR without a parallel edition, by what broken OCR looks like.

    No answer key exists for these works -- that is what "exclusive" means. But
    the failures that ruined tcm-rag-768 were not subtle mistranscriptions, they
    were collapse: a page returning 700 repetitions of 無, a page entirely in
    kana, a page of digits. Those are visible in the text's own statistics, no
    comparison required.

    Four measures, and one of them is a genuine comparison after all: the same
    statistics computed over the proofread daizhige/wiki corpus give a baseline
    for what correct classical Chinese looks like here. A book far outside that
    envelope is suspect even though its own text was never proofread.

      cjk       fraction of characters that are CJK -- catches kana and digit pages
      top1      share taken by the single most frequent character -- catches 無無無
      distinct  distinct characters per 1000 -- catches low-entropy collapse
      rep       longest run of one repeated character

    This cannot prove a book is accurate. It can prove a book is broken, which
    is the decision actually in front of us: what to keep out of the index.
    """
    import random
    random.seed(20260728)

    def profile(t):
        s = norm(t)
        if len(s) < 200:
            return None
        c = Counter(s)
        top1 = c.most_common(1)[0][1] / len(s)
        distinct = len(c) / (len(s) / 1000.0)
        rep = 1
        run = 1
        for i in range(1, len(s)):
            run = run + 1 if s[i] == s[i - 1] else 1
            rep = max(rep, run)
        return {"cjk": len(s) / max(1, len(re.sub(r"\s", "", t))),
                "top1": top1, "distinct": distinct, "rep": rep, "chars": len(s)}

    # Baseline from text that was proofread by someone else.
    base = []
    for b in list(truth.values())[:25]:
        t = get_text(s3, "%s%s/%s.txt" % (CLEAN, b["source"], b["key"]))
        p = profile(t or "")
        if p:
            base.append(p)
    if base:
        bl = {k: sum(p[k] for p in base) / len(base) for k in ("cjk", "top1", "distinct")}
        print("\n   已校对语料基线(%d 本): CJK占比 %.3f | 最高频字占比 %.4f | 每千字不同字 %.1f"
              % (len(base), bl["cjk"], bl["top1"], bl["distinct"]), flush=True)
    else:
        bl = {"cjk": 0.95, "top1": 0.05, "distinct": 300.0}
        print("\n   取不到基线,用经验阈值", flush=True)

    sample = random.sample(ocr_books, min(SAMPLE, len(ocr_books)))
    rows = []
    def one(b):
        t = get_text(s3, "%socr/%s.txt" % (CLEAN, b["key"]))
        p = profile(t or "")
        return (b["key"], p) if p else None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(one, sample):
            if r:
                rows.append(r)

    bad = []
    print("\n   %-16s %7s %7s %9s %6s %s" % ("book_id", "CJK", "最高频", "千字异字", "连重", "判定"),
          flush=True)
    for key, p in sorted(rows, key=lambda x: x[1]["distinct"]):
        why = []
        if p["cjk"] < 0.80:
            why.append("非汉字过多")
        if p["top1"] > max(0.15, bl["top1"] * 4):
            why.append("单字霸屏")
        if p["distinct"] < max(60.0, bl["distinct"] * 0.35):
            why.append("字种过少")
        if p["rep"] >= 12:
            why.append("连续重复%d" % p["rep"])
        if why:
            bad.append(key)
        print("   %-16s %7.3f %7.4f %9.1f %6d %s"
              % (key, p["cjk"], p["top1"], p["distinct"], p["rep"],
                 "★ " + "/".join(why) if why else "正常"), flush=True)

    n = len(rows)
    print("\n   ══ 退化检测 %d 本 ══" % n, flush=True)
    print("   疑似退化: %d 本 (%.0f%%)" % (len(bad), 100.0 * len(bad) / max(1, n)), flush=True)
    print("   判读:%s" % (
        "未见崩溃型退化 —— 可以放行入库,准确率仍需 ctext 或人工抽样才能给出百分比"
        if not bad else
        "把上面标★的剔出后再入库" if len(bad) < n * 0.3 else
        "★ 退化比例过高,整批先别入库"), flush=True)


def main():
    s3 = s3_client()
    manifest = json.loads(s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read())
    books = manifest.get("books") or []

    truth = {}
    for b in books:
        if b.get("source") in TRUTH_SOURCES:
            k = title_key(b.get("book") or b.get("key"))
            if len(k) >= 3:
                truth.setdefault(k, b)
    # Read both lists. Books awaiting exactly this check were moved out of
    # `books` into `_held_pending_qc` so ingest would not embed them before they
    # were judged -- so the held list is where the subjects of this check
    # normally live. Reading only `books` reports "nothing to check" precisely
    # when there is the most to check, which is what the first run did.
    held = manifest.get("_held_pending_qc") or []
    in_use = [b for b in books if b.get("source") == "ocr"]
    parked = [b for b in held if b.get("source") == "ocr"]
    ocr_books = in_use + parked
    print("manifest: 可信版本 %d 本(%s) | 待验 OCR %d 本(在册 %d + 暂缓 %d)"
          % (len(truth), "/".join(TRUTH_SOURCES), len(ocr_books), len(in_use), len(parked)),
          flush=True)

    if not ocr_books:
        print("OCR 侧还没有条目 —— 先跑 ocr-to-rag 再验", flush=True)
        return

    # OCR entries key on book_id; get their titles so they can be matched by work.
    ids = [b["key"] for b in ocr_books]
    titles = {}
    for i in range(0, len(ids), 100):
        part = ids[i:i + 100]
        ph = ",".join("?" * len(part))
        for r in d1_query(
                "SELECT book_id, book_title FROM books_assets_v2 WHERE book_id IN (%s)" % ph,
                part):
            titles[str(r.get("book_id"))] = str(r.get("book_title") or "")

    pairs = []
    for b in ocr_books:
        k = title_key(titles.get(b["key"], ""))
        if len(k) >= 3 and k in truth:
            pairs.append((b, truth[k], k))
    print("同名可比对的:%d 本" % len(pairs), flush=True)
    if not pairs:
        print("没有同名的已校对版本可比 —— 这批是独家内容,恰恰因此没有现成答案可对。"
              "改走不依赖对照的判据:退化检测。", flush=True)
        intrinsic(s3, ocr_books, truth)
        return

    def score(item):
        ob, tb, k = item
        o = get_text(s3, "%socr/%s.txt" % (CLEAN, ob["key"]))
        t = get_text(s3, "%s%s/%s.txt" % (CLEAN, tb["source"], tb["key"]))
        if not o or not t:
            return None
        no, nt = norm(o), norm(t)
        # Compare on a common span; whole-book Dice punishes partial OCR unfairly.
        span = min(len(no), len(nt), 20000)
        return {"key": ob["key"], "work": k, "truth_source": tb["source"],
                "ocr_chars": len(no), "truth_chars": len(nt),
                "dice": round(dice(no[:span], nt[:span]), 4),
                "degenerate": degenerate(no[:5000])}

    res = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(score, pairs[:SAMPLE]):
            if r:
                res.append(r)

    if not res:
        print("取文本失败,无结果", flush=True)
        return

    res.sort(key=lambda x: x["dice"])
    print("\n   %-16s %-18s %8s %8s %7s %s" %
          ("book_id", "作品", "OCR字数", "对照字数", "Dice", "退化"), flush=True)
    for r in res:
        print("   %-16s %-18s %8d %8d %7.3f %s"
              % (r["key"], r["work"][:18], r["ocr_chars"], r["truth_chars"],
                 r["dice"], "★是" if r["degenerate"] else ""), flush=True)

    bad = [r for r in res if r["dice"] < BAD_BELOW or r["degenerate"]]
    avg = sum(r["dice"] for r in res) / len(res)
    print("\n   ══ 比对 %d 本 ══" % len(res), flush=True)
    print("   平均 Dice 相似度: %.3f" % avg, flush=True)
    print("   低于 %.2f 或退化的: %d 本 (%.0f%%)"
          % (BAD_BELOW, len(bad), 100.0 * len(bad) / len(res)), flush=True)
    print("   判读:%s" % (
        "OCR 与印本高度一致,可放心灌库" if avg >= 0.75 else
        "多数可用,但需把低分本剔出灌库" if avg >= 0.55 else
        "★ 整体质量不足 —— 灌进去会重蹈 tcm-rag-768 的覆辙,先别灌"), flush=True)


main()
