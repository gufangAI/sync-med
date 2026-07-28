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
# 检测判据 1)~6)9) 是语言无关的比例/重复数学;7) 是绝对计数;8) 是唯一一条看字面的:
#   1) repeat_ngram   — 最高频 n-gram(n=2..8)占全文比例:短语刷屏的核心症状
#   2) max_run        — 某周期 p 的连续复读(back-to-back)最长游程占比:"A后A后A后"
#   3) line_dup       — 去空白后重复行占比:整行/整块复读
#   4) garbage_ratio  — 既非 CJK、非 CJK 标点、非 ASCII 可打印、非版面记号的"乱码"占比(含 U+FFFD/控制符)
#   5) single_char    — 单一最高频【内容】字符占比:单字符刷屏(。。。。 / oooo)
#   6) cjk_ratio      — CJK 占比(软信号:封面/牌记/西文页天然低,不单独判死)
#   7) abs_repeat     — 同一片段【背靠背连出】的绝对次数(不是占比):全长度生效
#   8) model_meta     — OCR 模型自己的元话术("图中包含的文字内容是…"):短页专用
#   9) mark_ratio     — 版面记号占比:整页只剩缺字方框/点线/同上记号 = 这页没有内容
#
# 每条判据都有自己的最短生效长度,短于它就不出判决(比例在小分母上没有判别力):
#   1)2)3) -> MIN_LEN_FOR_REPEAT(=LONG_PAGE 40)   4)5)9) -> MIN_LEN_FOR_RATIO(20)   6) -> LONG_PAGE(40)
#   7) 没有长度门:次数不吃分母,长短页一样有判别力(2026-07-28 由短页专用扩到全长度)。
#   8) 反过来:【只在】 n < MIN_LEN_FOR_REPEAT 时生效,专门补 1)2)3) 让开的那一档。
#
# 7) 还兼一份【纠错】职责:1)2) 是比例,分不开"药名+剂量反复出现"(方剂组成页,正货)
#   与"模型卡住复读"(垃圾)——两者比例长得一模一样;而 7) 把两堆分得干干净净
#   (正货背靠背连出恒 1~2,复读恒 >=6)。所以 7) 极低时会把 1)2) 的判决豁免掉,
#   详见 ABS_REPEAT_CLEAN。
#
# opsec:1)~7) 不含可读 CJK 字面量,原文片段一律 unicode_escape 后才进返回值。
#   8) 必须写中文字面量(它要认的就是那几句现代汉语),按本仓已有先例办
#   ——ocr_recover_scan.py 的 _MARKERS 同样直接写「兩」「錢」「湯」等词典词,
#   opsec 要挡的是把 OCR 出来的【原文】回吐进 public 仓,不是挡词典词。
#   命中时 reasons 只报标记的【序号】(model_meta=k3),不回吐命中的原文。
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
_ASCII_ALNUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
# 版面记号:这些字符成串出现是【排版/缺字标记】,不是模型复读,也不是乱码。
#   〇 ○ ● □ ■ = 缺字/漫漶方框;… ‥ = 目录点线;・ = 假名分隔点;
#   ヽ ヾ ゝ ゞ 々 〃 = 叠字/同上记号(和刻本表格里成列出现);ー ― — – = 长音符/横线;
#   ◇ ◆ △ ▲ ▽ ▼ ※ = 校勘/注记符号。
# 不排掉它们,一页「傷寒論………………三」的目录就会因为 12 个点线被判成复读。
# 注意 ヽ ゝ ・ ー ヾ ゞ 落在 _CJK_RE 的假名区间里,不显式排除就会被当成"内容字符"。
#
# 【本表是 4 条判据共用的,名字里的 REPEAT 只是它最早的用途,别按名字理解范围】
#   abs_repeat(判据 7,经 _unit_has_content)/ garbage_ratio(4)/ single_char(5)/ mark_ratio(9)。
#
# 2026-07-28 补 garbage/single_char:在此之前本表与 _CJK_PUNCT 【两张表互相不认】,
# 后果是同一个字符在两条判据里身份相反 —— □ 和 々 在本表里是"版面记号"(abs_repeat 放过),
# 在 _CJK_PUNCT 里查无此字(garbage_ratio 当乱码),而 〇 两张表都有所以没事。
# 实测这条不一致把影印善本判死:古籍影印本用 □ 标注漫漶缺字,□ 一过半整页就被 garbage 判死。
#     缺字方框页 33 字(□×22)  -> reject  garbage=0.66 single_char=0.66
#     长缺字方框页 47 字(□×30) -> reject  garbage=0.64 single_char=0.64
#   (背靠背豁免 scattered_repeat 早已生效,拦住了 repeat_ngram/max_run,拦不住这两条。)
# 差分扫描(占比 0.05→1.00 × 页长 30/47/90/200 × 散点/小段/整列三种糊法,240 条):
#   □ 占比 >=0.50 一律 reject、>=0.30 一律 suspect,与页长和糊法完全无关
#   —— 判死的是 garbage_ratio 这个逐字符计数,不是任何"重复"信号。
# 越是珍稀的漫漶善本越容易中招,而影像善本正是平台区别于纯文本站的核心资产。
_REPEAT_MARKS = set("〇○●□■◇◆△▲▽▼※…‥・ヽヾゝゞ々〃ー―—–")

