# -*- coding: utf-8 -*-
"""哪些码位算「中日韩文字」—— 一份定义,六个调用方读它。

和 ocr_degeneracy.py 是同一个理由建的、同一类病治的,只是低一层:
那边收敛的是【四个门槛数】,这边收敛的是【字符表本身】。

病历:2026-07-28 之前,同一张 CJK 表在仓里有五份互相独立的副本,而且各自漂移 ——

    ocr_quality.py        21 段(2026-07-28 补齐)   页级判据,4 条判据共用
    ocr_quality_gate.py    1 段  [\\u4e00-\\u9fff]   闸门:bigram 相似度 + 书名匹配
    ocr_degeneracy.py      1 段  [\\u4e00-\\u9fff]   书级判据,判错一本丢一整本
    diagnose_bad_ocr.py    1 段  [\\u4e00-\\u9fff]   失败原因分类
    compare_ocr.py         4 段                    引擎对比报告
    compare_ocr_v2.py      4 段                    同上

后果不是"风格不统一"这种事:窄表把它不认识的真汉字【逐个当成乱码】,
于是同一本古籍在页级放行、在书级判死。实测(见下面 ★ 缺口有多大)
旧的 4 段表漏掉 unicodedata 认作 CJK 的 98901 个码点里的 71134 个 = 71.9%;
只画一段的那三份漏得更多,连扩展A 都不认。

──────────────────────────────────────────────────────────────────────────
选哪个 pattern —— 三个问题,别混:

    HAN        汉字(表意 + 部首),【不含假名】。
               给"这段文本是不是汉文"用。ocr_degeneracy.CJK_MIN 立身之本就是
               「整页假名 = 退化」,把假名收进来它当场变瞎(见 HAN 下方注释)。

    CJK_TEXT   汉字 + 假名 = 中日韩文字。
               给"这个字符是不是正常内容(而不是乱码)"用。和刻本漢方书满篇假名,
               把假名算成乱码就会整页判死 —— ocr_quality 的 garbage_ratio 走这条。

    KANA       只有假名。给"这本是不是日文汉方"这种分流判断用。

三个都由同一张区块表拼出来,不许任何调用方再写自己的字面量区间。
tests/test_cjk_charset.py 会逐文件扫描全仓,谁再复制一份,CI 当场红。
──────────────────────────────────────────────────────────────────────────
"""
import re

