# coding: utf-8
"""Book-level degeneracy criterion for OCR text -- one definition, three callers.

Not to be confused with ocr_quality.py, which judges a single page at write time
(repeat n-grams, garbage characters, per-page ratios) and is imported by the OCR
writers themselves. This module judges a whole book after the fact, from four
corpus statistics, and answers exactly one question: has the text collapsed?

Why it exists: that same four-measure check had been written three times --
ocr_quality_gate.py, release_passed_ocr.py, diagnose_bad_ocr.py -- and the three
copies drifted to three different sets of thresholds. The same book could be
rejected by one script and passed by the next, which is worse than any single
threshold being wrong: nobody could say what the pipeline's criterion actually
was. The numbers now live here and nowhere else, so changing them is one edit.

What this check can and cannot say -- written here rather than in one caller's
docstring, because it is the part that keeps getting forgotten:

    it CAN say a book is broken   -- collapse is visible in the text's own statistics
    it CANNOT say a book is right -- that needs a parallel edition or a human

Hence the label released books carry is `degeneracy-pass`, never `verified`.
"""
import re
from collections import Counter

import cjk_charset

# \u6c49\u5b57\u5224\u5b9a\u30022026-07-28 \u4ece\u81ea\u5e26\u7684\u4e00\u6bb5 [\u4e00-\u9fff] \u6362\u6210\u5171\u4eab\u8868 cjk_charset.HAN
# (\u8868\u610f + \u90e8\u9996,14 \u6bb5;\u3010\u523b\u610f\u4e0d\u542b\u5047\u540d\u3011,\u7406\u7531\u89c1\u4e0b)\u3002
#
# \u6362\u4e4b\u524d\u8fd9\u91cc\u8fde\u6269\u5c55A \u90fd\u4e0d\u8ba4,\u540e\u679c\u662f\u4ece"\u4e22\u4e00\u9875"\u5347\u7ea7\u6210"\u4e22\u4e00\u6574\u672c":
# \u4e00\u672c 700 \u5b57\u6b63\u6587 + 300 \u5b57\u751f\u50fb\u5b57\u7684\u4e66,cjk \u53ea\u6709 0.700,\u4f4e\u4e8e CJK_MIN 0.80,
# \u300c\u975e\u6c49\u5b57\u8fc7\u591a\u300d\u6574\u672c\u5224\u6b7b\u3002\u800c\u751f\u50fb\u5b57\u6bb5\u662f\u6269\u5c55A / \u6269\u5c55B / \u517c\u5bb9\u8868\u610f / \u5eb7\u7199\u90e8\u9996
# \u54ea\u4e00\u7c7b\u90fd\u4e00\u6837 \u2014\u2014 \u5224\u6b7b\u7684\u4e0d\u662f\u5b57,\u662f"\u8fd9\u4e2a\u7801\u4f4d\u4e0d\u5728\u8868\u91cc"\u8fd9\u4e00\u4ef6\u4e8b,
# \u548c\u9875\u7ea7 2026-07-28 \u4fee\u7684\u662f\u540c\u4e00\u4e2a bug,\u53ea\u662f\u4ee3\u4ef7\u5927\u4e00\u6863\u3002
#
# \u2605 \u5047\u540d\u5fc5\u987b\u7559\u5728\u8868\u5916,\u8fd9\u662f\u627f\u91cd\u7684,\u4e0d\u662f\u53e3\u5473:CJK_MIN \u4e0b\u65b9\u90a3\u53e5
#   "catches pages that came back entirely in kana" \u5c31\u662f\u5b83\u7684\u7acb\u8eab\u4e4b\u672c\u3002
#   \u6536\u8fdb\u5047\u540d,\u6574\u9875\u5047\u540d\u7684 cjk \u7acb\u523b\u53d8\u6210 1.00,\u8fd9\u6761\u5224\u636e\u5f53\u573a\u5931\u660e \u2014\u2014
#   \u800c\u5b83\u6b63\u662f\u704c\u6ee1\u65e7 tcm-rag-768 \u7d22\u5f15\u7684\u4e24\u79cd\u5931\u8d25\u6a21\u5f0f\u4e4b\u4e00\u3002\u6240\u4ee5\u672c\u6a21\u5757\u53d6 HAN,
#   \u9875\u7ea7 ocr_quality \u53d6 CJK_TEXT(\u542b\u5047\u540d,\u5426\u5219\u548c\u523b\u672c\u6f22\u65b9\u4e66\u4f1a\u88ab\u6574\u9875\u5224\u6210\u4e71\u7801)\u3002
#   \u4e24\u8fb9\u9009\u4e86\u4e0d\u540c\u7684 pattern,\u4f46\u9009\u7684\u662f\u540c\u4e00\u5f20\u533a\u5757\u8868,\u8fd9\u6b63\u662f\u6536\u655b\u8981\u7684\u6837\u5b50\u3002
#
# \u2605 \u4e66\u7ea7\u5dee\u5206(\u6539\u524d\u7a84\u8868 / \u6539\u540e\u5bbd\u8868,\u56db\u4e2a\u6570\u9010\u4e2a,\u771f\u8dd1 profile() \u5f97\u5230):
#   \u4ed3\u91cc 8 \u6761\u65e2\u6709\u4e66\u7ea7\u8bed\u6599 + \u7eaf BMP \u6b63\u6587:\u56db\u4e2a\u6570\u3010\u4e00\u4f4d\u4e0d\u52a8\u3011,\u5224\u51b3\u96f6\u6539\u52a8\u3002
#   \u7f3a\u53e3\u672c\u4f53(\u6539\u524d\u8bef\u6740 -> \u6539\u540e\u653e\u884c):
#       \u6b63\u6587700+\u6269\u5c55B300     cjk 0.700->1.000  top1 .0429->.0300  distinct 150.0->165.0  rep 2->2
#       \u6b63\u6587\u4e0e\u6269\u5c55B\u9010\u5b57\u76f8\u95f4   cjk 0.500->1.000  top1 .0420->.0210  distinct 210.0->165.0  rep 2->1
#       \u6b63\u6587700+\u517c\u5bb9\u8868\u610f300   cjk 0.700->1.000  top1 .0429->.0300  distinct 150.0->145.0  rep 2->2
#       \u6b63\u6587700+\u6269\u5c55A300      cjk 0.700->1.000  top1 .0429->.0300  distinct 150.0->305.0  rep 2->2
#       \u5eb7\u7199\u90e8\u9996\u7d22\u5f15\u6574\u672c      \u6539\u524d chars \u4e0d\u8db3 200 -> profile()=None(\u6574\u672c\u3010\u8fde\u5224\u90fd\u6ca1\u5224\u3011,
#                            \u5728 ocr_quality_gate \u7684\u62a5\u8868\u91cc\u76f4\u63a5\u6d88\u5931);\u6539\u540e cjk=1.000 \u653e\u884c
#   \u4e0a\u4e00\u8f6e\u70b9\u540d\u8b66\u544a\u7684 rep/distinct \u7ea0\u7f20,\u91cf\u51fa\u6765\u662f\u8fd9\u6837(\u65b9\u5411\u786e\u5b9e\u4e0d\u5355\u8c03,\u4f46\u90fd\u662f\u7ea0\u9519):
#       \u4e00X\u4e00X\u4e00X(X=\u6269\u5c55B) \u7a84\u8868\u628a\u5339\u914d\u5230\u7684\u5b57\u3010\u62fc\u8d77\u6765\u3011,\u4e00X\u4e00X \u62fc\u6210 \u4e00\u4e00\u4e00 \u2014\u2014
#           \u6539\u524d rep=400 / distinct=2.5 / top1=1.000,\u4e09\u4e2a\u6570\u3010\u5168\u662f\u634f\u9020\u7684\u3011;
#           \u6539\u540e rep=1 / distinct=63.8 / top1=0.500,\u53ea\u5269\u300c\u5355\u5b57\u9738\u5c4f\u300d\u4e00\u6761,\u800c\u90a3\u6761\u662f\u771f\u7684\u3002
#   \u2605 \u552f\u4e00\u7684"\u53d8\u4e25"\u65b9\u5411,480 \u7ec4\u5bf9\u6297\u626b\u63cf(\u6b63\u6587\u957f x \u751f\u50fb\u6bb5\u957f x \u82b1\u6837\u6570 x \u56db\u7c7b\u751f\u50fb\u5b57)\u91cf\u5230\u5e95:
#       40 \u7ec4\u6539\u524d\u653e\u884c\u3001\u6539\u540e\u5224\u6b7b,\u3010\u5168\u90e8 40 \u7ec4\u7684\u82b1\u6837\u6570\u90fd\u662f 1\u3011,\u5373\u67d0\u4e2a\u751f\u50fb\u5b57\u80cc\u9760\u80cc\u8fde\u51fa
#       50~5000 \u6b21\u3002\u7a84\u8868\u6839\u672c\u5339\u914d\u4e0d\u5230\u5b83\u3001\u5b83\u538b\u6839\u6ca1\u8fdb s,\u4e8e\u662f longest_run \u770b\u4e0d\u89c1 \u2014\u2014
#       \u8fd9\u4e0d\u662f\u65b0\u589e\u8bef\u6740,\u662f\u5224\u636e\u539f\u6765\u5bf9\u8fd9\u7c7b\u5783\u573e\u662f\u3010\u778e\u7684\u3011\u3002\u5b83\u548c\u65e2\u6709\u8bed\u6599
#       collapsed_char_run_81.txt \u662f\u540c\u4e00\u79cd\u5783\u573e,\u53ea\u662f\u6362\u4e86\u4e2a\u65e7\u8868\u4e0d\u8ba4\u8bc6\u7684\u5b57\u3002
#       \u88ab distinct(\u552f\u4e00\u65b9\u5411\u4e0d\u5b9a\u7684\u90a3\u4e2a\u6570)\u65b0\u5224\u6b7b\u7684:0 \u7ec4\u3002\u6539\u524d None \u6539\u540e\u5224\u6b7b\u7684:0 \u7ec4\u3002
CJK = cjk_charset.HAN


