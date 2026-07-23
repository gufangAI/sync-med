# -*- coding: utf-8 -*-
# OCR 质量闸 / 幻觉检测(蓝图「数据飞轮」第一组件)。
# 纯规则、零第三方依赖(只用标准库),可被 ocr_xf.py / ocr.py / ocr_ndl.py 复用,
# 也可被独立审计器 ocr_quality_check.py 调用。GitHub Actions 云端跑,本机只发 HTTP,免费。
#
# 治什么:LLM 视觉 OCR(讯飞 HunyuanOCR = ocr_xf.py)在空白衬页/密排多栏/馆藏章页上会
#   "幻觉"出垃圾——同一短语刷屏(back-to-back 复读,如 A 后 A 后 A 后)、整行复读、
#   纯拉丁/数字乱码、替换符/控制符乱码。这些若静默 put 进 _ocr/ 会毒化 SueAI 燃料。
#   本模块给出 label(ok/suspect/reject/empty)+ reasons,调用方据此"标记/退回重跑"而非静默入库。
#
# 检测判据(全部语言无关的比例/重复数学,不含可读 CJK 字面量,符合 public 仓 opsec):
#   1) repeat_ngram   — 最高频 n-gram(n=2..8)占全文比例:短语刷屏的核心症状
#   2) max_run        — 某周期 p 的连续复读(back-to-back)最长游程占比:"A后A后A后"
#   3) line_dup       — 去空白后重复行占比:整行/整块复读
#   4) garbage_ratio  — 既非 CJK、非 CJK 标点、非 ASCII 可打印的"乱码"字符占比(含 U+FFFD/控制符)
#   5) single_char    — 单一最高频字符占比:单字符刷屏(。。。。 / oooo)
#   6) cjk_ratio      — CJK 占比(软信号:封面/牌记/西文页天然低,不单独判死)
#
# 说明:纯规则闸主打"便宜可靠"地拦下上面这些主流失败模式;真正的"跨书语义串味"
#   (内容是另一本书的正经文字)需语料统计或 LLM 复核,本模块诚实不声称覆盖,
#   但短语刷屏式的模板串味(同一段幻觉 boilerplate 反复出现)会被 1/2/3 命中。
import re
from collections import Counter

# CJK 统一表意 + 扩展A + 平/片假名(沿用 ocr_ndl.py 已实测标定的范围)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ゟ゠-ヿ]")
# CJK 常见标点 + 全角标点(算"正常"字符,不计乱码)
_CJK_PUNCT = set(
    "　、。，．；：？！“”‘’"
    "（）【】《》〈〉「」『』"
    "—…·～－：｜〆〇"
    "０１２３４５６７８９"
)
_ASCII_OK = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    " \t\r\n.,;:!?\"'()[]{}<>/\\-_=+*&%$#@|~`^"
)

# ---- 阈值(模块常量,便于调参)----
REPEAT_NGRAM_REJECT = 0.50   # 最高频 2..8-gram 占全文 >=50% -> 短语刷屏,判死
REPEAT_NGRAM_SUSPECT = 0.32
MAX_RUN_REJECT = 0.45        # 连续复读游程 >=45% -> 判死
MAX_RUN_SUSPECT = 0.28
LINE_DUP_REJECT = 0.60       # 重复行占比 >=60%(且行数够多)-> 判死
LINE_DUP_SUSPECT = 0.40
LINE_DUP_MIN_LINES = 5
GARBAGE_REJECT = 0.50        # 乱码字符 >=50%(且够长)-> 判死
GARBAGE_SUSPECT = 0.30
SINGLE_CHAR_REJECT = 0.60    # 单字符刷屏 >=60%(且够长)-> 判死
CJK_SUSPECT = 0.15           # 够长但 CJK 占比极低 -> 存疑(可能西文页,不判死)
MIN_LEN_FOR_RATIO = 20       # 太短的页不做乱码/单字符判死(避免误伤正常短牌记)
LONG_PAGE = 40


def _clean(text):
    return re.sub(r"\s", "", text or "")


def cjk_ratio(text):
    """CJK 占比。沿用 ocr_ndl.py 实测:垃圾幻觉块 CJK 占比恒为 0,真实古籍/漢方恒接近 1.0。"""
    t = _clean(text)
    if not t:
        return 0.0
    return len(_CJK_RE.findall(t)) / len(t)


def garbage_ratio(text):
    """既非 CJK、非 CJK 标点、非 ASCII 可打印的字符占比。U+FFFD/控制符计为乱码。"""
    t = _clean(text)
    if not t:
        return 0.0
    bad = 0
    for ch in t:
        if _CJK_RE.match(ch) or ch in _CJK_PUNCT or ch in _ASCII_OK:
            continue
        bad += 1
    return bad / len(t)


def single_char_ratio(text):
    """单一最高频字符占全文比例(单字符刷屏)。"""
    t = _clean(text)
    if not t:
        return 0.0
    return Counter(t).most_common(1)[0][1] / len(t)


