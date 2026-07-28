# -*- coding: utf-8 -*-
"""CJK 字符表收敛的哨兵 —— 钉的不是"这次改对了",是"下次谁再复制一份会当场红"。

病历(为什么要有这份文件):同一张 CJK 表在仓里长到过【五份】互相独立的副本,
各自漂移,而且窄的那四份把它们不认识的真汉字逐个当成乱码:

    ocr_quality.py       21 段    页级判据          <- 2026-07-28 补齐了
    ocr_quality_gate.py   1 段    闸门(有牙)      <- 没补
    ocr_degeneracy.py     1 段    书级判据(丢整本)<- 没补
    diagnose_bad_ocr.py   1 段    失败原因分类      <- 没补
    compare_ocr(_v2).py   4 段    引擎对比报告      <- 没补

于是同一本古籍页级放行、书级判死。补齐其中一份【解决不了这个问题】——
上一轮就是只补了一份,五份变成"一宽四窄",差异反而更大。
所以本文件里最重要的一条是 TestNobodyCopiesTheTable:它逐文件扫全仓源码,
谁再写一行自己的 CJK 区间字面量,CI 立刻红在那一行上。

跑法与其余两套一致(CI 同一个 workflow 一起跑):
    python -m pytest tests/ -q
    python -m unittest discover -s tests -q
"""
import io
import os
import re
import sys
import unittest
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cjk_charset as c          # noqa: E402
import ocr_degeneracy as d       # noqa: E402
import ocr_quality as q          # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 全仓源码扫描:找"写死的 CJK 区间" ────────────────────────────────────────
# 只看源码文本,【不 import】—— 本仓一半的脚本 import 就会跑 main()、读环境变量、
# 连 R2(ocr_quality_gate.py 最后一行就是裸的 main())。哨兵必须能盯住这些文件,
# 所以判据落在源码字面上,不落在运行时对象上。
_TOK = r"(?:\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|[^\\\]\"'])"
_RANGE = re.compile(_TOK + r"-" + _TOK)
_CLASS = re.compile(r"\[([^\]\n]*)\]")
# 裸区间字符串(如 align_full.py 的 CJK = r"㐀-鿿"),它稍后被拼进 [...] 里用
_BARE = re.compile(r"""['"](\s*""" + _TOK + r"-" + _TOK + r"""\s*)['"]""")


def _cp(tok):
    return int(tok[2:], 16) if tok[:2] in ("\\u", "\\U") else ord(tok)


def _is_cjk(cp):
    return any(lo <= cp <= hi for lo, hi, _n in c.CJK_BLOCKS)


def _ranges_in(fragment):
    out = set()
    for m in _RANGE.finditer(fragment):
        g = re.match(r"(" + _TOK + r")-(" + _TOK + r")", m.group(0))
        if not g:
            continue
        try:
            lo, hi = _cp(g.group(1)), _cp(g.group(2))
        except (TypeError, ValueError):
            continue
        if lo < hi and _is_cjk(lo) and _is_cjk(hi):
            out.add("U+%04X-U+%04X" % (lo, hi))
    return out


