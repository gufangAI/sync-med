# -*- coding: utf-8 -*-
"""守卫：仓里不许再出现第二份 CJK 区间字面量。

立此因（2026-08-28）：
    cjk_charset.py 的文件头写着「**唯独不许再写一行字面量区间。本模块存在的
    全部理由就是不再有第二份字面量。**」—— 这条规矩只写在 docstring 里，
    **没有任何东西在执行它**。2026-07-28 收敛了五份副本；四个月后实测，
    仓里仍有 8 处未收敛的区间字面量分布在 6 个文件。

    规矩不执行就是没有规矩。这个脚本把那句话变成 CI。

为什么这件事值得一个守卫（不是洁癖）：
    窄表把它不认识的真汉字【逐个当成乱码】。实测（独立判据 = unicodedata
    认作 CJK 表意/部首的 98,227 个码位）：
        单段 [㐀-鿿]  认得 27,584 = 28.1%
        权威表 HAN    认得 98,175 = 99.9%
    align_full.py 那一份的实际后果：锚点从「连续 ≥6 个 CJK」里切，区外汉字
    先被静默删掉 → 含它的串断成两截、或掉到 6 字门槛以下产不出锚点。
    而扩展 B 与兼容表意字正是明代木刻本满篇都是的那类字。

误报是这类守卫的头号死因：
    guard_no_list.yml 自己的注释记着教训 ——「CI 连续 10+ 次红灯 = 闸门形同
    虚设，新的真违规 push 进来反而没人看」。所以本检测叠了三层收紧，
    每一层都是被实测的误报逼出来的（12 → 10 → 9 → 8，最终零误报）：
        ① 只认真实 CJK 区块        排掉 emoji 区间 \\U0001F000-\\U0001FAFF
        ② 必须在正则字符类内，或整行是裸区间赋值   排掉散文里的连字符
        ③ 该行必须确实在写正则                    排掉 f"[情报雷达-竞品监管]"

放行方式（与 ZERO-LIST-OK 同一套路）：
    确有理由保留的，在**同一行**加  # CJK-RANGE-OK: <理由>
    标注本身就是审计线索 —— review 时一眼看得到有几处例外、各自理由。

实现说明：
    不用正则去匹配 `\\uXXXX` 这种反斜杠形态（那要写四层反斜杠，极易写错，
    我在这上面连栽五次）。改成**先把该行的转义解码成真字符**再匹配，
    整类转义问题一次消掉。
"""
import sys
import io
import os
import re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 真实 CJK 码位区块，与 cjk_charset.py 同源口径：部首 + 表意 + 兼容 + 扩展 B..G
CJK_CLASS = (
    "[⺀-⻿⼀-⿟㐀-䶿一-鿿豈-﫿"
    "\U00020000-\U0002FA1F\U00030000-\U0003134F]"
)
RANGE = re.compile(CJK_CLASS + r"\s*-\s*" + CJK_CLASS)
ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}")

EXEMPT_FILES = {"./cjk_charset.py"}          # 权威表本身当然要写字面量
EXEMPT_MARK = "CJK-RANGE-OK"


def decode_escapes(line):
    """\\uXXXX / \\UXXXXXXXX -> 真字符。只用于判定，不用于报位置。"""
    def sub(m):
        try:
            return chr(int(m.group(0)[2:], 16))
        except (ValueError, OverflowError):
            return m.group(0)
    return ESCAPE.sub(sub, line)