# --------------------------------------------------------------------------
# Thresholds. Every number below is the one release_passed_ocr.py arrived at on
# 2026-07-28 (commit e09dd5f), after its first pass rejected books that were
# plainly fine. ocr_quality_gate.py and diagnose_bad_ocr.py carried older,
# stricter numbers and now read these instead.
#
# Read the provenance note on each before touching it. A threshold with no
# evidence behind it is how the first version rejected a canonical commentary.
# --------------------------------------------------------------------------

MIN_CHARS = 200
# Below 200 CJK characters the statistics are noise: a 60-character colophon can
# show any distinct-per-1000 figure at all. Such a text gets no verdict rather
# than a bad one -- profile() returns None and the caller decides.

CJK_MIN = 0.80
# Minimum fraction of non-whitespace characters that are CJK. Catches the two
# failure modes that filled the old tcm-rag-768 index: pages that came back
# entirely in kana, and pages of digits from plate/index scans.

TOP1_ABS_MAX = 0.15
TOP1_BASELINE_MULT = 4.0
# Share of the text taken by its single most frequent character. Proofread
# classical Chinese sits near 0.05, so 4x the measured baseline is already far
# outside normal; the absolute 0.15 is the backstop for when the baseline itself
# is unavailable. This is what catches a page returned as 700 repetitions of one
# character.