# ---- 区块表:按用途分三组,合并成一张 -----------------------------------------
# 下面这段考古(为什么是这些区块、为什么按区块边界而不按已分配码位、星平面的坑、
# 哪些刻意没收)原本写在 ocr_quality.py 的 _CJK_BLOCKS 上方,随表一起搬过来 ——
# 表在哪,理由就该在哪,否则下一个人只看得见数字看不见依据。
#
# 2026-07-28 补齐。在此之前只有【统一表意 + 扩展A + 平/片假名】四段
# (沿用 ocr_ndl.py 当年标定的范围),扩展B~I、兼容表意(U+F900-FAFF)、
# 康熙部首(U+2F00-2FDF)、汉字部首补充(U+2E80-2EFF)全部落在表外,
# 于是 garbage_ratio 把这些码位的字【逐个计成乱码】。改前实测
# (120 字正常古籍正文页,把其中一定比例的字逐字换成生僻字):
#     掺入占比 0.10/0.20 -> ok      0.30/0.40 -> suspect(garbage~0.30/0.40)
#     掺入占比 0.50      -> reject(garbage=0.50)   0.60 -> reject(garbage=0.60)
#   扩展B(U+20B9F 𠮟)/ 兼容表意(U+F9A8)/ 康熙部首(U+2F97 ⾗)/ 部首补充(U+2E85 ⺅)
#   四类走的是【完全同一条曲线】—— 判死的不是字,是"这个码位不在表里"这一件事。
# 而古籍的生僻药名、人名、异体字真的会用到这些码位:兼容表意区里的 U+FA11 﨑、
# U+FA10 塚 是日本人名/地名常用字(内閣文庫和刻本漢方书满篇都是),康熙部首整段是
# 字書/類書的部首索引页(那种页面几乎 100% 由部首构成,改前 garbage=1.00 整页判死),
# 扩展B 以上则是善本里最稀见的那一档异体字。越是稀见的古籍、越是需要精确保留的异体字,
# 越容易中招 —— 和「漫漶善本被当乱码」是同一类病:判据把"我不认识"当成了"这是垃圾"。
#
# ★ 范围依据:取 Unicode【区块边界】,不取"当前 Unicode 版本已分配到哪个码位"。
#   区块边界是固定的,已分配范围会随 Unicode 版本往后长(扩展H 是 Unicode 15.0 才有的,
#   Python 3.11 的 unicodedata 是 14.0、3.12 是 15.0)—— 按已分配码位写,换个 Python
#   版本就漂一次;按区块边界写,新版本新加的字自动在表内。代价是表里含未分配码位,
#   而未分配码位在真实 OCR 文本里【不可能出现】,不吃亏。
#   这不是抄来的常识:tests/test_ocr_quality.py::test_cjk_table_covers_every_ideographic_codepoint
#   拿 Python 自带的 unicodedata 逐码点全量核对本表,漏一个码点当场红。
#
# ★ 星平面(U+10000 以上)的坑,实测过再写下来的:Python 3 的 str 按【码点】存,
#   len("\U00020B9F") == 1、逐字符遍历拿到的就是那一个码点、sys.maxunicode = 0x10FFFF,
#   没有 UTF-16 代理对那套问题;re 模块也直接支持 \UXXXXXXXX 转义与跨平面字符区间。
#   所以 garbage_ratio 里的逐字符循环、single_char 的 Counter、mark_ratio 的计数
#   全都不需要为 4 字节字符改写。(若哪天换成 JS/Java 那种 UTF-16 语言,这里必须重写。)
#
# ★ 写成 (lo, hi, name) 表 + 拼出正则,而不是一行字面量:一行字面量里 16 段区间
#   肉眼核不出对错,而这一整轮修的就是"上一版那行字面量少画了 17 段"。
#   缺口有多大,是量出来的不是估的:unicodedata 15.0 认作 CJK 表意/部首/假名的
#   98901 个码点里,旧表(4 段)漏掉 71134 个 = 71.9%,新表(21 段)漏 0 个。
#   表能被测试逐条核对,字面量不能。
#
# ★ 为什么这里可以"按标准全收",而门槛必须"只站在实测样本上"——这两条规矩不打架,
#   而且分不清正是本 bug 的根:门槛是取舍(松一点漏垃圾、紧一点杀正货),没有实测
#   就没有取舍依据;而"U+FA11 算不算汉字"是【事实问题】,答案在 Unicode 标准里,
#   不在我们的语料里。上一版的表是照"我见过什么"画的,不是照"标准怎么定"画的,
#   于是把没见过的真汉字判成了乱码。事实问题就该把事实收全。

RADICAL_BLOCKS = (
    (0x02E80, 0x02EFF, "CJK Radicals Supplement"),              # 汉字部首补充 ⺀⺅
    (0x02F00, 0x02FDF, "Kangxi Radicals"),                      # 康熙部首 ⼀⾗
)
# 部首归在 HAN 一侧而不是单独一档:它们是汉字构件、Unicode Script=Han,
# 字書/類書的部首索引页几乎 100% 由它们构成。归到假名侧或排除在外,
# 那种页面在书级判据上就是 cjk≈0 -> 整本判死。

KANA_BLOCKS = (
    (0x03040, 0x0309F, "Hiragana"),                             # 平假名(原有)
    (0x030A0, 0x030FF, "Katakana"),                             # 片假名(原有)
    (0x031F0, 0x031FF, "Katakana Phonetic Extensions"),         # 片假名语音扩展
    (0x1AFF0, 0x1AFFF, "Kana Extended-B"),
    (0x1B000, 0x1B0FF, "Kana Supplement"),                      # 変体仮名(和刻本)
    (0x1B100, 0x1B12F, "Kana Extended-A"),                      # 変体仮名(和刻本)
    (0x1B130, 0x1B16F, "Small Kana Extension"),
)
# 假名那 4 段星平面区块(Kana Supplement / Extended-A / Extended-B / Small Kana Extension)
# 收进来是【同一个理由】,不是顺手:U+1B002 起整段是変体仮名,和刻本古籍满篇都是它,
# 而原表只画了 BMP 里的现代平/片假名两段。全量核对时它们正是漏在表外的 44 个码点。