# ---- 阈值(模块常量,便于调参)----
REPEAT_NGRAM_REJECT = 0.50   # 最高频 2..8-gram 占全文 >=50% -> 短语刷屏,判死
REPEAT_NGRAM_SUSPECT = 0.32
# repeat_ngram 扫到的最长 n-gram。原先是 repeat_ngram() 里写死的 range(2, 9),
# 提成常量不是为了好看:ABS_REPEAT_MAX_UNIT 是从它算出来的,写死在两处就会各自漂移。
REPEAT_NGRAM_MAX_N = 8
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

# ---- 判据 9:整页只剩版面记号 = 这页没有内容 -------------------------------
# 为什么必须显式立这一条,而不是"放松完就完事":放松 garbage/single_char 会亲手开一个洞。
# _REPEAT_MARKS 里的 ヽ ヾ ゝ ゞ ・ ー 六个记号落在 _CJK_RE 的假名区间内,
# 于是一页纯 ヽ 的 cjk_ratio = 1.00 —— 它同时骗过三道闸:
#   garbage 认它是 CJK(本来就 0.00)、cjk_low 认它是中日文正文、
#   背靠背豁免的第二道闸 cjk >= CJK_SUSPECT 也照样过(abs_repeat 又因为它是记号恒报 1)。
# 实测改前/改后(「ヽ」×60):
#   改前 single_char=1.00 -> reject   ← 整页纯记号,唯一按住它的就是 single_char
#   若只放松不补这一条     -> ok       ← 一页零信息的记号页直接进燃料池
# 另外五个(ゝ ヾ ゞ ・ ー)同款。
#
# ★ 这一条是【承重的】,不是补丁 —— 这句话是实测推翻我自己的估计之后才写下的。
#   我原以为非假名记号(□ ○ ● ■)不吃这个洞、判据 9 只多管假名区那十来条;
#   拿"只放松、不加判据 9"的版本对 518 条语料逐条比对,实际是 **32 条会漏放**,横跨三类:
#     ① 假名区记号刷屏 13 条 —— cjk_ratio 虚高,cjk_low 与背靠背豁免同时失灵(上面那个洞)
#     ② 20~40 字的短记号页 12 条(□ 占比 0.85/0.90/0.95/1.00 × 30 字 × 三种糊法)
#        —— 重复类判据在 MIN_LEN_FOR_REPEAT(40)以下【完全不生效】,那一段本来就没有兜底
#     ③ 记号占比【恰好】= 0.85 的长页 7 条(□ ■ ○ ● ※)—— 此时 cjk_ratio 正好 0.15,
#        豁免的第二道闸写的是 `cjk >= CJK_SUSPECT`,取等号照样放行,重复判据被静音
#   ②③ 两类都不是假名记号的问题,是"把判决托付给别的判据碰巧还够得着"本身就靠不住。
#
# ★ 门槛怎么定的:它【不是一个新数】,是 CJK_SUSPECT 的补集,写成表达式不留可漂移的副本。
#   模块里 CJK_SUSPECT=0.15 的既有含义就是"CJK 低于 15% = 这页基本没有中日文内容"
#   (cjk_low 那条 suspect 用的就是它)。记号占比 >= 1-0.15 意味着页面里【最多】15%
#   还是别的东西(含真正的内容字),正是同一句话反过来说。
#   死线两侧最贴近的实测样本(240 条占比扫描,0.05→1.00 逐格):
#     放行侧最高 mark_ratio = 0.8440 -> ok      判死侧最低 mark_ratio = 0.8500 -> reject
#   即门槛落在实测样本之间,不在任何一堆样本内部。
# ★ 代价说清楚(必须说,因为它是真的):占比落在 [0.70, 0.85) 的页会放行,
#   也就是一页 84% 是缺字方框、只剩 16% 真字,照样进燃料池。这是刻意选的方向:
#   漫漶善本本来就是最珍稀的一档,宁可让 16% 的真字进池,不可再误杀整页。
#   放行不等于没人看见:命中时 reasons 会留 layout_marks=,日审报告的 top_reasons 里看得到。
# ★ 为什么不顺手加一条"记号偏多"的 suspect:那会把 0.53 的目录点线页、和刻本同上记号页
#   从 ok 翻成 suspect,而任务判据要的是"正常古籍页判定零改动"。留痕用 reasons,不用 label。
MARK_RATIO_REJECT = 1.0 - CJK_SUSPECT   # = 0.85