DISTINCT_ABS_MIN = 30.0
DISTINCT_BASELINE_MULT = 0.30
# Distinct characters per 1000 characters.
#
# THIS IS THE NUMBER THAT GETS SET WRONG. Do not raise it because 30 "looks low".
#
# The floor used to be 60, and 60 was a guess -- it was nobody's measurement, and
# it outvoted the baseline-derived figure of 34. It threw out 01-0022912, Cheng
# Wuji's annotated Shanghan Lun, one of the canonical commentaries of the
# tradition, opening with a perfectly clean title line, on the grounds that it
# had 51 distinct characters per thousand.
#
# Classical Chinese concentrates its vocabulary: particles, drug names and
# formula names recur constantly, so 50-60 per thousand is ordinary for a real
# text and is not evidence of anything. The proofread corpus averages 97, and
# genuine collapse in this corpus ran to 21. The floor is 30 and the
# baseline-relative arm is 0.30 x baseline (~29 against a 97 baseline), so the
# absolute floor is what normally binds.
#
# Rejecting a canonical commentary is worse than admitting a mediocre scan: the
# entire reason for owning these books is that nobody else has them. Raise this
# only with a specific book in hand that the higher value catches and this one
# misses.

REP_MAX = 30
# Longest run of a single character repeated back to back.
#
# Was 12, and 12 caught real books: a dense pharmacopoeia legitimately prints
# long runs of the same character down a dosage column. Observed collapse in this
# corpus ran to 70+ (one sampled page was 81 identical characters), so 30 sits in
# the empty gap between the two populations rather than inside the good one.

