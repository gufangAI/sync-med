# -*- coding: utf-8 -*-
"""ocr_degeneracy 判据回归 —— 书级判据,风险比页级只高不低。

为什么这套测试直到 2026-07-28 才有:ocr_quality 那边判错一页丢一页,这边判错一本
**丢一整本**。而本仓最贵的一次误杀恰恰出在这个模块 —— 成無己《註解傷寒論》
01-0022912,一部传统里的正典注本,开卷第一行干干净净,就因为「51 字种/千字」
被一个【猜出来的】字种下限 60 整本扔掉;紧接着 FALLBACK_BASELINE 的 distinct=300
又会算出门槛 90,在基线读不出来的那条路径上把同一个 bug 原样再犯一次。
两个数今天都改对了(30 / 97),但**从来没有一条用例钉着它们**。这份文件就是那条钉子。

跑法与 ocr_quality 那套完全一致(CI 同一个 workflow 一起跑):
    python -m pytest tests/ -q
    python -m unittest discover -s tests -q

写断言的规矩,沿用 test_ocr_quality.py 那句命门:
  **不许只断言"通过/不通过"。** verdict() 由四条判据共同决定,任何一条挂了都可能被
  另一条兜住,于是测试永远绿。所以每条垃圾语料都钉住【该抓它的那条判据】,
  每条正货都钉住【当年差点把它判死的那个度量】。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ocr_degeneracy as d          # noqa: E402
import ocr_quality as q             # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "degeneracy")


def load(name):
    with io.open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


# ── 正货:必须通过(verdict 为空)。第二列 = 当年差点把它判死的那个度量 ──────────
GOOD = [
    ("chengwuji_51_distinct_per_1000.txt", "distinct",
     "成無己《註解傷寒論》01-0022912 —— 字种 51/千字,被猜出来的 floor=60 整本扔掉"),
    ("bencao_dose_column_runs.txt", "rep",
     "本草剂量列:同一个字成列连出 22 次,REP_MAX 当年是 12,就是被这种页咬的"),
]

# ── 垃圾:必须判退。第二列 = 该抓它的那条判据在 verdict 里的中文串 ───────────────
JUNK = [
    ("collapsed_low_distinct.txt", "字种过少",
     "字种塌到 21/千字 —— 本仓实测到的真退化值"),
    ("collapsed_char_run_81.txt", "连续重复",
     "一段 81 个相同字符的长连出 —— REP_MAX 注释里那个实测抽样页"),
    ("collapsed_top1_domination.txt", "单字霸屏",
     "单字散布霸屏(不连出),连出判据抓不到,只能靠 top1"),
    ("collapsed_kana_page.txt", "非汉字过多",
     "整页假名 —— 旧 tcm-rag-768 索引里的两种失败模式之一"),
    ("collapsed_digit_page.txt", "非汉字过多",
     "整页数字(图版/索引扫描)—— 另一种"),
]


class TestGoodBooksPass(unittest.TestCase):
    """正货放行。误杀一本 = 少一部书,而"拥有别人没有的书"正是这批书唯一的理由。"""

    def test_good_books_have_no_verdict(self):
        for fn, _metric, why in GOOD:
            with self.subTest(fixture=fn):
                p = d.profile(load(fn))
                self.assertIsNotNone(p, "%s 短到取不到 profile,它就不再是哨兵了" % fn)
                self.assertEqual(d.verdict(p), [], "%s 被误杀(%s)" % (fn, why))

    def test_good_books_pass_with_the_fallback_baseline_too(self):
        """基线读不出来的那条路径上,同一本书必须还是过 —— 见 FALLBACK_BASELINE 注释:
        「丢了基线绝不许悄悄把闸变严」。"""
        bl = d.baseline_of([])
        for fn, _metric, why in GOOD:
            with self.subTest(fixture=fn):
                p = d.profile(load(fn))
                self.assertEqual(d.verdict(p, bl), [],
                                 "%s 在兜底基线下被判死(%s)" % (fn, why))


class TestJunkBooksRejected(unittest.TestCase):
    """垃圾判退,而且必须是【该抓它的那条判据】抓的。
    只看"判没判退"的话,某条判据悄悄失效时会被别的判据兜住,测试就成了永远绿的假哨兵。"""

    def test_junk_books_rejected(self):
        for fn, _tag, why in JUNK:
            with self.subTest(fixture=fn):
                p = d.profile(load(fn))
                self.assertIsNotNone(p, "%s 短到取不到 profile" % fn)
                self.assertTrue(d.verdict(p), "%s 漏放(%s)" % (fn, why))

    def test_junk_caught_by_the_right_criterion(self):
        for fn, tag, why in JUNK:
            with self.subTest(fixture=fn):
                p = d.profile(load(fn))
                why_list = d.verdict(p)
                self.assertTrue(any(w.startswith(tag) for w in why_list),
                                "%s 虽然判退了,但不是「%s」抓的 —— 说明那条已经失效、"
                                "只是被别的判据兜住了(%s);verdict=%s" % (fn, tag, why, why_list))

    def test_each_junk_fixture_trips_exactly_one_criterion(self):
        """一条语料同时踩两条判据 = 它测不出任何单条判据是否还活着。
        本仓的假哨兵就是这么来的:判据 A 挂了,判据 B 兜住,label 照样对。"""
        for fn, tag, _why in JUNK:
            with self.subTest(fixture=fn):
                why_list = d.verdict(d.profile(load(fn)))
                self.assertEqual(len(why_list), 1,
                                 "%s 同时踩了 %s —— 它只该踩「%s」,请调语料"
                                 % (fn, why_list, tag))


class TestChengwujiIncident(unittest.TestCase):
    """血证复现:成無己《註解傷寒論》。两个【猜出来的数】各差点把它扔掉一次。

    这一组用例的价值不在"今天过不过",而在**把当年那两个数会造成的后果钉死在代码里**。
    谁哪天觉得 30「看着太低」想调回 60、或者觉得兜底基线 97「太宽松」想调回 300,
    这里会立刻红,并且红在这本书的名字上。
    """

    HISTORICAL_FLOOR = 60.0          # 当年 DISTINCT_ABS_MIN 的值,一个猜出来的数
    HISTORICAL_FALLBACK_DISTINCT = 300.0   # 当年 FALLBACK_BASELINE["distinct"] 的值

    def setUp(self):
        self.p = d.profile(load("chengwuji_51_distinct_per_1000.txt"))
        self.assertIsNotNone(self.p)

    def test_fixture_really_reproduces_51_distinct_per_1000(self):
        """语料自己先立得住:字种必须真的落在 51/千字附近。

        这条是本文件最要紧的一句 —— 古文词汇会饱和,**同一本书取样越短,
        distinct/千字 越高**。拿 1000 字的短样本冒充,量出来是 105/千字,
        当年 floor=60 根本不会踩雷,下面那几条就全是从写下起就永远绿的假哨兵。
        """
        self.assertGreater(self.p["chars"], 2000,
                           "取样太短,distinct/千字 会虚高,复现不了当年的 51")
        self.assertLess(self.p["distinct"], 55.0)
        self.assertGreater(self.p["distinct"], 45.0)

    def test_it_would_have_been_killed_by_the_guessed_floor_of_60(self):
        """当年 floor=60 的后果,原样复现。"""
        self.assertLess(self.p["distinct"], self.HISTORICAL_FLOOR,
                        "语料够不着当年那条线了 = 复现不了这次事故,请换语料")

    def test_todays_floor_lets_it_live(self):
        self.assertGreaterEqual(self.p["distinct"], d.distinct_floor())
        self.assertEqual(d.verdict(self.p), [])
        self.assertEqual(d.DISTINCT_ABS_MIN, 30.0,
                         "DISTINCT_ABS_MIN 被改了 —— 它不是「看着低」就能调的数,"
                         "读 ocr_degeneracy.py 里那段出处再动")

    def test_the_old_fallback_baseline_would_have_repeated_the_bug(self):
        """兜底基线那条路径上的同一个 bug:300 x 0.30 = 90,门槛比绝对下限高三倍,
        任何一次基线读取失败都会把这本书按同一套算术再扔一次。"""
        old = {"cjk": 0.95, "top1": 0.05, "distinct": self.HISTORICAL_FALLBACK_DISTINCT}
        self.assertEqual(
            d.distinct_floor(old),
            self.HISTORICAL_FALLBACK_DISTINCT * d.DISTINCT_BASELINE_MULT)
        self.assertTrue(any(w.startswith("字种过少") for w in d.verdict(self.p, old)),
                        "当年那个兜底基线不再判死它了 = 这条复现不了事故,请换语料")

    def test_todays_fallback_baseline_does_not_tighten_anything(self):
        """今天的兜底基线必须【一点都不比绝对下限严】。

        FALLBACK_BASELINE 的注释把这条写成了立据:「丢了基线绝不许悄悄把闸变严」。
        97 x 0.30 = 29.1,被 max() 丢掉,恰好退回绝对下限 30 —— 这不是巧合,是选 97
        的理由。这里用【表达式】断言而不是写死 30,数一改这条跟着走,不留会漂移的副本。
        """
        bl = d.baseline_of([])
        self.assertEqual(bl, d.FALLBACK_BASELINE)
        self.assertEqual(d.distinct_floor(bl), d.distinct_floor(),
                         "兜底基线把字种门槛抬高了 —— 基线读不出来就把闸变严,"
                         "正是 FALLBACK_BASELINE=300 那次的病根")
        self.assertGreaterEqual(d.top1_ceiling(bl), d.top1_ceiling(),
                                "兜底基线把单字霸屏的天花板压低了(同上)")
        self.assertEqual(d.verdict(self.p, bl), [])

    def test_missing_baseline_never_makes_any_book_stricter(self):
        """把上一条从"这一本书"推广到"全部语料":基线缺失只许更松,不许更严。"""
        bl = d.baseline_of([])
        for fn in sorted(os.listdir(FIX)):
            if not fn.endswith(".txt"):
                continue
            p = d.profile(load(fn))
            if p is None:
                continue
            with self.subTest(fixture=fn):
                self.assertLessEqual(len(d.verdict(p, bl)), len(d.verdict(p)),
                                     "%s 在兜底基线下被多判了一条 —— 丢基线变严了" % fn)


class TestShortTextGetsNoVerdict(unittest.TestCase):
    """样本太小就【不给判决】,而不是给一个坏判决 —— 与 ocr_quality.MIN_LEN_FOR_REPEAT
    同一个思路。60 字的牌记能量出任何字种数,拿它下结论等于掷骰子。"""

    def test_short_text_returns_none(self):
        self.assertIsNone(d.profile(load("short_colophon_under_min.txt")))
        self.assertIsNone(d.profile(""))
        self.assertIsNone(d.profile(None))

    def test_min_chars_is_the_boundary(self):
        """边界两侧各钉一下:MIN_CHARS-1 不给判决,MIN_CHARS 给。"""
        body = load("chengwuji_51_distinct_per_1000.txt").replace("\n", "")
        self.assertIsNone(d.profile(body[:d.MIN_CHARS - 1]))
        self.assertIsNotNone(d.profile(body[:d.MIN_CHARS]))
        self.assertEqual(d.profile(body[:d.MIN_CHARS])["chars"], d.MIN_CHARS)


class TestPrimitives(unittest.TestCase):
    """底层原语。四条判据全建在它们上面,它们错了上面全错,而且不会报错、只会算错。"""

    def test_longest_run(self):
        self.assertEqual(d.longest_run(""), 1)      # 沿用实现:空串也返回 1
        self.assertEqual(d.longest_run("一"), 1)
        self.assertEqual(d.longest_run("一二三"), 1)
        self.assertEqual(d.longest_run("一一一二"), 3)
        self.assertEqual(d.longest_run("二一一一"), 3)
        self.assertEqual(d.longest_run("一" * 81), 81)

    def test_profile_counts_only_cjk(self):
        """四个数都只在汉字上算:标点与版面在不同版本之间本来就不一样,不是错误。"""
        p = d.profile("一二三四五" * 40 + "、。,;：!?" * 20)
        self.assertEqual(p["chars"], 200)
        self.assertAlmostEqual(p["top1"], 0.2)
        self.assertLess(p["cjk"], 1.0, "cjk 是【占非空白字符】的比例,标点必须进分母")

    def test_profile_ignores_whitespace_in_the_cjk_ratio(self):
        a = d.profile("一二三四五" * 40)
        b = d.profile("\n".join(["一二三四五"] * 40))
        self.assertEqual(a["cjk"], b["cjk"])
        self.assertEqual(a["chars"], b["chars"])

    def test_baseline_of_averages_and_skips_none(self):
        """profile() 对短文本返回 None,而 baseline_of 拿到的正是一堆 profile ——
        它必须把 None 滤掉再平均,否则一本短书就能让整批基线炸掉。"""
        ps = [{"cjk": 1.0, "top1": 0.04, "distinct": 90.0},
              None,
              {"cjk": 0.9, "top1": 0.06, "distinct": 110.0}]
        bl = d.baseline_of(ps)
        self.assertAlmostEqual(bl["distinct"], 100.0)
        self.assertAlmostEqual(bl["top1"], 0.05)
        self.assertEqual(set(bl), set(d.BASELINE_KEYS))

    def test_baseline_of_empty_falls_back(self):
        for empty in ([], None, [None, None]):
            self.assertEqual(d.baseline_of(empty), d.FALLBACK_BASELINE)

    def test_baseline_of_returns_a_copy_not_the_module_constant(self):
        """调用方拿到的若是 FALLBACK_BASELINE 本体,谁改一下就把全仓的兜底改了。"""
        bl = d.baseline_of([])
        bl["distinct"] = 1.0
        self.assertEqual(d.FALLBACK_BASELINE["distinct"], 97.0)

    def test_ceilings_and_floors_never_go_below_the_absolute_ones(self):
        """相对臂只许放松,不许收紧 —— 这是 max() 那两句的全部意义。"""
        for distinct in (0.0, 10.0, 97.0, 300.0):
            bl = {"cjk": 0.95, "top1": 0.0, "distinct": distinct}
            self.assertGreaterEqual(d.distinct_floor(bl), d.DISTINCT_ABS_MIN)
            self.assertGreaterEqual(d.top1_ceiling(bl), d.TOP1_ABS_MAX)


class TestThresholdProvenance(unittest.TestCase):
    """门槛的出处。这个模块每个数背后都有一段"它当年错成什么样"的注释,
    这几条用例就是那些注释的执行者 —— 注释拦不住人改数,红掉的测试拦得住。"""

    def test_the_numbers_that_have_bitten_us(self):
        self.assertEqual(d.DISTINCT_ABS_MIN, 30.0, "曾是 60(猜的),扔了成無己註解傷寒論")
        self.assertEqual(d.FALLBACK_BASELINE["distinct"], 97.0, "曾是 300,门槛会算成 90")
        self.assertEqual(d.REP_MAX, 30, "曾是 12,咬过本草的剂量列")
        self.assertEqual(d.MIN_CHARS, 200)
        self.assertEqual(d.CJK_MIN, 0.80)

    def test_fallback_distinct_is_the_measured_corpus_average(self):
        """97 不是换了个猜的数,它就是 DISTINCT_ABS_MIN 注释里引的那个实测均值。
        两处引同一个数,写在两个地方就会各自漂移 —— 这条把它们绑在一起。"""
        self.assertEqual(d.FALLBACK_BASELINE["distinct"] * d.DISTINCT_BASELINE_MULT,
                         97.0 * 0.30)
        self.assertLess(d.FALLBACK_BASELINE["distinct"] * d.DISTINCT_BASELINE_MULT,
                        d.DISTINCT_ABS_MIN,
                        "兜底基线的相对臂必须落在绝对下限之内,否则 max() 就不再是"
                        "「退回绝对下限」而是「把闸变严」")

    def test_verdict_is_a_reason_list_not_a_boolean(self):
        """返回契约:三个调用方(ocr_quality_gate / release_passed_ocr / diagnose_bad_ocr)
        都是拿它当 list 用的(`why = deg.verdict(p, bl)` 然后判真假 + 打印)。
        改成 bool 不会报错,只会让判退原因在台账里全部消失。"""
        p = d.profile(load("collapsed_low_distinct.txt"))
        why = d.verdict(p)
        self.assertIsInstance(why, list)
        self.assertTrue(all(isinstance(w, str) for w in why))
        self.assertEqual(d.verdict(d.profile(load(GOOD[0][0]))), [])

    def test_profile_keys_are_never_removed(self):
        """profile() 的键被三个调用方直接索引,少一个就是一次 KeyError。"""
        p = d.profile(load(GOOD[0][0]))
        self.assertEqual(set(p), {"cjk", "top1", "distinct", "rep", "chars"})

    def test_public_helpers_still_exist(self):
        """零删除:三个调用方引用的函数/常量一个都不许消失。"""
        for name in ("profile", "verdict", "baseline_of", "distinct_floor", "top1_ceiling",
                     "longest_run", "CJK", "MIN_CHARS", "CJK_MIN", "REP_MAX",
                     "DISTINCT_ABS_MIN", "DISTINCT_BASELINE_MULT", "TOP1_ABS_MAX",
                     "TOP1_BASELINE_MULT", "FALLBACK_BASELINE", "BASELINE_KEYS"):
            self.assertTrue(hasattr(d, name), "%s 没了" % name)


class TestKnownGapRareChars(unittest.TestCase):
    """【已知缺口,本轮不改,留给 CTO 定夺】书级判据的 CJK 表比页级的窄一整轮。

    2026-07-28 这一轮把 ocr_quality._CJK_RE 从「统一表意+扩展A+假名」补齐到了
    扩展B~I / 兼容表意 / 康熙部首 / 部首补充 / 変体仮名。**ocr_degeneracy.CJK 没跟着补**
    —— 它至今只有 U+4E00-9FFF 一段,连扩展A 都不认。

    后果不是理论的,实测在这:
        700 字正文 + 300 字扩展B 的书  -> cjk=0.702 < CJK_MIN 0.80 -> 「非汉字过多」整本判死
        正文与扩展B 逐字相间的书        -> cjk=0.500                -> 同上
    这和本轮修的是同一类病,只是从"丢一页"升级成"丢一整本"。

    为什么本轮不顺手改:profile() 的四个数【互相纠缠】——
    s = "".join(CJK.findall(t)) 是把匹配到的字符【拼起来】,放宽表会同时改动
    distinct(分子分母一起变,方向不定)与 rep(现在会把 一X一X一X 拼成 一一一 而误报连出 3)。
    方向不像页级那样单调,必须有自己的一轮书级差分才能动。**这是任务外的发现,写进报告等 CTO 拍板。**

    下面两条是【事实断言,不是期望行为】:它们钉住"两个模块的 CJK 定义今天不一致"这件事。
    谁哪天把它们对齐了,这里会红 —— 那正是提醒他回来读这段、并把这个类删掉的时候。
    """

    RARE = ("\U00020B9F", "﨑", "⾗", "⺅", "\U0001B002", "㐀")

    def test_page_level_table_accepts_these_but_book_level_does_not(self):
        for ch in self.RARE:
            with self.subTest(cp="U+%04X" % ord(ch)):
                self.assertTrue(q._CJK_RE.match(ch),
                                "页级表不认 U+%04X,本轮的修复没做全" % ord(ch))
                self.assertIsNone(d.CJK.match(ch),
                                  "书级表已经认 U+%04X 了 —— 缺口被补上了(好事),"
                                  "请回来读 TestKnownGapRareChars 的说明并删掉这个类"
                                  % ord(ch))

    def test_a_book_of_rare_characters_is_rejected_today(self):
        """把后果量出来钉住,免得下一轮又要从头考古一遍这个缺口有多大。"""
        body = load("chengwuji_51_distinct_per_1000.txt").replace("\n", "")[:700]
        book = body + "".join(chr(0x20B9F + (i % 60)) for i in range(300))
        p = d.profile(book)
        self.assertLess(p["cjk"], d.CJK_MIN)
        self.assertTrue(any(w.startswith("非汉字过多") for w in d.verdict(p)),
                        "缺口已被补上,请删掉这个类;verdict=%s" % d.verdict(p))


class TestFixtureIntegrity(unittest.TestCase):
    """语料自身的体检。语料一空/一漂,上面所有断言都会变成假绿。"""

    def test_all_fixtures_load_and_are_long_enough(self):
        for fn, _t, _w in GOOD + JUNK:
            with self.subTest(fixture=fn):
                self.assertIsNotNone(d.profile(load(fn)),
                                     "%s 短到取不到 profile,它测不了任何判据" % fn)

    def test_every_fixture_file_is_referenced(self):
        """磁盘上每个语料都必须被上面的表引用到 —— 没人用的语料下一轮就会被误删。"""
        listed = set([f for f, _t, _w in GOOD + JUNK]) | {"short_colophon_under_min.txt"}
        on_disk = {fn for fn in os.listdir(FIX) if fn.endswith(".txt")}
        self.assertEqual(on_disk - listed, set(), "有语料没被任何用例引用")
        self.assertEqual(listed - on_disk, set(), "用例引用了不存在的语料")


if __name__ == "__main__":
    unittest.main(verbosity=2)