def scan_repo():
    """{相对路径: {"U+XXXX-U+YYYY", ...}},只统计【代码】里的,# 注释里的不算。

    注释里留旧值是好事(ocr_degeneracy / ocr_quality_gate 的注释都写着当年那段
    U+4E00-U+9FFF 长什么样),那是病历不是副本;把注释也算进来会逼着人删掉病历。

    ★ 只豁免 # 开头的行,【docstring 不豁免】—— 本函数自己的 docstring 就踩过一次:
      原先这里照抄了一个可直接粘贴的字符类字面量,扫描当场把本文件报了出来。
      那不是误报:一个能被复制走的区间字面量,写在哪里都是下一份副本的种子。
      所以规矩是:散文里提区间一律写成 U+XXXX-U+YYYY 这种【粘不走】的形式。
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [x for x in dirnames if x not in (".git", "__pycache__", "node_modules")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            with io.open(path, encoding="utf-8") as f:
                text = f.read()
            hits = set()
            for line in text.split("\n"):
                if line.lstrip().startswith("#"):
                    continue
                for m in _CLASS.finditer(line):
                    hits |= _ranges_in(m.group(1))
                for m in _BARE.finditer(line):
                    hits |= _ranges_in(m.group(1))
            if hits:
                found[rel] = hits
    return found


# ── 允许自带区间的文件:每一条都必须写清【为什么它问的不是同一个问题】────────
# 这不是"豁免名单",是"把每一份幸存的副本变成一个写下过理由的决定"。
# 名单里连区间值都钉死 —— 改动 align_full 的范围同样会红。
ALLOWED = {
    "cjk_charset.py": (
        None,                       # None = 不校验具体区间:这里就是那份唯一的定义
        "唯一的定义所在地"),
    "align_full.py": (
        {"U+3400-U+9FFF"},
        "问的不是同一个问题:它是【跨版本对齐】的归一化(把 OCR 页锚到已校对全文上),"
        "不是质量判据。这里「放宽」不等于「更对」——已校对底本(殆知阁/维基)基本是 BMP,"
        "OCR 侧多留生僻字反而【降低】锚点命中率。而且它现在这一段是连续的 "
        "U+3400-U+9FFF,顺带含易经卦符 U+4DC0-4DFF,从区块表重拼会悄悄改掉行为。"
        "改它需要一轮拿 R2 真语料跑的对齐命中率差分,本轮没有那个数,不动。已呈报 CTO。"),
    "ctext_migrate_rename.py": (
        {"U+3400-U+4DBF", "U+4E00-U+9FFF"},
        "问的是【文件名首字是不是汉字】(命名规范识别),不是文本质量判定。"),
    "formula_mine.py": (
        {"U+4E00-U+9FFF"},
        "问的是【方剂名/别名长什么样】(命名实体抽取),方名用字是 BMP 常用汉字;"
        "放宽到扩展B 只会让噪声进方名表。与 OCR 质量判据无关。"),
    "scripts/intel_radar/daily_report_v3.py": (
        {"U+4E00-U+9FFF"},
        "情报雷达的中文分词切片,输入是现代中文网页不是古籍。"),
    "scripts/intel_radar/weekly_hunter_v1.py": (
        {"U+4E00-U+9FFF"},
        "同上。"),
    "scripts/pan_register.py": (
        {"U+4E00-U+9FFF"},
        "问的是【文件名前缀那一个汉字】(番号解析),不是文本判定。"),
}


class TestNobodyCopiesTheTable(unittest.TestCase):
    """★ 本文件的命门。上一轮"补齐一份表"没治本,治本的是这一条。"""

    def test_no_new_module_writes_its_own_cjk_range(self):
        found = scan_repo()
        unexpected = sorted(set(found) - set(ALLOWED))
        self.assertEqual(
            unexpected, [],
            "这些文件自己写了 CJK 区间字面量 —— 仓里又多出一份会独立漂移的副本。\n"
            "要么改成 import cjk_charset(HAN / CJK_TEXT / KANA / char_class),\n"
            "要么在本文件的 ALLOWED 里写清它问的为什么不是同一个问题。\n"
            "明细:%s" % {k: sorted(found[k]) for k in unexpected})

    def test_allowed_files_still_carry_exactly_the_ranges_they_were_allowed(self):
        """名单不是免死金牌:允许的是【那一段】,不是"这个文件随便写"。
        谁把 align_full 的范围偷偷改宽/改窄,这里照样红。"""
        found = scan_repo()
        for rel, (ranges, why) in sorted(ALLOWED.items()):
            if ranges is None:
                continue
            with self.subTest(file=rel):
                self.assertIn(rel, found, "%s 不再自带区间了 —— 好事,请从 ALLOWED 里删掉它" % rel)
                self.assertEqual(sorted(found[rel]), sorted(ranges),
                                 "%s 的区间变了(允许的理由是:%s)" % (rel, why))

    def test_the_five_drifted_copies_are_gone(self):
        """五份副本逐个点名钉死。只断言"总数为零"的话,某天有人把新副本
        顺手加进 ALLOWED,这五个文件回退了也没人知道。"""
        found = scan_repo()
        for rel in ("ocr_quality.py", "ocr_quality_gate.py", "ocr_degeneracy.py",
                    "diagnose_bad_ocr.py", "compare_ocr.py", "compare_ocr_v2.py"):
            with self.subTest(file=rel):
                self.assertNotIn(rel, found,
                                 "%s 又自带 CJK 区间了 —— 这正是本轮收敛掉的那五份副本之一" % rel)

    def test_every_ocr_module_binds_its_cjk_name_from_the_shared_module(self):
        """正面断言:这些文件里的 CJK/KANA/cjk_ratio 必须来自共享模块。
        走源码文本而不是 import —— ocr_quality_gate.py 最后一行是裸 main(),import 它
        会当场去读 CF/R2 环境变量;compare_ocr.py 没有 XF_KEYS 直接 SystemExit。"""
        want = {
            "ocr_quality.py": ("_CJK_RE = cjk_charset.CJK_TEXT",
                               "_CJK_BLOCKS = cjk_charset.CJK_BLOCKS"),
            "ocr_degeneracy.py": ("CJK = cjk_charset.HAN",),
            "ocr_quality_gate.py": ("CJK = cjk_charset.HAN",),
            "diagnose_bad_ocr.py": ("CJK = cjk_charset.HAN", "KANA = cjk_charset.KANA"),
            "compare_ocr.py": ("from ocr_quality import cjk_ratio",),
            "compare_ocr_v2.py": ("from ocr_quality import cjk_ratio",),
        }
        for rel, needles in sorted(want.items()):
            with io.open(os.path.join(REPO, rel), encoding="utf-8") as f:
                src = f.read()
            for needle in needles:
                with self.subTest(file=rel, binding=needle):
                    self.assertIn(needle, src,
                                  "%s 不再从共享模块取 CJK 判定了" % rel)


class TestOneTableThreeQuestions(unittest.TestCase):
    """一张表,三个 pattern。分歧是【设计】,必须钉住;分歧一旦消失反而是 bug。"""

    def test_han_excludes_kana_and_cjk_text_includes_it(self):
        """假名的去留是承重的:
        HAN 收进假名 -> ocr_degeneracy「整页假名=退化」失明(旧 tcm-rag-768 两种失败模式之一);
        CJK_TEXT 排除假名 -> ocr_quality 把和刻本漢方书满篇的假名计成乱码,整页判死。"""
        for ch in ("あ", "ア", "\U0001B002", "\U0001B132"):   # 平/片/変体/小假名
            with self.subTest(kana="U+%04X" % ord(ch)):
                self.assertTrue(c.KANA.match(ch))
                self.assertTrue(c.CJK_TEXT.match(ch), "页级表丢了假名,和刻本会被判成乱码")
                self.assertIsNone(c.HAN.match(ch), "书级表收了假名,CJK_MIN 当场失明")
        for ch in ("一", "㐀", "﨑", "⾗", "⺅", "\U00020B9F"):
            with self.subTest(han="U+%04X" % ord(ch)):
                self.assertTrue(c.HAN.match(ch))
                self.assertTrue(c.CJK_TEXT.match(ch))
                self.assertIsNone(c.KANA.match(ch))

    def test_block_groups_partition_the_merged_table(self):
        """三组区块必须【不重不漏】地拼成合并表,而且合并表升序不重叠。
        乱序/重叠不会报错,只会让这张表核不动 —— 而"核不动"正是当年
        一行字面量少画 17 段没人发现的原因。"""
        parts = c.RADICAL_BLOCKS + c.KANA_BLOCKS + c.IDEOGRAPH_BLOCKS
        self.assertEqual(c.CJK_BLOCKS, tuple(sorted(parts)))
        self.assertEqual(len(set(parts)), len(parts), "三组之间有重复条目")
        self.assertEqual(c.HAN_BLOCKS, tuple(sorted(c.RADICAL_BLOCKS + c.IDEOGRAPH_BLOCKS)))
        prev_hi = -1
        for lo, hi, name in c.CJK_BLOCKS:
            with self.subTest(block=name):
                self.assertLessEqual(lo, hi, "%s 的区间反了" % name)
                self.assertGreater(lo, prev_hi, "%s 与前一段重叠或乱序" % name)
            prev_hi = hi

    def test_patterns_are_built_from_the_table_not_pasted(self):
        """三个 pattern 必须是【从表拼出来的】。谁哪天为了"快一点"把 pattern 写成
        字面量,表就又成了一份没人读的摆设 —— 那正是本轮修的病。"""
        for pat, blocks in ((c.CJK_TEXT, c.CJK_BLOCKS), (c.HAN, c.HAN_BLOCKS),
                            (c.KANA, c.KANA_BLOCKS)):
            with self.subTest(pattern=pat.pattern[:24]):
                self.assertEqual(pat.pattern, "[" + c.char_class(blocks) + "]")

    def test_han_covers_every_han_codepoint_unicodedata(self):
        """HAN 必须盖住 unicodedata 认得的每一个汉字/部首,且【一个假名都不许有】。
        这是本文件唯一不靠人眼、不靠语料的判据 —— 与 test_ocr_quality.py 里
        针对 CJK_TEXT 的那一条同一个写法,换成汉字侧再钉一次。"""
        han_prefix = ("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH",
                      "KANGXI RADICAL", "CJK RADICAL")
        kana_prefix = ("HIRAGANA ", "KATAKANA ", "HENTAIGANA ")
        missed, leaked, seen = [], [], 0
        for cp in range(0x110000):
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            if name.startswith(han_prefix):
                seen += 1
                if not c.HAN.match(ch):
                    missed.append("U+%04X %s" % (cp, name))
            elif name.startswith(kana_prefix):
                if c.HAN.match(ch):
                    leaked.append("U+%04X %s" % (cp, name))
        self.assertGreater(seen, 90000, "扫到的汉字码点太少,扫描逻辑坏了")
        self.assertEqual(missed[:20], [],
                         "这些字 unicodedata 认作汉字,HAN 却不认(共 %d 个)—— "
                         "含它们的书会被「非汉字过多」整本判死" % len(missed))
        self.assertEqual(leaked[:20], [],
                         "HAN 里混进了 %d 个假名 —— 「整页假名=退化」会失明" % len(leaked))


class TestTheDefectAtBothLevels(unittest.TestCase):
    """同一批生僻字,页级和书级必须给出【同向】的判定。
    收敛之前它们给的是反的:页级 ok、书级整本判死,而两级用的是同一份文本。"""

    RARE = ("\U00020B9F", "﨑", "⾗", "⺅", "㐀")

    def test_a_rare_char_book_passes_both_the_page_and_the_book_judge(self):
        fix = os.path.join(REPO, "tests", "fixtures", "degeneracy",
                           "chengwuji_51_distinct_per_1000.txt")
        with io.open(fix, encoding="utf-8") as f:
            body = f.read().replace("\n", "")[:700]
        for probe in self.RARE:
            with self.subTest(cp="U+%04X" % ord(probe)):
                book = body + "".join(chr(ord(probe) + (i % 40)) for i in range(300))
                # 页级:一个乱码都不许有
                a = q.analyze(book)
                self.assertEqual(a["garbage_ratio"], 0.0,
                                 "页级把生僻字当乱码了;reasons=%s" % a["reasons"])
                # 书级:必须放行,且 cjk 必须满格
                p = d.profile(book)
                self.assertIsNotNone(p)
                self.assertGreaterEqual(p["cjk"], d.CJK_MIN,
                                        "书级 cjk=%.3f 掉到 CJK_MIN 以下" % p["cjk"])
                self.assertEqual(d.verdict(p), [],
                                 "书级把生僻字正货判死了;verdict=%s" % d.verdict(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