# 重复类三条判据(repeat_ngram / max_run / line_dup)的最短生效长度。
# 取模块自己已有的 LONG_PAGE,不另立新数——本仓吃过"拍脑袋定阈值"的亏
# (FALLBACK_BASELINE 的 distinct=300 误杀过成无己《註解傷寒論》),
# 多一个能独立漂移的常量就是多一个会咬人的门槛。
#
# 为什么必须有这道下限:2026-07-28 之前【只有这三条没有长度下限】
# (garbage / single_char 有 MIN_LEN_FOR_RATIO,cjk 有 LONG_PAGE)。
# 页一短,正常的结构化中文靠巧合就撞阈值:比例的分母太小,一点点重复就占满全页。
# 实测(判据一个没改,只换文本长度):
#     桂枝湯组成          26 字 -> max_run=0.46      判退
#     六君子湯组成        24 字 -> repeat_ngram=0.50 判退
#     目录卷之一…卷之十   30 字 -> repeat_ngram=0.67 判退
#     傷寒論正文          86 字 -> 0.09 / 0.15       放行
#     真·短语刷屏        168 字 -> 0.57             判退
# 判别力在正常长度的页上完好,只在短页上塌掉。而被误杀的恰恰是方剂组成页——
# 药方库与 AI 寻脉「古籍出处」的原始来源,平台最核心的燃料。
# 线上实证:2026-07-27 抽样审计 199 页,唯一一条 reject 是 _ocr/bcgmj21/page_0007.txt,
# 清洗后 28 字的本草药名著录页(max_run=0.46, p=12)——不是垃圾,是正货。
#
# 代价说清楚:加下限的方向是【放松】,40 字以下"只靠短语重复才认得出"的短垃圾页
# 会漏进燃料池。兜底还在:garbage / single_char 从 20 字起照常生效,
# 乱码页与单字符刷屏页仍然判死。权衡是清楚的——漏几十字噪音 vs 丢掉方剂组成页。
#
# 与 ocr_degeneracy.MIN_CHARS 同一个思路:样本太小就不给判决,而不是给一个坏判决。
# 2026-07-28 之前这道限长只在 ocr_ndl.py 里有一份(GATE_MIN_CHARS),
# 另外 4 条线裸奔;收进本模块后 5 条线一致,不再有第二个会漂移的副本。
MIN_LEN_FOR_REPEAT = LONG_PAGE

# ---- 背靠背连出次数(判据 7)与元话术(判据 8)-----------------------------
# 判据 7 最初是"短页专用"(补 MIN_LEN_FOR_REPEAT 让开的那一档),2026-07-28 扩到全长度,
# 因为 40 字那条线把方剂组成页劈成了两半:短的救了,长的照样被比例判据误杀。
#   八珍湯组成    39 字 -> ok       走短页路径
#   十全大補湯    49 字 -> reject   走长页路径,被 repeat_ngram=0.50 判死
# 十全大補湯、八味丸这类大方随便就超 40 字,不是边缘案例,是药方库的主力。
# 而背靠背连出在长页上同样把两堆分得干干净净(正货恒 1~2,复读恒 >=6),
# 说明错的是【比例判据】,不是页面本身。扩过去之后 7) 同时干两件事:
#   ① 判死:背靠背连出 >= ABS_REPEAT_REJECT 的页,任何长度都判死
#      (顺带补上原先自报的"代价②":长页里的局部复读,占比够不着但次数够得着)
#   ② 豁免:背靠背连出 <= ABS_REPEAT_CLEAN 的页,把 1)2) 的判决降级掉(见下)
#
# 判据 8 仍然只在短页生效,理由没变,见 MODEL_META_MARKERS 上方 ②。
#
# 下面这段是判据 7 当初的立据,原样保留:
# 上面那道下限方向是对的,但"低于 40 字就完全不判"太粗:2026-07-28 run 30339783256
# 全量扫描 86 个历史判退页,11 页因这道下限从 reject 翻成 ok,其中大部分本来就该判退
# (「鶯鳥」连出 12 次 24 字、「鵝鶚」连出 9 次 18 字、「上」连出 8 次 8 字)。
#
# 短页不能用【占比】,但可以用【绝对次数】—— 占比的分母一小就没判别力,次数没有这个毛病。
#
# ★ 关键:次数必须数"背靠背连出"的,不能数"任意位置出现"。这一条是实测逼出来的,
#   不是推理出来的。同一批语料上两种数法的分离度:
#                                      任意位置出现次数   背靠背连出次数
#     八珍湯组成 32 字(「一錢」×8)          8            1     ← 正货
#     桂枝湯组成 26 字                       3            1     ← 正货
#     本草药名著录 28 字                     2            1     ← 正货
#     「上」连出 8 次(8 字)                 7            8     ← 复读
#     「鵝鶚」连出 9 次(18 字)              9            9     ← 复读
#     「鶯鳥」连出 12 次(24 字)            12           12     ← 复读
#   按"任意位置出现 >=8 次"判死,第一行的八珍湯组成页当场被误杀——方剂组成页里
#   剂量单位天然重复(一錢/三兩/各等分),十全大補湯能到 10 次。那正是上一轮拼命救回来的页。
#   改数背靠背连出,正货全部塌到 1,复读全部 >=8,两堆彻底不重叠。
#
# ★ 阈值 8 怎么定的(数据在前,数字在后):420 条语料(413 条构造页 + 7 条线上真实样本)
#   跑下来,短页(<40 字)里——
#     正货侧 abs_repeat 最大值 = 2   (167 条短页正货里 166 条 =1,唯一的 2 是「上上」这个 2 字页本身)
#     判退侧 abs_repeat 最小值 = 8   (「上」连出 8 次那条)
#     中间 3..7 一条样本都没有
#   取 8 = 判退侧【实测到的最小恶性值】,不向下外推。为什么不取 6(正货侧仍有 3 倍余量)?
#   因为 3..7 这一段我一条样本都没有,取 6 就是拿没证据的数当门槛——本仓正是这么被咬过的
#   (FALLBACK_BASELINE 的 distinct=300 误杀成无己《註解傷寒論》)。
#   代价说清楚:连出 3~7 次的短复读现在会漏过去。等判退台账(ocr_reject_log 已把
#   abs_repeat 一起记下)攒出这一段的真实样本,再拿样本把门槛往下挪,而不是现在猜。
ABS_REPEAT_REJECT = 8
# 门槛 8 在长页上同样站得住(2026-07-28 扩到全长度时复测,123 条语料):
#   长页正货侧 abs_repeat 最大 = 2;长页判退侧最小 = 6(6 条整行复读页,
#   它本来就被 line_dup=0.83 + repeat_ngram=0.50 两条判死,不靠这条)。
#   其余长页垃圾全在 8..300。3..5 依旧一条样本没有,门槛不动、不向下外推。

