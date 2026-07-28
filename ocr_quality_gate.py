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
    ocr_books = [b for b in books if b.get("source") == "ocr"]
    print("manifest: 可信版本 %d 本(%s) | OCR 本 %d 本"
          % (len(truth), "/".join(TRUTH_SOURCES), len(ocr_books)), flush=True)

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
        print("没有找到同名的可信版本 —— 无法用这条路验准确率,"
              "说明这批 OCR 的书目与已数字化部分不重叠(这本身是好事:是独家内容),"
              "需要另找判据(人工抽样 / ctext 外部比对)", flush=True)
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
