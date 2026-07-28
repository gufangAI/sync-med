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

# CJK unified ideographs, U+4E00-U+9FFF. Same range the three callers each had
# as a literal character class; written as escapes here so the range is legible
# as a range and the file carries no corpus text of its own.
CJK = re.compile(r"[\u4e00-\u9fff]")


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

FALLBACK_BASELINE = {"cjk": 0.95, "top1": 0.05, "distinct": 300.0}
# Used only when the proofread corpus cannot be read at all. Deliberately
# conservative, and note the consequence: distinct 300 x 0.30 = 90, far above the
# 30 floor, so a baseline outage makes the check much stricter than intended and
# would reject the Shanghan Lun again. Callers should report when they fall back
# to this rather than let it pass silently.

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