# 单元长度上限。旧值 4 是"短页专用"时代定的(理由:再长的片段背靠背连出 8 次要 32 字
# 以上,已越过 40 字长度门,归 max_run / repeat_ngram 管)。判据扩到长页后这条理由
# 不再成立,而且恰恰反过来——长单元的复读会同时骗过旧 abs_repeat 和肉眼。实测:
#     「傷寒論卷之」×15 (75 字)  旧 abs_repeat = 1
#     「太陽之爲病脉浮頭」×10 (80 字) 旧 abs_repeat = 1
#     「太陽之爲病脉浮頭項強痛而惡寒太陽」×8 (128 字) 旧 abs_repeat = 2
#   它们全是模型卡住复读,旧值一律报"没有重复"。这一档如果拿去做豁免判据,
#   就会把这三页从 reject 放行进燃料池 —— 豁免比判死更需要这个上限够宽。
#
# 新值 25 是【算出来的,不是拍的】:一个 u 字单元背靠背铺满整页时,repeat_ngram
# 最高只能到 min(REPEAT_NGRAM_MAX_N, u)/u;要够得着它最松的一档 REPEAT_NGRAM_SUSPECT,
# 就得 8/u >= 0.32,即 u <= 25。单元比 25 还长的背靠背复读,比例判据本来就一句话不说,
# 豁免也就没有"豁错"的可能 —— 上限取 25 正好把"比例判据会开口"的区间盖满,不多不少。
# 写成表达式而不是字面量 25:阈值一改这里跟着走,不留第二个会漂移的副本。
#
# 下限仍取 1 而不是 2:实测样本「上」连出 8 次,按 2 字单元「上上」数只有 4 次,
# 够不到门槛;单字连出正是最常见的一种复读。
#
# 加宽【不动短页的判死行为】,这是算出来的不是跑出来的:rep = run // p + 1 >= 8
# 要求 run >= 7p,而 run <= L - p,故 L >= 8p。L < 40 时只有 p <= 4 够得着门槛 8,
# 与旧上限完全一致。加宽只会让短页的 abs_repeat 报得更准(进台账),不改判决。
# 代价是扫描量 O(25·L):2000 字的页 5 万次字符比较,实测毫秒级。
ABS_REPEAT_MAX_UNIT = int(round(REPEAT_NGRAM_MAX_N / REPEAT_NGRAM_SUSPECT))   # = 25

# 「背靠背连出次数低到这个数以内」= 这一页里根本没有复读。
# 此时 repeat_ngram / max_run 看到的"重复"是【散布】的 —— 方剂组成页里药名与剂量单位
# 天然反复出现(一錢/三兩/各等分),十全大補湯的「一錢」能占到全页一半,比例判据当场
# 判死,而它一次背靠背都没有。比例分不开的,背靠背分得开,所以让背靠背去纠比例的错。
#
# 取 2 = 【正货侧实测到的最大值】,不向上外推。123 条语料的长页(n>=40)上:
#     正货侧 abs_repeat 最大 = 2   (傷寒論正文/序跋 60 字以上偶尔 =2,方剂组成页恒 =1)
#     判退侧 abs_repeat 最小 = 6   (整行复读 6 行那条)
#     中间 3..5 一条样本都没有
# 与 ABS_REPEAT_REJECT 正好是一对:那个取判退侧实测最小值(8),这个取正货侧实测
# 最大值(2),中间无样本区 3..7 两边都不碰 —— 既不豁免也不判死,维持比例判据原判。
# 本仓被"拿没证据的数当门槛"咬过(FALLBACK_BASELINE 的 distinct=300 误杀成无己
# 《註解傷寒論》),两个门槛都只站在自己那侧的实测极值上。
#
# ★ 豁免还有第二道闸:必须 cjk_ratio >= CJK_SUSPECT。理由是实测逼出来的,不是谨慎:
#   替换符乱码页、纯 ASCII 符号刷屏页的复读单元不含 CJK/字母数字,被 _unit_has_content
#   挡在计数外,abs_repeat 报 1 —— 光看"背靠背=1"会把它们当正货放行。而它们的
#   cjk_ratio 恒为 0.00,方剂组成页恒为 1.00,两堆零重叠。语义上也对得上:
#   "重复是散布的药名剂量"这个说法,只对【确实是中日文正文页】的页成立。
#   复用已有的 CJK_SUSPECT,不另立新数。
ABS_REPEAT_CLEAN = 2