FALLBACK_BASELINE = {"cjk": 0.95, "top1": 0.05, "distinct": 97.0}
# Used only when the proofread corpus cannot be read at all.
#
# Only one entry here has teeth. `cjk` is never read by verdict() -- CJK_MIN is
# absolute -- and `top1` can only loosen the ceiling, because top1_ceiling() is
# max(0.15, top1 x 4). `distinct` is the one the relative arm tightens with:
# distinct_floor() is max(30, distinct x 0.30). So this dict does not decide how
# conservative the fallback looks. It decides HOW STRICT THE CHECK BECOMES WHEN
# THE BASELINE GOES MISSING, and that is a much easier thing to get wrong.
#
# It was 300, and 300 x 0.30 = 90 -- three times the floor of 30, and far above
# the 51 distinct-per-1000 of Cheng Wuji's annotated Shanghan Lun. Any baseline
# outage would therefore have rejected that book again, by the same arithmetic
# that the floor of 60 was corrected for in e09dd5f. The fallback path was
# quietly keeping the exact bug the floor had just been fixed for, and the only
# thing standing between the pipeline and a repeat was the baseline never
# failing to load. It does fail: title_key() returning empty for a batch whose
# titles carry no Han characters is enough, as is one unreadable clean-text key.
#
# 97 is not a replacement guess. It is the measured average of the proofread
# corpus, the same figure cited under DISTINCT_ABS_MIN above. 97 x 0.30 = 29.1,
# which max() discards in favour of the 30 floor -- and that is the point: with
# no baseline there is nothing to tighten with, so the check falls back to
# exactly the absolute floors and no further. Losing the baseline must never
# silently make the pipeline stricter than the numbers anyone has evidence for.
#
# Callers must still report when they fall back to this. Not because the verdict
# gets harsher -- it no longer does -- but because a proofread corpus that
# cannot be read is a fault in its own right, and the pass/fail counts alone
# will not show it.

BASELINE_KEYS = ("cjk", "top1", "distinct")


def longest_run(s):
    """Length of the longest run of one character repeated back to back."""
    rep = run = 1
    for i in range(1, len(s)):
        run = run + 1 if s[i] == s[i - 1] else 1
        rep = max(rep, run)
    return rep


def profile(t):
    """The four measures, plus size, for one text. None if the text is too short
    to say anything about (see MIN_CHARS).

    Computed over CJK characters only: punctuation and layout differ legitimately
    between editions and are not errors.
    """
    s = "".join(CJK.findall(t or ""))
    if len(s) < MIN_CHARS:
        return None
    c = Counter(s)
    return {"cjk": len(s) / max(1, len(re.sub(r"\s", "", t))),
            "top1": c.most_common(1)[0][1] / len(s),
            "distinct": len(c) / (len(s) / 1000.0),
            "rep": longest_run(s),
            "chars": len(s)}


def baseline_of(profiles):
    """Average the profiles of text somebody else proofread, so the relative arm
    of each threshold comes from our own good corpus rather than from a guess.
    Falls back to FALLBACK_BASELINE when there is nothing to average.
    """
    ps = [p for p in (profiles or []) if p]
    if not ps:
        return dict(FALLBACK_BASELINE)
    return {k: sum(p[k] for p in ps) / len(ps) for k in BASELINE_KEYS}


def distinct_floor(bl=None):
    """Distinct-per-1000 below which a book is called collapsed."""
    if not bl:
        return DISTINCT_ABS_MIN
    return max(DISTINCT_ABS_MIN, bl["distinct"] * DISTINCT_BASELINE_MULT)


def top1_ceiling(bl=None):
    """Most-frequent-character share above which a book is called collapsed."""
    if not bl:
        return TOP1_ABS_MAX
    return max(TOP1_ABS_MAX, bl["top1"] * TOP1_BASELINE_MULT)


def verdict(p, bl=None):
    """Reasons this book is rejected. An empty list means nothing broken was
    found -- which is not the same as "accurate", and the label reflects that.

    `bl` is a baseline from baseline_of(); omit it to use the absolute floors
    only, which is what a caller without access to the proofread corpus does.
    """
    why = []
    if p["cjk"] < CJK_MIN:
        why.append("非汉字过多%.2f" % p["cjk"])          # non-Han too high
    if p["top1"] > top1_ceiling(bl):
        why.append("单字霸屏%.3f" % p["top1"])               # one character dominates
    if p["distinct"] < distinct_floor(bl):
        why.append("字种过少%.0f" % p["distinct"])           # too few distinct characters
    if p["rep"] >= REP_MAX:
        why.append("连续重复%d" % p["rep"])                  # long repeated run
    return why