def scan(rootdir="."):
    hits, exempted = [], []
    for root, dirs, files in os.walk(rootdir):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "node_modules", "__pycache__", ".venv")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn).replace(os.sep, "/")
            if p in EXEMPT_FILES or p.startswith("./tests/"):
                continue
            if os.path.basename(p) == "guard_cjk_literal.py":
                continue                       # 守卫自己要写区块表
            try:
                lines = open(p, encoding="utf-8", errors="ignore").read().split("\n")
            except OSError:
                continue
            for i, raw in enumerate(lines, 1):
                if '"' not in raw and "'" not in raw:
                    continue                   # 区间只可能在字符串/正则里
                ln = decode_escapes(raw)
                m = RANGE.search(ln)
                if not m:
                    continue
                a, b = m.span()
                before, after = ln[:a], ln[b:]
                # 裸区间赋值 —— 形如 HAN = '一-鿿'，整行就是一个区间常量。
                # 这一支**不要求**同行出现 re.：区间常量通常在别处被拼进正则。
                # （第一版把规则③套在它头上，漏掉了 formula_mine.py:87 这个真违规。）
                bare = re.match(
                    r"^\s*[A-Za-z_]\w*\s*=\s*r?['\"]" + re.escape(m.group(0))
                    + r"['\"]\s*(#.*)?$", ln)
                # 正则字符类内 —— 这一支才需要规则③，否则散文里的真方括号
                # （如 f"[情报雷达-竞品监管]"）会误报。
                in_class = (
                    (("re." in ln) or re.search(r"r['\"]", ln) is not None)
                    and before.rfind("[") > before.rfind("]") and "]" in after)
                if not (in_class or bare):
                    continue
                rec = (p, i, m.group(0), raw.strip()[:72])
                (exempted if EXEMPT_MARK in raw else hits).append(rec)
    return hits, exempted


BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "guard_cjk_literal.baseline")


def load_baseline():
    """{路径: 允许的处数}。文件不存在 = 零容忍（新仓的正确默认）。"""
    out = {}
    if not os.path.exists(BASELINE):
        return out
    for ln in open(BASELINE, encoding="utf-8"):
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        path, _, n = ln.rpartition("	")
        if path:
            out[path.strip()] = int(n)
    return out


def main():
    """棘轮：以现存违规为基线，**只拦新增**，且基线只许缩不许涨。

    为什么不是一上来就全拦：现存 8 处分布在 6 个文件，每一处要判 HAN 还是
    CJK_TEXT（假名收不收进来是承重的语义选择），不是机械替换。一次性拦死
    只会让 CI 长红 —— 而长红的闸门等于没有闸门，这正是 guard_no_list.yml
    注释里记的教训。棘轮让存量可以慢慢收，同时新增当场被拦。

    收敛掉一处之后，把 baseline 里对应的数字改小即可；改大会被下面这条拦住。
    """
    hits, exempted = scan()
    if exempted:
        print("已标注放行（可审计，%d 处）：" % len(exempted))
        for p, i, _g, ln in exempted:
            print("  %s:%d  %s" % (p[2:], i, ln))
        print()
    base = load_baseline()
    cur = {}
    for h in hits:
        cur[h[0]] = cur.get(h[0], 0) + 1

    over, newfile, shrunk = [], [], []
    for path, n in sorted(cur.items()):
        allowed = base.get(path)
        if allowed is None:
            newfile.append((path, n))
        elif n > allowed:
            over.append((path, allowed, n))
    for path, allowed in sorted(base.items()):
        n = cur.get(path, 0)
        if n < allowed:
            shrunk.append((path, allowed, n))

    print("存量 %d 处 / %d 个文件（基线共 %d 处）："
          % (len(hits), len(cur), sum(base.values())))
    for p, i, g, ln in hits:
        print("  %s:%d  %s   %s" % (p[2:], i, repr(g), ln))
    print()

    if shrunk:
        print("有文件收敛了，请把 baseline 调小（棘轮只许缩）：")
        for path, a, n in shrunk:
            print("  %s  %d -> %d" % (path[2:], a, n))
        print()

    if not over and not newfile:
        print("OK: 没有**新增**的 CJK 区间字面量。存量按基线放行，"
              "收一处就把 baseline 调小一处。")
        return 0

    for path, n in newfile:
        print("::error file=%s::新文件出现 CJK 区间字面量 %d 处" % (path[2:], n))
    for path, a, n in over:
        print("::error file=%s::CJK 区间字面量从基线 %d 涨到 %d" % (path[2:], a, n))
    print()
    print("::error::CJK 区间字面量必须收敛到 cjk_charset.py —— "
          "窄表把不认识的真汉字逐个当成乱码（单段表实测只认 28.1% 的汉字）。"
          "改用 char_class(HAN_BLOCKS) / HAN / CJK_TEXT；"
          "确有理由保留的，在该行加 '# CJK-RANGE-OK: <理由>' 并接受 review。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