# OCR 模型的「元话术」:模型没读出字时,不是返回空,而是用现代汉语描述这张图
# (线上实测样本:一整页只有「图中包含的文字内容是日文。」13 字)。
# 这不是识别结果,是模型在说话——它进了燃料池,SueAI 就会把它当成古籍原文引用。
# 重复类判据永远抓不到它(它一个字都不重复:abs_repeat=1),只能单独认。
#
# 为什么误伤风险低(证据,不是感觉):
#   ① 标记全部 >=3 字,而且是【整句现代汉语】(「图中包含」「文字内容是」「我无法」)。
#      单字不行——讯飞 HunyuanOCR 会把繁体字形归一化成简体,单看「图」会冤枉真古籍页;
#      但字形归一化只改字形、不会凭空造出这样的词序和内容,整句短语因此是安全的。
#   ② 只在 n < MIN_LEN_FOR_REPEAT(40 字)时生效。够长的页 = 模型确实读出了东西,
#      哪怕尾巴上缀了一句元话术也该保住正文,而不是整页判死。
#   ③ 420 条语料实跑:20 条元话术页标记全中(其中 10 条短页判死、10 条长页按 ② 放行),
#      250 条正货语料(长页短页都算)零命中。
# 覆盖不到的(诚实标注):纯日语元话术(「画像には文字が含まれ…」)没有实测样本,没写;
#   40 字以上的长篇道歉("抱歉,我无法识别…这可能是因为…建议您…")按 ② 放行。
# 入选标准(卡死,别往里加顺手的短词):>=3 字,且脱离图片语境就说不通。
# 「图书」「无法」「内容」这种词本身能出现在真书里,一律不收。
MODEL_META_MARKERS = (
    "图中包含", "图中的文字", "图中显示", "图中没有",
    "图片中的文字", "图片中没有", "图片中包含", "该图片", "这张图", "这幅图",
    "文字内容是", "文字内容如下", "以下是图",
    "无法识别", "无法辨认", "无法提取", "未能识别", "没有可识别",
    "不包含任何", "我无法", "作为AI", "作为人工智能",
)


def _clean(text):
    return re.sub(r"\s", "", text or "")


def cjk_ratio(text):
    """CJK 占比。沿用 ocr_ndl.py 实测:垃圾幻觉块 CJK 占比恒为 0,真实古籍/漢方恒接近 1.0。"""
    t = _clean(text)
    if not t:
        return 0.0
    return len(_CJK_RE.findall(t)) / len(t)


def garbage_ratio(text, count_marks=False):
    """既非 CJK、非 CJK 标点、非 ASCII 可打印、非版面记号的字符占比。U+FFFD/控制符计为乱码。

    版面记号(□ 缺字方框 / … 目录点线 / 々〃 同上记号)是【排版】不是乱码 —— 这是
    2026-07-28 的修正,在此之前 _REPEAT_MARKS 里 16 个记号在这里被逐字符计成乱码,
    漫漶影印本一过半就整页判死(详见 _REPEAT_MARKS 上方的实测)。

    count_marks=True = 恢复旧口径(记号算乱码),【只给考古用】:
    ocr_recover_scan.old_gate_view 要回答的是"当年那一版闸会不会误杀它",
    它必须拿到当年的数,不能拿改后的数去替当年作答。产线一律用默认值。
    只加一个开关而不复制一份函数:本仓吃过"同一判据两份副本各自漂移"的亏。
    """
    t = _clean(text)
    if not t:
        return 0.0
    bad = 0
    for ch in t:
        if not count_marks and ch in _REPEAT_MARKS:
            continue
        if _CJK_RE.match(ch) or ch in _CJK_PUNCT or ch in _ASCII_OK:
            continue
        bad += 1
    return bad / len(t)


def single_char_ratio(text, count_marks=False):
    """单一最高频【内容】字符占全文比例(单字符刷屏)。

    分子只在版面记号之外的字符里取最高频,分母仍是全页长度 —— 这样这个数只会变小、
    不会变大,不可能因为本次放松而新判死任何一页(纯放松,零新增误杀)。
    为什么要排掉记号:一页 □ 占 66% 的漫漶页,最高频字符就是 □,0.66 直接够着
    SINGLE_CHAR_REJECT=0.60 —— 但"缺字方框刷屏"根本不是模型刷屏,是原书就糊了。
    整页只剩记号的那一档不靠本函数拦,靠判据 9 MARK_RATIO_REJECT 显式拦(见其注释)。

    count_marks=True 同 garbage_ratio:旧口径,只给考古用。
    """
    t = _clean(text)
    if not t:
        return 0.0
    body = t if count_marks else [ch for ch in t if ch not in _REPEAT_MARKS]
    if not body:
        # 整页全是版面记号:内容侧一个字符都没有,这里诚实报 0 而不是 1.0
        # (它该由判据 9 按 mark_ratio 判死,不该借单字符刷屏的名义判)。
        return 0.0
    return Counter(body).most_common(1)[0][1] / len(t)