IDEOGRAPH_BLOCKS = (
    (0x03400, 0x04DBF, "CJK Unified Ideographs Extension A"),   # 扩展A(原有)
    (0x04E00, 0x09FFF, "CJK Unified Ideographs"),               # 统一表意(原有)
    (0x0F900, 0x0FAFF, "CJK Compatibility Ideographs"),         # 兼容表意 﨑塚
    (0x20000, 0x2A6DF, "CJK Unified Ideographs Extension B"),
    (0x2A700, 0x2B73F, "CJK Unified Ideographs Extension C"),
    (0x2B740, 0x2B81F, "CJK Unified Ideographs Extension D"),
    (0x2B820, 0x2CEAF, "CJK Unified Ideographs Extension E"),
    (0x2CEB0, 0x2EBEF, "CJK Unified Ideographs Extension F"),
    (0x2EBF0, 0x2EE5F, "CJK Unified Ideographs Extension I"),
    (0x2F800, 0x2FA1F, "CJK Compatibility Ideographs Supplement"),
    (0x30000, 0x3134F, "CJK Unified Ideographs Extension G"),
    (0x31350, 0x323AF, "CJK Unified Ideographs Extension H"),
)

# 【刻意没收进来的,连同理由一起写在这,免得下一轮再考古一遍】
#   U+2FF0-2FFF 表意文字描述符(⿰⿱⿲):它描述的是"一个 Unicode 里没有的字长什么样",
#     本身不是字,性质更接近 ocr_quality._REPEAT_MARKS 里的缺字方框 □。本仓至今零实测样本,
#     按本仓规矩(门槛/覆盖只站在实测样本上,不向外推)不收。
#   U+31C0-31EF 汉字笔画(㇀㇁):同样零样本。
#   U+3000-303F CJK 符号与标点:那是【标点】,归 ocr_quality._CJK_PUNCT / _REPEAT_MARKS 管
#     (〇 U+3007、々 U+3005、〃 U+3003 都在那两张表里);收进来会把一页满是「。、《》」
#     的文本的 cjk_ratio 抬高,反过来动到 cjk_low 与背靠背豁免的判决,不是本轮的事。
#   U+AC00-D7AF 谚文音节:韩文古医籍写的是汉字(已在统一表意区内),谚文不是表意文字,
#     收它会把 cjk_ratio 的语义从"表意文字占比"改掉。

# 合并表。刻意保持 (lo, hi, name) 三元组:ocr_quality._CJK_BLOCKS 就是它,
# 而 tests/test_ocr_quality.py 是按三元组解包遍历的 —— 加第四个字段会当场炸测试,
# 分组信息放在上面三个常量里表达,不塞进元组。
CJK_BLOCKS = tuple(sorted(RADICAL_BLOCKS + KANA_BLOCKS + IDEOGRAPH_BLOCKS))
HAN_BLOCKS = tuple(sorted(RADICAL_BLOCKS + IDEOGRAPH_BLOCKS))


def char_class(blocks):
    """把区块表拼成正则字符类的【类体】(不含外面那对方括号)。

    公开出来是有原因的:调用方要么用下面三个现成 pattern,要么用本函数自己拼 ——
    唯独不许再写一行字面量区间。本模块存在的全部理由就是不再有第二份字面量。
    """
    return "".join("\\U%08X-\\U%08X" % (lo, hi) for lo, hi, _name in blocks)


CJK_TEXT = re.compile("[" + char_class(CJK_BLOCKS) + "]")
# 汉字 + 假名。"这个字符是不是正常内容" 走这条(ocr_quality 的 4 条判据共用)。

HAN = re.compile("[" + char_class(HAN_BLOCKS) + "]")
# 汉字(表意 + 部首),【不含假名】—— 这个排除是承重的,不是口味问题。
#   ocr_degeneracy.CJK_MIN=0.80 的原话是"catches pages that came back entirely in
#   kana";把假名收进 HAN,整页假名的 cjk 立刻变成 1.00,那条判据当场失明,
#   而它正是当年灌满 tcm-rag-768 那 1828000 条垃圾向量的两种失败模式之一。
#   tests/test_cjk_charset.py 与 tests/test_ocr_degeneracy.py 各钉了一条守着它。

KANA = re.compile("[" + char_class(KANA_BLOCKS) + "]")
# 只有假名。diagnose_bad_ocr 用它分流"日文汉方(含假名)"。