def repeat_ngram(text):
    """n=2..8 中最高频 n-gram 覆盖全文的最大占比。返回 (ratio, n, unit_escaped)。"""
    t = _clean(text)
    n_total = len(t)
    if n_total < 6:
        return 0.0, 0, ""
    best, best_n, best_unit = 0.0, 0, ""
    for n in range(2, 9):
        if n_total < n * 3:
            break
        c = Counter(t[i:i + n] for i in range(0, n_total - n + 1))
        unit, cnt = c.most_common(1)[0]
        # 重叠计数会让 cnt*n 超过全长,封顶到 1.0 作"覆盖占比"(不影响 >=阈值 的判死)
        ratio = min(cnt * n / n_total, 1.0)
        if ratio > best:
            best, best_n, best_unit = ratio, n, unit
    # 转义,避免把原文 CJK 片段回吐进 public 仓的报告/日志
    return best, best_n, best_unit.encode("unicode_escape").decode("ascii")


def max_run_ratio(text):
    """最长"周期性连续复读"游程占全文比例。
    对周期 p=1..12,找 s[i]==s[i+p] 连续成立的最长段;段长(含首拍)/全长即游程占比。
    覆盖 back-to-back 复读(A后A后A后 = 周期3)与单字符刷屏(周期1)。"""
    t = _clean(text)
    L = len(t)
    if L < 6:
        return 0.0, 0
    best_run, best_p = 0, 0
    for p in range(1, min(12, L // 2) + 1):
        run = 0
        i = 0
        while i + p < L:
            if t[i] == t[i + p]:
                run += 1
                seg = run + p  # 首拍 p 个字符 + run 个复读字符
                if seg > best_run:
                    best_run, best_p = seg, p
            else:
                run = 0
            i += 1
    return min(best_run / L, 1.0), best_p


def line_dup_ratio(text):
    """去空白后,重复行占比 = 1 - 去重行数/总行数。返回 (ratio, n_lines)。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return 0.0, 0
    return 1.0 - len(set(lines)) / len(lines), len(lines)


def analyze(text):
    """对一页 OCR 文本打质量分。返回 dict:含各子分、label、reasons、score。
    label: ok / suspect / reject / empty。"""
    text = text or ""
    clean = _clean(text)
    n = len(clean)
    reasons = []

    if n == 0:
        return {"len": 0, "label": "empty", "score": 0, "reasons": ["empty"],
                "cjk_ratio": 0.0, "garbage_ratio": 0.0, "single_char": 0.0,
                "repeat_ngram": 0.0, "repeat_n": 0, "repeat_unit": "",
                "max_run": 0.0, "run_period": 0, "line_dup": 0.0, "n_lines": 0}

    cjk = cjk_ratio(text)
    garbage = garbage_ratio(text)
    single = single_char_ratio(text)
    rep, rep_n, rep_unit = repeat_ngram(text)
    run, run_p = max_run_ratio(text)
    ldup, nlines = line_dup_ratio(text)

    reject = False
    suspect = False

    if rep >= REPEAT_NGRAM_REJECT:
        reject = True; reasons.append(f"repeat_ngram={rep:.2f}(n={rep_n})")
    elif rep >= REPEAT_NGRAM_SUSPECT:
        suspect = True; reasons.append(f"repeat_ngram~{rep:.2f}")

    if run >= MAX_RUN_REJECT:
        reject = True; reasons.append(f"max_run={run:.2f}(p={run_p})")
    elif run >= MAX_RUN_SUSPECT:
        suspect = True; reasons.append(f"max_run~{run:.2f}")

    if nlines >= LINE_DUP_MIN_LINES and ldup >= LINE_DUP_REJECT:
        reject = True; reasons.append(f"line_dup={ldup:.2f}({nlines}L)")
    elif nlines >= LINE_DUP_MIN_LINES and ldup >= LINE_DUP_SUSPECT:
        suspect = True; reasons.append(f"line_dup~{ldup:.2f}")

    if n >= MIN_LEN_FOR_RATIO and garbage >= GARBAGE_REJECT:
        reject = True; reasons.append(f"garbage={garbage:.2f}")
    elif n >= MIN_LEN_FOR_RATIO and garbage >= GARBAGE_SUSPECT:
        suspect = True; reasons.append(f"garbage~{garbage:.2f}")

    if n >= MIN_LEN_FOR_RATIO and single >= SINGLE_CHAR_REJECT:
        reject = True; reasons.append(f"single_char={single:.2f}")

    if n >= LONG_PAGE and cjk < CJK_SUSPECT and not reject:
        suspect = True; reasons.append(f"cjk_low={cjk:.2f}")

    label = "reject" if reject else ("suspect" if suspect else "ok")
    # 分数:reject 一律低分;suspect 中档;ok 高分。给个连续分便于排序/看板。
    penalty = int(min(100, rep * 60 + run * 60 + ldup * 40 + garbage * 60 + max(0, single - 0.3) * 40))
    score = 100 - penalty
    if label == "reject":
        score = min(score, 25)
    elif label == "suspect":
        score = min(max(score, 30), 69)
    else:
        score = max(score, 70)

    return {"len": n, "label": label, "score": score, "reasons": reasons or ["clean"],
            "cjk_ratio": round(cjk, 3), "garbage_ratio": round(garbage, 3),
            "single_char": round(single, 3), "repeat_ngram": round(rep, 3),
            "repeat_n": rep_n, "repeat_unit": rep_unit,
            "max_run": round(run, 3), "run_period": run_p,
            "line_dup": round(ldup, 3), "n_lines": nlines}


def is_reject(text):
    """给产线做闸用的布尔:True = 幻觉/乱码,应退回重跑而非入库。empty 不算 reject。"""
    return analyze(text)["label"] == "reject"


def verdict(text):
    """返回 label 字符串,便于产线分流到 _ocr/ vs _ocr_rejected/。"""
    return analyze(text)["label"]