def mark_ratio(text):
    """版面记号占全页比例(判据 9)。高到一定程度 = 这页只剩排版符号,没有内容。
    与 garbage_ratio / single_char_ratio 共用 _REPEAT_MARKS 一张表,不另立字符表。"""
    t = _clean(text)
    if not t:
        return 0.0
    return sum(1 for ch in t if ch in _REPEAT_MARKS) / len(t)


def repeat_ngram(text):
    """n=2..8 中最高频 n-gram 覆盖全文的最大占比。返回 (ratio, n, unit_escaped)。"""
    t = _clean(text)
    n_total = len(t)
    if n_total < 6:
        return 0.0, 0, ""
    best, best_n, best_unit = 0.0, 0, ""
    for n in range(2, REPEAT_NGRAM_MAX_N + 1):
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


def _is_content_char(ch):
    """这个字符算不算"内容":CJK 表意字/假名 或 ASCII 字母数字,且它自己不是版面记号。
    判据 7(_unit_has_content)与判据 9(content_len)共用这一条 —— 本仓吃过
    "同一判据两份副本各自漂移"的亏,"什么叫内容字"只许有一个定义。

    已知盲区(诚实标注,本轮不改):_CJK_RE 只覆盖 CJK 统一表意 + 扩展A + 假名。
    扩展B/C(U+20000+)、兼容表意(U+F900-FAFF)、康熙部首(U+2F00-2FDF)都不算内容字
    —— 而古籍里的生僻药名/人名真会用到它们。这【不是本函数引入的】:garbage_ratio
    今天就已经把这些字逐个计成乱码(实测在正常古籍页里掺入 >=50% 即整页 reject、
    >=30% 即 suspect),本函数只是沿用同一个既有盲区,没有把它扩大。
    根子和本轮修的是同一类病:字符覆盖表画窄了,真汉字被当成乱码。"""
    if ch in _REPEAT_MARKS:
        return False
    return bool(_CJK_RE.match(ch)) or ch in _ASCII_ALNUM


def _unit_has_content(u):
    """这个复读单元算不算"内容"。至少要有一个 CJK 表意字/假名或 ASCII 字母数字,
    且它自己不是版面记号。挡的是目录点线、缺字方框、和刻本的同上记号成串出现
    ——那些在真古籍页上本来就连成排,不该被当成模型复读。"""
    return any(_is_content_char(ch) for ch in u)


def content_len(text):
    """去掉版面记号后还剩几个【内容】字符(绝对个数,不是占比)。
    占比吃分母,个数不吃 —— 这一条与 abs_repeat 是同一个思路(见 ABS_REPEAT_REJECT)。"""
    return sum(1 for ch in _clean(text) if _is_content_char(ch))


def abs_repeat_run(text):
    """最长"同一片段背靠背连出"的【绝对次数】。返回 (repeats, unit_len)。

    与 max_run_ratio 是同一个原语(周期 p 上 s[i]==s[i+p] 的连续段),差别只在
    返回次数而不是占比——占比吃分母(短页分母太小、长页分母太大),次数不吃。
    刻意复用同一套游程逻辑,不另造一份数法:本仓吃过"同一判据两份副本各自漂移"的亏。
    没有任何重复时返回 1(= 这个片段只出现了它自己那一次),不是 0。

    注意周期上限:本函数扫 p=1..ABS_REPEAT_MAX_UNIT(25),max_run_ratio 扫 p=1..12。
    两者不一样是【故意的】——max_run 是占比,长单元的复读它靠 p<=12 那一段已经能看见;
    本函数要给豁免当依据,必须把"比例判据会开口"的单元长度全盖住,漏一档就漏一页垃圾。
    """
    t = _clean(text)
    L = len(t)
    if L < 2:
        return 1, 0
    best_rep, best_p = 1, 0
    for p in range(1, ABS_REPEAT_MAX_UNIT + 1):
        if L < p * 2:
            break
        run = 0
        start = 0
        i = 0
        while i + p < L:
            if t[i] == t[i + p]:
                if run == 0:
                    start = i          # 这一段周期性复读从 t[start] 起,单元 = t[start:start+p]
                run += 1
                # run 个字符的连续匹配 = run//p 个完整复读单元,再加上打头的那一个
                rep = run // p + 1
                if rep > best_rep and _unit_has_content(t[start:start + p]):
                    best_rep, best_p = rep, p
            else:
                run = 0
            i += 1
    return best_rep, best_p


