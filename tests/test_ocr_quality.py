# -*- coding: utf-8 -*-
"""ocr_quality 判据回归。
判据改到第四轮了(长度下限 -> 背靠背豁免 -> 扩到长页 -> 版面记号),前三轮的语料和差分
脚本每次都留在临时目录,会话一结束就没,下一轮又从头构造 —— 这套测试就是为了断掉
"每轮重新考古"。**每条 fixture 都是一次真实误杀的产物,删之前先去 MANIFEST.md 看它记的是哪一次。**

跑法(两种都行,CI 用第一种):
    python -m pytest tests/ -q
    python -m unittest discover -s tests -q      # 零第三方依赖的退路

写断言的规矩(这套测试自己的命门):
  **不许只断言 label。** label 由 9 条判据共同决定,任何一条挂了都可能被另一条兜住,
  于是测试永远绿 —— 本仓吃过这个亏(有一条回归断言的 fixture 用了全角括号,旧实现
  根本不会踩雷,从写下起就是永远绿的假哨兵)。所以每条历史事故除了 label,
  还要钉住【当年真正把它判死的那个度量】,那个数一变测试就红。
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ocr_quality as q          # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(rel):
    """读一条语料。`.esc.txt` = unicode_escape 存的,读回来要解码。

    为什么要有 .esc 这一档:① 线上真实 OCR 原文不以明文进 public 仓(沿用本仓既有约定,
    见 ocr_recover_scan.SNIPPET_MODE="escaped");② 控制符/替换符这类样本本来就没法
    当明文存。构造出来的正常语料一律明文,可读性优先。"""
    p = os.path.join(FIX, rel)
    with io.open(p, encoding="utf-8") as f:
        t = f.read()
    if rel.endswith(".esc.txt"):
        return t.rstrip("\n").encode("ascii").decode("unicode_escape")
    return t


def _make_illegible(n, ratio, run_len, mark="□"):
    """造一页【正好 n 字】、版面记号占比≈ratio、记号按 run_len 成段铺开的漫漶页。
    真实的漫漶是"某一列糊了一片"而不是逐字随机丢,所以 run_len 要能调:
    1=散点 / 4=小段 / 12=整列。基文取语料里那页正常正文,循环铺满。

    这个造法只用来做【区域扫描】(证明整片占比区间都放行),不替代静态语料 ——
    静态语料记的是"哪一次真事故",生成器记不了那个。"""
    base = "".join(load("synthetic/shanghan_body_long.txt").split())
    body = list((base * (n // len(base) + 2))[:n])
    want = int(round(n * ratio))
    n_runs = max(1, want // run_len)
    step = max(run_len, n // n_runs)
    placed, pos = 0, 0
    while placed < want and pos < n:
        take = min(run_len, want - placed, n - pos)
        for j in range(take):
            body[pos + j] = mark
        placed += take
        pos += step
    i = 0
    while placed < want and i < n:
        if body[i] != mark:
            body[i] = mark
            placed += 1
        i += 1
    return "".join(body)


# ─────────────────────────────────────────────────────────────────────────────
# 正货:必须放行(label != reject)。每条后面写清它是哪一次事故的产物。
# ─────────────────────────────────────────────────────────────────────────────
GOOD = [
    ("real/bcgmj21_page_0007.esc.txt",
     "2026-07-27 抽样审计 199 页,唯一一条 reject 就是它 —— 线上真货被误杀的第一份实证"),
    ("synthetic/formula_guizhitang_26.txt",
     "第一轮:26 字桂枝湯组成,max_run=0.54 被判退。方剂组成页 = 药方库与 AI 寻脉的原始来源"),
    ("synthetic/formula_bazhentang_39.txt",
     "第二轮:39 字八珍湯,repeat_ngram=0.54;刚好卡在 40 字门槛下沿,靠长度下限活着"),
    ("synthetic/formula_shiquandabu_49.txt",
     "第三轮:49 字十全大補湯 —— 40 字那条线把方剂页劈成两半,长的这半继续被误杀"),
    ("synthetic/formula_shiquandabu_50.txt",
     "第三轮承重版:50 字,repeat_ngram=0.54 真够着判死门槛,靠背靠背豁免才活"),
    ("synthetic/chengwuji_dense_commentary.txt",
     "成無己《註解傷寒論》式密排注文:字种高度集中的真古籍页不许被当成复读"),
    ("synthetic/shanghan_body_long.txt",
     "普通古籍正文长页 —— 零改动基准,任何一轮改判据都不许动它"),
    ("synthetic/mulu_dotted_leader.txt",
     "目录点线页(… 占 53%):第三轮 _REPEAT_MARKS 建表就是为它"),
    ("synthetic/wakoku_ditto_marks.txt",
     "和刻本同上记号(〃ゝ々 占 53%):第四轮之前是 suspect,被 garbage~0.35 挂着"),
    ("synthetic/illegible_real_style_47.txt",
     "第四轮主角:47 字漫漶缺字页,□ 占 64% -> garbage=0.64 + single_char=0.64 判死"),
    ("synthetic/illegible_real_style_preface.txt",
     "第四轮:序跋页零星漫漶,□ 只占 14%,当基准 —— 少量记号页本来就该 clean"),
    ("synthetic/illegible_boxes_r40.txt", "第四轮:□ 占 40% 的漫漶页"),
    ("synthetic/illegible_boxes_r50.txt", "第四轮:□ 占 50%,旧 GARBAGE_REJECT 正好在这判死"),
    ("synthetic/illegible_boxes_r60.txt", "第四轮:□ 占 60%"),
    ("synthetic/illegible_boxes_r70.txt", "第四轮:□ 占 70%,放行区的上沿"),
    ("synthetic/illegible_boxes_scattered_r60.txt",
     "第四轮:60% 的 □ 逐字散布(不是成段糊)—— 糊法不该影响判决"),
    ("synthetic/illegible_boxes_column_r60.txt",
     "第四轮:60% 的 □ 成整列糊在一起,200 字长页"),
]

# ─────────────────────────────────────────────────────────────────────────────
# 垃圾:必须判死。第三项 = 当年【真正按住它】的那条判据,必须出现在 reasons 里,
# 只断言 label 会让判据之间互相兜底,测试就成了假哨兵。
# ─────────────────────────────────────────────────────────────────────────────
JUNK = [
    ("synthetic/repeat_yingniao_x12.txt", "abs_repeat",
     "第二轮 run 30339783256:「鶯鳥」连出 12 次(24 字),因 40 字下限从 reject 翻成 ok"),
    ("synthetic/repeat_egaku_x9.txt", "abs_repeat",
     "同上:「鵝鶚」连出 9 次(18 字)"),
    ("synthetic/repeat_shang_x8.txt", "abs_repeat",
     "同上:「上」连出 8 次(8 字)—— ABS_REPEAT_REJECT=8 就是取的这条实测最小恶性值"),
    ("synthetic/repeat_unit20_x8.txt", "abs_repeat",
     "第三轮:20 字单元连出 8 次;旧单元上限 4 数不到它,旧 abs_repeat 报 1"),
    ("synthetic/repeat_inline_in_body.txt", "abs_repeat",
     "第三轮自报的代价②:长页正文里夹 30 字局部复读,占比够不着、次数够得着"),
    ("synthetic/line_dup_6lines.txt", "line_dup",
     "整行复读 6 行 —— 背靠背豁免只纠 repeat_ngram/max_run,绝不许纠到 line_dup 头上"),
    ("synthetic/model_meta_japanese.txt", "model_meta",
     "线上实测:一整页只有「图中包含的文字内容是日文。」13 字,重复类判据一条都抓不到"),
    ("synthetic/garbage_replacement.esc.txt", "garbage",
     "替换符乱码页(U+FFFD)—— 第四轮放松 garbage_ratio 后,它必须一点没松"),
    ("synthetic/garbage_control_block.esc.txt", "garbage",
     "控制符块 —— 同上"),
    ("synthetic/garbage_ascii_spam.txt", "repeat_ngram",
     "纯 ASCII 符号刷屏:garbage 看不见它(符号都在 _ASCII_OK 里),靠重复判据按住"),
    ("synthetic/marks_all_boxes.txt", "layout_mark_page",
     "第四轮:整页只有 □,一个字都没读出来"),
    ("synthetic/marks_kana_iteration_spam.txt", "layout_mark_page",
     "第四轮开出来的洞:ヽ 落在假名区,cjk_ratio=1.00 骗过 cjk_low 与背靠背豁免两道闸"),
    ("synthetic/marks_zero_content.txt", "layout_mark_page",
     "第四轮开出来的洞之二:记号占 83% 够不着 0.85,但记号之外一个内容字都没有"),
    ("synthetic/marks_launder_replacement.esc.txt", "garbage",
     "第四轮洗白试探:一半替换符 + 一半 □,记号不许把真乱码的占比稀释到门槛以下"),
]


class TestGoodPagesPass(unittest.TestCase):
    """正货放行。误杀一页方剂组成页 = 药方库少一条出处,比漏一页噪音贵得多。"""

    def test_good_pages_not_rejected(self):
        for rel, why in GOOD:
            with self.subTest(fixture=rel):
                a = q.analyze(load(rel))
                self.assertNotEqual(a["label"], "reject",
                                    f"{rel} 被误杀({why});reasons={a['reasons']}")

    def test_good_pages_are_ok_not_merely_suspect(self):
        """再进一步:这些页应该是 ok 而不是 suspect。
        suspect 页虽然照旧入库,但会把日审报告的「非空异常率」顶起来 —— 正常方剂页
        被计成异常,那是另一种虚惊,本仓明令禁止。"""
        for rel, why in GOOD:
            with self.subTest(fixture=rel):
                a = q.analyze(load(rel))
                self.assertEqual(a["label"], "ok",
                                 f"{rel} 落到 {a['label']}({why});reasons={a['reasons']}")


class TestJunkPagesRejected(unittest.TestCase):
    """垃圾判死,而且必须是【该抓它的那条判据】抓的。"""

    def test_junk_rejected(self):
        for rel, _tag, why in JUNK:
            with self.subTest(fixture=rel):
                a = q.analyze(load(rel))
                self.assertEqual(a["label"], "reject",
                                 f"{rel} 漏放({why});reasons={a['reasons']}")

    def test_junk_caught_by_the_right_judgment(self):
        for rel, tag, why in JUNK:
            with self.subTest(fixture=rel):
                a = q.analyze(load(rel))
                self.assertTrue(any(r.startswith(tag) for r in a["reasons"]),
                                f"{rel} 虽然判死了,但不是 {tag} 抓的 —— 说明 {tag} 已经失效、"
                                f"只是被别的判据兜住了({why});reasons={a['reasons']}")


class TestHistoricalMetrics(unittest.TestCase):
    """承重哨兵:钉住【当年真正把这一页判死的那个度量】。

    只钉 label 是不够的 —— 判据之间会互相兜底,某一条悄悄失效时 label 还是对的,
    测试就永远绿。下面每条断言的意思都是:"这一页确实踩在门槛上,它现在活着是因为
    某个机制在保它;那个机制一没,这里立刻红。"
    """

    def test_short_page_length_floor_is_load_bearing(self):
        """26 字桂枝湯 / 39 字八珍湯:比例本身确实超了判死门槛,靠 MIN_LEN_FOR_REPEAT 救。
        MIN_LEN_FOR_REPEAT 一旦被调小/删掉,这两页当场重新被误杀。"""
        a = q.analyze(load("synthetic/formula_guizhitang_26.txt"))
        self.assertLess(a["len"], q.MIN_LEN_FOR_REPEAT)
        self.assertGreaterEqual(a["max_run"], q.MAX_RUN_REJECT,
                                "桂枝湯 fixture 已经够不着 max_run 门槛了 = 它不再是哨兵,请换一条")
        self.assertEqual(a["label"], "ok")

        b = q.analyze(load("synthetic/formula_bazhentang_39.txt"))
        self.assertLess(b["len"], q.MIN_LEN_FOR_REPEAT)
        self.assertGreaterEqual(b["repeat_ngram"], q.REPEAT_NGRAM_REJECT,
                                "八珍湯 fixture 已经够不着 repeat_ngram 门槛了 = 不再是哨兵,请换一条")
        self.assertEqual(b["label"], "ok")

    def test_long_page_scattered_exemption_is_load_bearing(self):
        """50 字十全大補湯:长页,比例真够着判死门槛,靠背靠背豁免(scattered)救。"""
        a = q.analyze(load("synthetic/formula_shiquandabu_50.txt"))
        self.assertGreaterEqual(a["len"], q.MIN_LEN_FOR_REPEAT)
        self.assertGreaterEqual(a["repeat_ngram"], q.REPEAT_NGRAM_REJECT,
                                "十全大補湯 fixture 已经够不着门槛了 = 不再是哨兵,请换一条")
        self.assertLessEqual(a["abs_repeat"], q.ABS_REPEAT_CLEAN,
                             "方剂组成页的背靠背连出必须恒低 —— 豁免正是靠这个把正货和复读分开")
        self.assertIn("scattered_repeat", "".join(a["reasons"]))
        self.assertEqual(a["label"], "ok")

    def test_abs_repeat_threshold_sits_on_measured_worst_good_and_best_bad(self):
        """ABS_REPEAT_REJECT=8 / ABS_REPEAT_CLEAN=2 都是站在实测极值上的,中间 3..7 无样本。
        「上」×8 是判退侧实测到的最小恶性值,门槛就取的它;它一旦够不着 8,说明有人动了数法。"""
        a = q.analyze(load("synthetic/repeat_shang_x8.txt"))
        self.assertEqual(a["abs_repeat"], q.ABS_REPEAT_REJECT)
        self.assertEqual(q.ABS_REPEAT_CLEAN, 2)

    def test_long_unit_repeat_needs_wide_unit_scan(self):
        """20 字单元连出 8 次:单元上限从 4 加宽到 25 才数得到它。
        ABS_REPEAT_MAX_UNIT 一旦被调回小值,这条立刻红。"""
        a = q.analyze(load("synthetic/repeat_unit20_x8.txt"))
        self.assertEqual(a["abs_repeat_unit"], 20)
        self.assertGreaterEqual(a["abs_repeat"], q.ABS_REPEAT_REJECT)

    def test_model_meta_only_on_short_pages(self):
        """元话术只在短页判死:够长的页说明模型确实读出了东西,尾巴上缀一句不该整页判死。"""
        short = load("synthetic/model_meta_japanese.txt")
        self.assertEqual(q.analyze(short)["label"], "reject")
        padded = load("synthetic/shanghan_body_long.txt") + short
        a = q.analyze(padded)
        self.assertGreaterEqual(a["len"], q.MIN_LEN_FOR_REPEAT)
        self.assertGreaterEqual(a["model_meta"], 0, "元话术标记本身要照旧命中(只是不判死)")
        self.assertNotEqual(a["label"], "reject")


class TestLayoutMarks(unittest.TestCase):
    """第四轮:版面记号。根因是【两张字符表互相不认】,所以第一条测的就是两表一致。"""

    def test_every_layout_mark_is_not_garbage(self):
        """根因回归:_REPEAT_MARKS 里的字符,一个都不许被 garbage_ratio 当成乱码。
        改动前 25 个记号里有 16 个(□ ○ ● ■ 々 〃 ※ ‥ ― – ◇ ◆ △ ▲ ▽ ▼)在这里是红的。"""
        for ch in sorted(q._REPEAT_MARKS):
            with self.subTest(mark=hex(ord(ch))):
                self.assertEqual(q.garbage_ratio(ch * 30), 0.0,
                                 f"U+{ord(ch):04X} 在 _REPEAT_MARKS 里是版面记号,"
                                 f"却被 garbage_ratio 当成乱码 —— 两张字符表又不一致了")

    def test_every_layout_mark_is_not_single_char_spam(self):
        """同一根因的另一半:记号刷屏不该算"单字符刷屏"(那是模型复读的症状,不是排版)。"""
        for ch in sorted(q._REPEAT_MARKS):
            with self.subTest(mark=hex(ord(ch))):
                self.assertEqual(q.single_char_ratio(ch * 30), 0.0,
                                 f"U+{ord(ch):04X} 被 single_char_ratio 当成刷屏字符")

    def test_every_layout_mark_counts_into_mark_ratio(self):
        """判据 9 必须认得全表,漏一个就漏一档记号刷屏页。"""
        for ch in sorted(q._REPEAT_MARKS):
            with self.subTest(mark=hex(ord(ch))):
                self.assertEqual(q.mark_ratio(ch * 30), 1.0)

    def test_illegible_pages_pass_up_to_the_line(self):
        """漫漶缺字页扫描:0.40/0.50/0.60/0.70 一律放行,而且确实带着高记号占比。"""
        for rel, want in (("synthetic/illegible_boxes_r40.txt", 0.40),
                          ("synthetic/illegible_boxes_r50.txt", 0.50),
                          ("synthetic/illegible_boxes_r60.txt", 0.60),
                          ("synthetic/illegible_boxes_r70.txt", 0.70)):
            with self.subTest(fixture=rel):
                a = q.analyze(load(rel))
                self.assertAlmostEqual(a["mark_ratio"], want, places=2,
                                       msg="语料自己漂了,先修语料再看判决")
                self.assertEqual(a["label"], "ok", f"reasons={a['reasons']}")

    def test_illegible_pages_pass_across_lengths_and_shapes(self):
        """同样四档占比,再乘上页长和"糊法"扫一遍 —— 改动前实测这三个维度对判决
        毫无影响(判死的是 garbage_ratio 这个逐字符计数),改动后也必须一样毫无影响。
        只钉四个静态语料文件是不够的:那样只证明了四个点,证不了"整片区域"。"""
        for ratio in (0.40, 0.50, 0.60, 0.70):
            for n in (30, 47, 90, 200):
                for run_len, shape in ((1, "散点"), (4, "小段"), (12, "整列")):
                    text = _make_illegible(n, ratio, run_len)
                    a = q.analyze(text)
                    with self.subTest(ratio=ratio, n=n, shape=shape):
                        self.assertLess(a["mark_ratio"], q.MARK_RATIO_REJECT)
                        self.assertEqual(a["label"], "ok",
                                         f"□ 占 {a['mark_ratio']:.2f} 的 {n} 字漫漶页"
                                         f"({shape})被判 {a['label']};reasons={a['reasons']}")

    def test_mark_ratio_death_line(self):
        """死线:记号占比 >= MARK_RATIO_REJECT 判死,低于它放行。
        用同一条基文只改记号数量扫过门槛,任何"多一个记号就翻脸"以外的不连续都会露出来。"""
        base = load("synthetic/shanghan_body_long.txt").strip()
        n = 200
        for k in range(150, 191, 2):          # 记号 150..190 个 => 占比 0.75..0.95
            text = "□" * k + (base * 3)[:n - k]
            a = q.analyze(text)
            want_reject = a["mark_ratio"] >= q.MARK_RATIO_REJECT
            with self.subTest(marks=k, mark_ratio=a["mark_ratio"]):
                self.assertEqual(a["label"] == "reject", want_reject,
                                 f"记号占比 {a['mark_ratio']:.3f} 落在门槛 "
                                 f"{q.MARK_RATIO_REJECT} 的错误一侧;reasons={a['reasons']}")

    def test_zero_content_page_dies_even_below_the_line(self):
        """记号占比够不着 0.85,但记号之外一个内容字都没有 —— 这页照样没有内容。
        专挡假名区记号(ヽヾゝゞ・ー):它们让 cjk_ratio 虚高,cjk_low 和豁免两道闸都失灵。"""
        a = q.analyze(load("synthetic/marks_zero_content.txt"))
        self.assertLess(a["mark_ratio"], q.MARK_RATIO_REJECT,
                        "这条 fixture 的意义就是【够不着死线】,占比一旦超了就测不到零内容那一支")
        self.assertEqual(a["content_len"], 0)
        self.assertEqual(a["label"], "reject")

    def test_marks_cannot_launder_real_garbage(self):
        """记号不许把真乱码的占比稀释到门槛以下。一半替换符 + 一半 □,garbage 仍须够着判死。"""
        a = q.analyze(load("synthetic/marks_launder_replacement.esc.txt"))
        self.assertGreaterEqual(a["garbage_ratio"], q.GARBAGE_REJECT,
                                "版面记号把真乱码洗白了 —— 记号只该从分子里退出,不该稀释分子")
        self.assertEqual(a["label"], "reject")

    def test_normal_pages_without_marks_are_untouched(self):
        """没有版面记号的页,第四轮一个数都不许动(garbage/single 的口径只对记号生效)。"""
        for rel, _why in GOOD + [(r, w) for r, _t, w in JUNK]:
            a = q.analyze(load(rel))
            if a["mark_ratio"] != 0.0:
                continue
            with self.subTest(fixture=rel):
                self.assertEqual(a["garbage_ratio"], a["garbage_ratio_raw"])
                self.assertEqual(a["single_char"], a["single_char_raw"])

    def test_layout_marks_breadcrumb_left_when_exemption_fires(self):
        """豁免留痕:版面记号确实把一页从判据 4/5 手里救下来时,reasons 必须留下 layout_marks=。
        这是本模块唯一两条【放行】逻辑之一,放错了得有人在日审报告里看得见。"""
        a = q.analyze(load("synthetic/illegible_real_style_47.txt"))
        self.assertTrue(any(r.startswith("layout_marks=") for r in a["reasons"]),
                        f"救了却没留痕,日审报告里查不到;reasons={a['reasons']}")
        # 反面:干净的正文页不许挂这条,否则 top_reasons 被刷屏
        b = q.analyze(load("synthetic/shanghan_body_long.txt"))
        self.assertNotIn("layout_marks", "".join(b["reasons"]))


class TestReturnContract(unittest.TestCase):
    """analyze() 的返回契约。调用方(ocr.py / ocr_xf.py / ocr_ndl.py / ocr_reflash.py /
    ocr_quality_check.py / ocr_recover_scan.py / ocr_reject_log.py)是直接索引键的。"""

    #

    REQUIRED_KEYS = {
        "len", "label", "score", "reasons", "cjk_ratio", "garbage_ratio", "single_char",
        "repeat_ngram", "repeat_n", "repeat_unit", "max_run", "run_period",
        "line_dup", "n_lines", "abs_repeat", "abs_repeat_unit", "model_meta",
        # 2026-07-28 第四轮新增。只增不改不删 —— 这个集合只许往里加。
        "mark_ratio", "content_len", "garbage_ratio_raw", "single_char_raw",
    }

    def test_keys_are_never_removed(self):
        got = set(q.analyze("傷寒論卷第一太陽病脉浮頭項強痛而惡寒").keys())
        missing = self.REQUIRED_KEYS - got
        self.assertFalse(missing, f"返回键被删/改名了:{sorted(missing)} —— 调用方会 KeyError")

    def test_empty_branch_has_identical_keys(self):
        """空文本那条早退分支是历史上最容易漏补的地方:新增字段只加在正常分支,
        ocr_recover_scan.py 一索引就是 KeyError。"""
        self.assertEqual(set(q.analyze("").keys()),
                         set(q.analyze("傷寒論").keys()))
        self.assertEqual(q.analyze("")["label"], "empty")

    def test_reasons_never_leak_page_text(self):
        """reasons 会被印进 public 仓的 run log,不许把页面原文回吐出去。
        判据 8 命中时只报标记序号(model_meta=k0),不报命中的那句话。"""
        for rel, _t, _w in JUNK:
            with self.subTest(fixture=rel):
                text = load(rel)
                joined = "".join(q.analyze(text)["reasons"])
                for i in range(0, len(text) - 4):
                    frag = text[i:i + 4].strip()
                    if len(frag) == 4:
                        self.assertNotIn(frag, joined,
                                         f"{rel}:reasons 里出现了页面原文片段")

    def test_thresholds_are_derived_not_duplicated(self):
        """本模块的规矩:能从别的常量算出来的门槛一律写成表达式,不留第二个会漂移的副本。"""
        self.assertEqual(q.MIN_LEN_FOR_REPEAT, q.LONG_PAGE)
        self.assertEqual(q.ABS_REPEAT_MAX_UNIT,
                         int(round(q.REPEAT_NGRAM_MAX_N / q.REPEAT_NGRAM_SUSPECT)))
        self.assertEqual(q.MARK_RATIO_REJECT, 1.0 - q.CJK_SUSPECT)

    def test_public_helpers_still_exist(self):
        """零删除:产线和审计脚本引用的函数一个都不许消失。"""
        for name in ("analyze", "is_reject", "verdict", "cjk_ratio", "garbage_ratio",
                     "single_char_ratio", "mark_ratio", "content_len", "repeat_ngram",
                     "max_run_ratio", "abs_repeat_run", "model_meta_hit", "line_dup_ratio"):
            self.assertTrue(callable(getattr(q, name, None)), f"{name} 没了")

    def test_empty_and_whitespace_are_empty_not_reject(self):
        """产线靠 label 分流,空页必须是 empty 而不是 reject(reject 会被退回重跑,白烧配额)。"""
        for t in ("", "   ", "\n\n\t "):
            self.assertEqual(q.analyze(t)["label"], "empty")
        self.assertFalse(q.is_reject(""))


class TestFixtureIntegrity(unittest.TestCase):
    """语料自身的体检:文件在、读得出、没被清空。语料一空,上面所有断言都会变成假绿。"""

    def test_all_fixtures_load_and_are_non_empty(self):
        listed = [r for r, _w in GOOD] + [r for r, _t, _w in JUNK]
        for rel in listed:
            with self.subTest(fixture=rel):
                self.assertTrue(len(load(rel).strip()) > 0, f"{rel} 是空的")

    def test_every_fixture_file_is_referenced(self):
        """磁盘上的每个语料文件都必须被上面的表引用到 —— 没人用的语料下一轮就会被误删。"""
        listed = set([r for r, _w in GOOD] + [r for r, _t, _w in JUNK])
        on_disk = set()
        for sub in ("real", "synthetic"):
            d = os.path.join(FIX, sub)
            for fn in os.listdir(d):
                if fn.endswith(".txt"):
                    on_disk.add(f"{sub}/{fn}")
        self.assertEqual(on_disk - listed, set(),
                         "有语料文件没被任何用例引用(要么补用例,要么连同 MANIFEST 一起删)")
        self.assertEqual(listed - on_disk, set(), "用例引用了不存在的语料文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