def model_meta_hit(text):
    """命中的元话术标记序号,没命中返回 -1。
    返回序号而不是命中的字符串:reasons 会被印进 public 仓的 run log,
    不能把页面原文回吐出去(ocr_ndl.py 那句"reasons 不含原文片段"要继续成立)。"""
    t = _clean(text)
    for i, m in enumerate(MODEL_META_MARKERS):
        if m in t:
            return i
    return -1


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
        # 键必须和下面正常返回的那份【完全一致】:ocr_recover_scan.py 是直接 a["xxx"] 索引的,
        # 少一个键就是一次 KeyError。新增字段同样要在这里补齐。
        return {"len": 0, "label": "empty", "score": 0, "reasons": ["empty"],
                "cjk_ratio": 0.0, "garbage_ratio": 0.0, "single_char": 0.0,
                "repeat_ngram": 0.0, "repeat_n": 0, "repeat_unit": "",
                "max_run": 0.0, "run_period": 0, "line_dup": 0.0, "n_lines": 0,
                "abs_repeat": 0, "abs_repeat_unit": 0, "model_meta": -1,
                "mark_ratio": 0.0, "content_len": 0,
                "garbage_ratio_raw": 0.0, "single_char_raw": 0.0}

    cjk = cjk_ratio(text)
    garbage = garbage_ratio(text)
    single = single_char_ratio(text)
    mark = mark_ratio(text)
    n_content = content_len(text)
    # 旧口径(版面记号算乱码/算刷屏)的同名两条,只测不判:考古专用。
    # ocr_recover_scan.old_gate_view 问的是"当年会不会误杀",它必须拿当年的数作答;
    # 判退台账攒下来之后要回看"这一页当年是被哪一条按住的",也只能靠这两个数。
    garbage_raw = garbage_ratio(text, count_marks=True)
    single_raw = single_char_ratio(text, count_marks=True)
    rep, rep_n, rep_unit = repeat_ngram(text)
    run, run_p = max_run_ratio(text)
    ldup, nlines = line_dup_ratio(text)
    arep, arep_u = abs_repeat_run(text)
    meta_k = model_meta_hit(text)

    reject = False
    suspect = False

    # 重复类三条判据只在页够长时才有判别力(为什么:见 MIN_LEN_FOR_REPEAT 上方注释)。
    # reject 与 suspect 一起挡:短页上这三个比例本身就没有意义,退一步给 suspect
    # 同样是错的判决——审计报告的"非空异常率"会把正常方剂页计成异常,那是另一种虚惊。
    # 注意只挡【判决】不挡【测量】:rep / run / ldup 的原值照旧算、照旧返回,
    # 诊断信息一条不少,调用方要自己看比例随时能看。
    long_enough = n >= MIN_LEN_FOR_REPEAT

    # 「散布型重复」:这一页一次背靠背都没有(见 ABS_REPEAT_CLEAN),而且它确实是
    # 中日文正文页。此时 repeat_ngram / max_run 量到的重复来自药名与剂量单位反复出现,
    # 不是模型卡住 —— 比例分不开正货和垃圾,背靠背分得开,让背靠背纠比例的错。
    # 只纠 repeat_ngram / max_run 这两条【量同一件事(片段重复)】的,不动 line_dup:
    # 整行一模一样是字面重复,方剂组成页每行药名都不同,它不会被这个机制冤枉。
    scattered = arep <= ABS_REPEAT_CLEAN and cjk >= CJK_SUSPECT
    repeat_judgable = long_enough and not scattered

    if repeat_judgable and rep >= REPEAT_NGRAM_REJECT:
        reject = True; reasons.append(f"repeat_ngram={rep:.2f}(n={rep_n})")
    elif repeat_judgable and rep >= REPEAT_NGRAM_SUSPECT:
        suspect = True; reasons.append(f"repeat_ngram~{rep:.2f}")

    if repeat_judgable and run >= MAX_RUN_REJECT:
        reject = True; reasons.append(f"max_run={run:.2f}(p={run_p})")
    elif repeat_judgable and run >= MAX_RUN_SUSPECT:
        suspect = True; reasons.append(f"max_run~{run:.2f}")

    # 豁免真的生效时(= 比例本来要开口了)才留痕,否则每一页干净长页都会带上这条,
    # 审计报告的 top_reasons 会被刷屏。留痕是必须的:豁免是本模块唯一一条【放行】
    # 逻辑,它要是哪天放错了,得有人能在日审报告里看见它放了多少页。
    if long_enough and scattered and (rep >= REPEAT_NGRAM_SUSPECT or run >= MAX_RUN_SUSPECT):
        reasons.append(f"scattered_repeat={arep}")

    if long_enough and nlines >= LINE_DUP_MIN_LINES and ldup >= LINE_DUP_REJECT:
        reject = True; reasons.append(f"line_dup={ldup:.2f}({nlines}L)")
    elif long_enough and nlines >= LINE_DUP_MIN_LINES and ldup >= LINE_DUP_SUSPECT:
        suspect = True; reasons.append(f"line_dup~{ldup:.2f}")

    if n >= MIN_LEN_FOR_RATIO and garbage >= GARBAGE_REJECT:
        reject = True; reasons.append(f"garbage={garbage:.2f}")
    elif n >= MIN_LEN_FOR_RATIO and garbage >= GARBAGE_SUSPECT:
        suspect = True; reasons.append(f"garbage~{garbage:.2f}")

    if n >= MIN_LEN_FOR_RATIO and single >= SINGLE_CHAR_REJECT:
        reject = True; reasons.append(f"single_char={single:.2f}")

    # ── 判据 9:整页只剩版面记号 ────────────────────────────────────────────
    # 长度门用 MIN_LEN_FOR_RATIO,与它上面两条比例判据同一档:短页上占比没有判别力,
    # 而且一页 5 个 □ 的残片今天本来就是 ok,这一条不该把它改掉。
    #
    # 第二个分句(记号之外一个内容字都没有)补的是我自己放松开出来的洞,占比够不着它:
    #     「ヽ」×50 +「。」×10  记号占比 0.83 < 0.85,内容字 = 0
    #   改前 single_char=0.83 判死;只放松不补这一句 -> ok,一页零信息的记号页进燃料池。
    #   假名区记号(ヽヾゝゞ・ー)让 cjk_ratio=0.83 高得离谱,cjk_low 和背靠背豁免
    #   两道闸全被它骗过去,只剩这一句拦得住。非假名记号(□○●■)不吃这个洞
    #   —— 它们 cjk_ratio=0,repeat_ngram/max_run 照样判死(实测「□」×48+「。」×12 = reject)。
    # 它【不是一个新门槛】,是个零判定:一页够长却连一个内容字都读不出来,就是没有内容。
    # 要求 mark > 0 是刻意的:判据 9 只管版面记号这一类,纯句号刷屏页照旧归 single_char 管,
    # 不去抢别条判据的地盘(抢了只会让 reasons 变样,给日审平添一次"指标突变")。
    if n >= MIN_LEN_FOR_RATIO and (mark >= MARK_RATIO_REJECT or (mark > 0 and n_content == 0)):
        reject = True; reasons.append(f"layout_mark_page={mark:.2f}")
    # 豁免留痕(与 scattered_repeat 同一个道理):只在版面记号【确实把这一页从判据 4/5
    # 手里救下来】时才写,否则每一页带一个点线的目录都会挂上这条,日审 top_reasons 被刷屏。
    # 必须留:这是本模块第二条【放行】逻辑,它哪天放错了,得有人在日审报告里看得见。
    elif (n >= MIN_LEN_FOR_RATIO
          and (garbage_raw >= GARBAGE_SUSPECT or single_raw >= SINGLE_CHAR_REJECT)
          and garbage < GARBAGE_SUSPECT and single < SINGLE_CHAR_REJECT):
        reasons.append(f"layout_marks={mark:.2f}")

    # ── 判据 7:背靠背连出次数,【不设长度门】────────────────────────────────
    # 2026-07-28 由 `if not long_enough:` 改成无条件。原先挡在短页那一档是为了让
    # "长页零改动"结构上成立,代价是长页里的【局部】复读全漏(200 字正文里夹 20 字
    # 「鶯鳥鶯鳥…」,占比才 0.11,三条比例判据都够不着 —— 那正是上一轮自报的代价②)。
    # 次数判据不吃分母,长页上和短页上一样准,没有理由只用一半。实测:那条局部复读页
    # 的背靠背连出 = 15,门槛 8 稳稳抓住;而长页正货最大才 2,离门槛还有 4 倍。
    if arep >= ABS_REPEAT_REJECT:
        reject = True; reasons.append(f"abs_repeat={arep}(u={arep_u})")

    # ── 判据 8:元话术,仍然只在短页生效 ────────────────────────────────────
    # 理由没变(见 MODEL_META_MARKERS 上方 ②):够长的页 = 模型确实读出了东西,
    # 哪怕尾巴上缀了一句元话术也该保住正文,而不是整页判死。本轮不动它。
    if not long_enough and meta_k >= 0:
        reject = True; reasons.append(f"model_meta=k{meta_k}")

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
            "line_dup": round(ldup, 3), "n_lines": nlines,
            # abs_repeat 全长度参与判决(2026-07-28);model_meta 仍只在短页判决,
            # 但两者照旧【全长度返回】,下次调门槛能直接在判退台账上重算,不用回 R2 考古
            # (ocr_reject_log 的注释写死了这条:记 ratio 就是为了不用再考古)。
            "abs_repeat": arep, "abs_repeat_unit": arep_u, "model_meta": meta_k,
            # 2026-07-28 新增三个键。返回键只增不改不删 —— 上面 empty 分支那份必须同步补,
            # ocr_recover_scan.py 是直接 a["xxx"] 索引的,少一个键就是一次 KeyError。
            # _raw 两条是旧口径(记号算乱码)的同名值,只测不判,给考古与门槛回算用。
            "mark_ratio": round(mark, 3), "content_len": n_content,
            "garbage_ratio_raw": round(garbage_raw, 3),
            "single_char_raw": round(single_raw, 3)}


def is_reject(text):
    """给产线做闸用的布尔:True = 幻觉/乱码,应退回重跑而非入库。empty 不算 reject。"""
    return analyze(text)["label"] == "reject"


def verdict(text):
    """返回 label 字符串,便于产线分流到 _ocr/ vs _ocr_rejected/。"""
    return analyze(text)["label"]
