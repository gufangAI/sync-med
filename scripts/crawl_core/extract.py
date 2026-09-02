#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crawl_core.extract —— 正文层:任意网页 HTML → 干净正文 / Markdown / 结构化数据。

═══ 吸收来源与许可(逐条注明,这是硬要求)═══════════════════════════════════
① adbar/trafilatura   Apache-2.0  —— **import 使用,不复制代码**
   用它的 BODY_XPATH 五层候选正文定位、prune_unwanted_sections 剪枝、
   以及 xml.py 里那个 CommonMark 安全的 markdown 发射器(代码围栏自适应 /
   内联转义幂等 / 嵌套列表标记 / 管道表格)。
   它已在本仓 scripts/intel_radar/{eagle_fetch,arsenal_enrich,daily_report_v3}.py
   里当依赖用了 —— 所以这里**继续 import,绝不把它的算法抄一份平行实现**。
   本模块对它做的是**两处中文适配**(见 CJK 适配段),不是重写。

② unclecode/crawl4ai  Apache-2.0  —— **移植代码,保留本声明**
   - PruningContentFilter 的五项加权复合打分 + 动态阈值树剪枝
     (源:crawl4ai/content_filter_strategy.py,第 541-806 行 PruningContentFilter)
   - is_data_table 打分制 + colspan 展开的表格抽取
     (源:crawl4ai/table_extraction.py,第 132-296 行)
   - convert_links_to_citations 的 ⟨N⟩ 引用编号 + 文末 References
     (源:crawl4ai/markdown_generation_strategy.py,第 82-146 行)
   移植时把 BeautifulSoup 换成 lxml(全模块共用一棵树,不为第二套算法再解析一遍),
   并做了中文适配与一处 link_text 口径修正,每处都在下方注释里写明「改了什么、为什么」。
   © unclecode & contributors,Apache-2.0,允许复制修改,保留本声明。

③ firecrawl/firecrawl AGPL-3.0(传染)—— **只借鉴架构思路,零行代码搬运**
   借鉴的唯一一点:它的 OMCE(按域名学习到的样板选择器签名)思路 ——
   去噪不必每页现算,可以按域名沉淀"这个站的样板长什么样"复用给同域名后续页面。
   它的实现是 Node + koffi FFI 加载 Go .so + Rust napi + 常驻签名服务,
   与我们「GitHub Actions 上的纯 Python、无服务器」硬约束完全不兼容,
   本文件的 learn_domain_boilerplate() 是**我们自己从零写的**:
   靠"同域名多页里逐字重复出现的块 = 样板"这条统计事实,落成一个仓内 JSON 词典,
   零服务、零 Redis。**没有读过、也没有搬过 firecrawl 的任何一行代码。**

═══ 为什么单独立这一层 ═══════════════════════════════════════════════════
平台的四条真实产线里有三条要吃网页正文:鹰眼情报(榜单站/trending)、
RAG 语料(网页正文喂检索库)、古籍站点著录页。此前正文提取散在三个文件里
各写各的(eagle_fetch._clean / arsenal_enrich.gh_readme / daily_report_v3
.fetch_article_text),参数各不相同 —— 平台铁律「同一份逻辑只许有一份实现」
(CJK 正则出现过五份互相打架的血证)。这个文件就是那唯一一份。

═══ 中文为什么必须单独适配(下面每个数字都是本机实跑出来的)═══════════════
英文库在中文上表现差,病根几乎全在"按空格/按字符数定的阈值"。实测两处:

【坑一】MIN_EXTRACTED_SIZE = 250 的兜底闸门吞掉全部 markdown 结构
  trafilatura 级联第 3 级:正文短于 250 字符就退回 baseline(),而 baseline
  只会往 body 里塞一串扁平 <p>,**标题层级/列表/链接全部丢失**。
  250 是按英文定的(250 英文字符 ≈ 40 词),250 个汉字的信息量约等于
  700+ 英文字符 —— 等于把中文的门槛抬高约 3 倍。
  本机实测之一(桂枝汤方义 131 字中文样本,单变量只改 MIN_EXTRACTED_SIZE):
      默认 250 → 输出 125 字,**无 # 标题、无 - 列表、链接被剥成裸文本**
                 '桂枝汤方义浅析桂枝汤由桂枝、芍药……组成与用量桂枝三两芍药三两'
      改成 100 → 输出 158 字,'# 桂枝汤方义浅析\\n\\n……\\n\\n## 组成与用量\\n\\n- 桂枝三两'
  本机实测之二(四逆汤条目,正文去空白 87 字,把门槛扫了一遍找分界点):
      min = 250 / 112 / 100 / 90 → 全都是 89 字、无 # 标题,而且**把导航"首页"带进了正文**
      min =  80 /  70 /  60 / 50 → 全都是 104 字、'# 四逆汤\\n\\n甘草二两……',导航也没了
      分界点落在 80~90 之间 —— 这就是"窄带"的具体位置。
  两次输出不同,因果确凿。治法两步走:
      ① min_extracted_size_for() 按中文占比把门槛折算回来(纯中文 → 100)
      ② 折算完仍被抹平的(像四逆汤这种 87 字的条目),由 extract() 里的
         **结构抢救**再降到 MIN_SIZE_FLOOR=50 重抽 —— 触发条件是"观测到结构丢失",
         不是常年生效的低门槛,所以正常页面一点不受影响。

【坑二】collect_link_info 里 `length < 10` 把中文交叉引用段整段吞掉
  它统计"短锚文本"占比,>80% 就判定整块是链接农场并删除。10 这个字符数
  是按英文定的(Home=4、About us=8);中文 8 个汉字已经是一整句。
  我们抓中医文章喂 RAG,「参见《伤寒论》原文与柯琴注解」这类交叉引用段落
  在中文里极常见、锚文本天然短,裸用会被**静默无日志**地整段吞掉。
  本机函数级单变量实测(link_density_test,True = 判定链接农场→删):
      中文 8 字锚文本 x2  (段长18)  补丁前 删=True   补丁后 删=False  ← 误杀,修好了
      中文 14 字锚文本 x2 (段长28)  补丁前 删=True   补丁后 删=True   ← 链接占比 24/28 真超标,该删
      英文 8 字符锚文本 x2(段长20)  补丁前 删=True   补丁后 删=True   ← 英文行为**字节级不变**
      英文 24 字符锚文本 x2(段长52) 补丁前 删=False  补丁后 删=False  ← 英文行为**字节级不变**
  管线级(MIN_EXTRACTED_SIZE=60 排除坑一干扰):
      补丁前 66 字无结构 → 补丁后 78 字带 # 标题与 [锚](链接)
  治法见 _cjk_collect_link_info():只把"短"的判据从字符数换成 CJK 加权长度,
  纯 ASCII 文本结果**完全相同**,所以对英文产线零影响。

【明确不要的东西】crawl4ai 里所有"按词数"的旋钮(min_word_threshold /
  extract_text_chunks 的词数过滤 / BM25ContentFilter)全部基于 `text.split()`,
  中文整段无空格 → 一个 200 字的中文段落只得 1 个 token。
  本模块**根本不实现 min_word_threshold**(不是设成 None,是压根没有这个参数),
  就是为了杜绝后人照着 crawl4ai 文档一调参把整个中文语料清空且毫无报错。
  按查询筛正文要走平台自己的 embedding 检索(讯飞索引 183 万向量),不走空格分词的 BM25。

【顺带】html2text 的 BODY_WIDTH=78 用 textwrap 按字符硬折行,对 CJK 无感知,
  会把中文正文切成一堆碎行。本模块**不经过 html2text**(trafilatura 的
  markdown 发射器不折行),所以这个坑天然不存在;自带兜底发射器也绝不折行。

═══ 红线 ═══════════════════════════════════════════════════════════════
纯 CPU、零模型、零浏览器、零网络(extract 只吃 HTML 字符串,取网是 fetch 层的事)。
依赖只有 lxml + trafilatura(两者都已在本仓 requirements 里)。
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

# Windows 控制台默认 GBK,中文直接 print 会被静默改写(平台血证:一夜炸三次)。
# 这一行是脚本自救头,库被 import 时不该改全局 stdout,所以只在主程序里做。
_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from lxml import etree, html as lhtml
except ImportError:                                                # pragma: no cover
    raise SystemExit("crawl_core.extract 需要 lxml:pip install lxml")

try:
    import trafilatura
    import trafilatura.htmlprocessing as _tra_hp
    from trafilatura.settings import DEFAULT_CONFIG as _TRA_DEFAULT_CONFIG
    from trafilatura.utils import trim as _tra_trim
except ImportError:                                                # pragma: no cover
    trafilatura = None
    _tra_hp = None
    _TRA_DEFAULT_CONFIG = None

    def _tra_trim(s):
        return re.sub(r"\s+", " ", s or "").strip()

__all__ = [
    "Extracted", "extract", "extract_text", "to_markdown",
    "extract_links", "extract_tables", "table_to_markdown",
    "prune_html", "prune_tree", "cite_links",
    "cjk_ratio", "cjk_weighted_len", "min_extracted_size_for",
    "learn_domain_boilerplate", "load_domain_rules", "apply_domain_rules",
]


# ═══════════════════════════════════════════════════════════════════════════
# 0. CJK 度量 —— 全模块只有这一处定义"中文有多重",别处一律引用
#    (平台血证:CJK 正则曾出现过五份互相打架的实现,所以这里只许有一份)
# ═══════════════════════════════════════════════════════════════════════════

# 覆盖:汉字基本区 / 扩展A / 兼容汉字 / 中日韩标点 / 全角符号 / 日文假名。
# 假名必须算进来 —— 下载线天天抓日本内閣文庫,那些著录页是日文。
_CJK_RANGES = (
    (0x3000, 0x303F),   # 中日韩标点(、。「」)—— 中文文本里密度很高,算进占比才准
    (0x3040, 0x30FF),   # 平假名 + 片假名(内閣文庫著录页)
    (0x3400, 0x4DBF),   # 汉字扩展 A
    (0x4E00, 0x9FFF),   # 汉字基本区
    (0xF900, 0xFAFF),   # 兼容汉字
    (0xFF00, 0xFF60),   # 全角 ASCII 变体
)

# 一个汉字顶几个英文字符。2.5 这个数不是拍的,是从两处独立证据收敛来的:
#   ① 信息量口径:250 汉字 ≈ 700+ 英文字符(trafilatura 的 250 门槛按英文定)
#   ② 实测口径:同一张四列三行方剂表,英文版 text_ratio 9.41、中文版 1.95,
#      结构分相同而中文永远拿不到 text_ratio 那 2-3 分的加分
# **全模块所有需要"中英折算"的地方都用这一个常数**,改它就是全局改一次。
CJK_WEIGHT = 2.5

_WS_RE = re.compile(r"\s+")


def cjk_count(s: str) -> int:
    """字符串里 CJK 字符的个数。"""
    if not s:
        return 0
    n = 0
    for ch in s:
        o = ord(ch)
        for lo, hi in _CJK_RANGES:
            if lo <= o <= hi:
                n += 1
                break
    return n


def cjk_ratio(s: str) -> float:
    """CJK 字符占比 0.0~1.0。用来决定各种按英文定死的阈值该往下调多少。

    分母**剔除空白字符**:HTML 里的换行和缩进不属于任何语言,算进去只会
    把中文页的占比稀释。实测差别不小 —— 四逆汤样本含空白 0.823、去空白 0.908,
    换算成门槛就是 106 与 100 的差,足以改变走不走 baseline 兜底。
    """
    if not s:
        return 0.0
    body = _WS_RE.sub("", s)
    if not body:
        return 0.0
    return cjk_count(body) / len(body)


def cjk_weighted_len(s: str) -> float:
    """CJK 加权长度:汉字按 CJK_WEIGHT 折算成英文等效字符数。

    纯 ASCII 时返回值 == len(s),**逐字节等价** —— 这一点很重要:
    所有拿它替换 len() 的地方,对英文页面的行为都不会有任何改变,
    所以本模块的中文适配不会给已有的英文情报产线带来回归风险。
    """
    if not s:
        return 0.0
    return len(s) + (CJK_WEIGHT - 1.0) * cjk_count(s)


# 结构抢救用的硬地板。**只在检测到"结构已被抹平"时才降到这一档**,
# 正常页面永远用不到它 —— 所以它不会把导航碎片当正文收进来。
# 本机实测(四逆汤条目,正文 87 字去空白,单变量只改 MIN_EXTRACTED_SIZE):
#     min=250/112/100/90 → 输出 89 字,**无 # 标题、含导航"首页"**(baseline 兜底)
#     min=80/70/60/50    → 输出 104 字,'# 四逆汤\n\n甘草二两,干姜一两半……'、导航也没了
# 分界点落在 80~90 之间,取 50 留足余量(50 与 80 输出逐字相同,再低无收益)。
MIN_SIZE_FLOOR = 50


def min_extracted_size_for(text_or_ratio) -> int:
    """按中文占比动态下调 trafilatura 的 MIN_EXTRACTED_SIZE。

    为什么必须调:见文件头【坑一】—— 默认 250 会让 100~250 字的中文短条目
    走进 baseline 兜底,markdown 结构被整体抹平。而平台语料里**大量**是这个
    长度带的东西(方剂条、医案短篇、古籍片段),正好卡在窄带上。

    公式:250 / (1 + (CJK_WEIGHT-1) * ratio),即"把门槛按信息密度折算回来"。
      ratio=0.0(纯英文)→ 250,与上游默认完全一致,英文产线零变化
      ratio=0.5(中英混排)→ 143
      ratio=1.0(纯中文)→ 100
    下限钉在 80。比 80 更狠的下调**不放在这条公式里**,而是交给
    MIN_SIZE_FLOOR 的抢救重试 —— 因为"再降一截"应该由**观测到的结构丢失**
    触发,而不是由一个我拍出来的常数常年生效
    (平台铁律:阈值只许站在实测样本上;猜两次没中就停手装诊断)。
    """
    r = text_or_ratio if isinstance(text_or_ratio, float) else cjk_ratio(str(text_or_ratio))
    v = 250.0 / (1.0 + (CJK_WEIGHT - 1.0) * max(0.0, min(1.0, r)))
    return int(max(80, min(250, round(v))))


# ═══════════════════════════════════════════════════════════════════════════
# 1. trafilatura 的中文适配(两处,都是"最小切口")
# ═══════════════════════════════════════════════════════════════════════════

_orig_collect_link_info = getattr(_tra_hp, "collect_link_info", None)


def _cjk_collect_link_info(links_xpath):
    """替换 trafilatura.htmlprocessing.collect_link_info —— 只改"短"的定义。

    上游那一行是 `shortelems = sum(1 for length in lengths if length < 10)`。
    10 是按英文锚文本定的(Home=4、About us=8)。中文 8 个字已经是一整句,
    照样 < 10,于是「参见<a>伤寒论太阳病篇</a>与<a>柯琴伤寒来苏集</a>」
    这种交叉引用段被判成链接农场、**整段静默删除**。

    改法只有一处:把"短"的判据从 len() 换成 cjk_weighted_len()。
    返回的第一个值 linklen 仍然是**原始字符数** —— 它在上游要和 elemlen=len(text)
    直接比大小,折算了就两边口径不一致,反而制造新 bug。这是刻意的不对称。

    实测(见文件头【坑二】):英文四组对照补丁前后判定**完全一致**,
    中文 8 字锚那组从"删"变成"留"。即:只修中文误杀,不动英文。
    """
    mylist = [e for e in (_tra_trim(el.text_content()) for el in links_xpath) if e]
    lengths = list(map(len, mylist))
    shortelems = sum(1 for t in mylist if cjk_weighted_len(t) < 10)
    return sum(lengths), len(mylist), shortelems, mylist


def install_cjk_patches(force: bool = False) -> bool:
    """把 CJK 链接密度补丁装到 trafilatura 上。

    **为什么敢在 import 时就装(而不是用上下文管理器临时装)**:
    补丁对纯 ASCII 输入逐字节等价(见 cjk_weighted_len 注释),所以对本仓
    已有的英文情报产线(daily_report_v3 / eagle_fetch)是零行为变化,
    而临时装卸在多线程下反而不安全(全局函数替换没有原子性)。
    要关掉:设环境变量 CRAWL_CORE_NO_CJK_PATCH=1。
    """
    if _tra_hp is None or _orig_collect_link_info is None:
        return False
    if os.environ.get("CRAWL_CORE_NO_CJK_PATCH") == "1" and not force:
        return False
    if getattr(_tra_hp.collect_link_info, "__name__", "") == "_cjk_collect_link_info":
        return True
    _tra_hp.collect_link_info = _cjk_collect_link_info
    return True


_CJK_PATCH_ON = install_cjk_patches()

_CONFIG_CACHE: dict = {}


def _tra_config(min_size: int):
    """造一份只改了 MIN_EXTRACTED_SIZE 的 trafilatura 配置。

    注意 configparser 的 key 是大小写不敏感的(内部 lower),所以写
    MIN_EXTRACTED_SIZE 和 trafilatura 内部的 getint('DEFAULT','MIN_EXTRACTED_SIZE')
    能对上 —— 这一点本机实跑验证过(改 250→100 输出确实变了)。
    """
    if _TRA_DEFAULT_CONFIG is None:
        return None
    if min_size in _CONFIG_CACHE:
        return _CONFIG_CACHE[min_size]
    import configparser
    c = configparser.ConfigParser()
    c.read_dict({"DEFAULT": dict(_TRA_DEFAULT_CONFIG.defaults())})
    c["DEFAULT"]["MIN_EXTRACTED_SIZE"] = str(min_size)
    _CONFIG_CACHE[min_size] = c
    return c


# ═══════════════════════════════════════════════════════════════════════════
# 2. 树工具
# ═══════════════════════════════════════════════════════════════════════════

def _to_tree(src):
    """HTML 字符串 / 已解析的树 → lxml HtmlElement。全模块只解析一次。"""
    if src is None:
        return None
    if not isinstance(src, str):
        return src
    s = src.strip()
    if not s:
        return None
    try:
        # 先按 bytes 走,让 lxml 自己读 <meta charset>;字符串路径遇到
        # 页面自带 encoding 声明时 lxml 会抛 ValueError。
        return lhtml.document_fromstring(s)
    except Exception:                                              # noqa: BLE001
        try:
            return lhtml.fromstring(s.encode("utf-8", "replace"))
        except Exception:                                          # noqa: BLE001
            return None


def _tostring(node) -> str:
    try:
        return lhtml.tostring(node, encoding="unicode")
    except Exception:                                              # noqa: BLE001
        return ""


def _text_of(node) -> str:
    try:
        return (node.text_content() or "").strip()
    except Exception:                                              # noqa: BLE001
        return ""


def _drop(node):
    """删掉节点,保留它的 tail 文本(tail 是散文,删了会丢正文)。"""
    parent = node.getparent()
    if parent is None:
        return
    if node.tail:
        prev = node.getprevious()
        if prev is not None:
            prev.tail = (prev.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


# ═══════════════════════════════════════════════════════════════════════════
# 3. PruningContentFilter 移植(crawl4ai,Apache-2.0)—— 第二套独立算法
#
#    为什么要第二套:trafilatura 自己的第二意见是 justext,而 justext 的核心
#    是停用词密度、靠空格分词,它的 JUSTEXT_LANGUAGES 表里明写着
#    「no justext stoplist available: 'ja'(Japanese), 'zh'(Chinese)」——
#    **中文上这一级兜底名存实亡**。这套打分全部基于字符数和标签数、不做任何分词,
#    对中文天然安全,正好补上那个空缺。
# ═══════════════════════════════════════════════════════════════════════════

# 硬删标签:这些从来不是正文(移植自 crawl4ai excluded_tags)
_EXCLUDED_TAGS = {"nav", "footer", "header", "aside", "script", "style",
                  "form", "iframe", "noscript"}

# class / id 里出现这些词 → 扣分(移植自 crawl4ai negative_patterns)
_NEG_CLASS_RE = re.compile(r"nav|footer|header|sidebar|ads|comment|promo|advert|social|share", re.I)

_TAG_WEIGHTS = {"div": 0.5, "p": 1.0, "article": 1.5, "section": 1.0, "span": 0.3,
                "li": 0.5, "ul": 0.5, "ol": 0.5,
                "h1": 1.2, "h2": 1.1, "h3": 1.0, "h4": 0.9, "h5": 0.8, "h6": 0.7}

_TAG_IMPORTANCE = {"article": 1.5, "main": 1.4, "section": 1.3, "p": 1.2,
                   "h1": 1.4, "h2": 1.3, "h3": 1.2, "div": 0.7, "span": 0.6}

_METRIC_WEIGHTS = {"text_density": 0.4, "link_density": 0.2, "tag_weight": 0.2,
                   "class_id_weight": 0.1, "text_length": 0.1}

# 打分低于它就整枝砍掉。0.48 是 crawl4ai 的实测默认值,原样沿用 ——
# 这个分数是纯结构指标(字符数/标签数),语言无关,中文不需要另调。
PRUNE_THRESHOLD = 0.48

# 默认白名单:这几类节点靠"结构指标"天生吃亏(表格标签多→文本密度低;
# 代码块字符怪),但它们恰恰是我们最要保的内容。命中即整枝保留、不再下钻。
# 注意**不能**把 article/main 放进来 —— 保留即跳过整棵子树的剪枝,
# 那等于把这个过滤器关掉了。
_PRESERVE_TAGS = ("table", "pre", "code", "blockquote", "figure")


def _inner_html_len(node) -> int:
    """节点的 innerHTML 长度(对齐 crawl4ai 的 node.encode_contents())。"""
    n = len(node.text or "")
    for ch in node:
        n += len(_tostring(ch))
    return n


def _class_id_weight(node) -> float:
    s = 0.0
    cls = node.get("class") or ""
    if cls and _NEG_CLASS_RE.search(cls):
        s -= 0.5
    eid = node.get("id") or ""
    if eid and _NEG_CLASS_RE.search(eid):
        s -= 0.5
    return s


def _composite_score(node, text_len: int, tag_len: int, link_text_len: int) -> float:
    """五项加权复合打分(移植自 crawl4ai _compute_composite_score)。

    **刻意没有实现 min_word_threshold**。上游那段是
    `word_count = text.count(" ") + 1; if word_count < N: return -1.0`,
    一个 200 字的中文段落 word_count 恒为 1,只要有人按 crawl4ai 文档把
    这个参数设成常见的 5 或 10,**整篇中文正文会 100% 拿到"保证删除"且毫无报错**。
    与其留个参数等人踩,不如让这条路根本不存在。
    """
    score = 0.0
    total = 0.0

    density = (text_len / tag_len) if tag_len > 0 else 0.0
    score += _METRIC_WEIGHTS["text_density"] * density
    total += _METRIC_WEIGHTS["text_density"]

    ld = 1.0 - ((link_text_len / text_len) if text_len > 0 else 0.0)
    score += _METRIC_WEIGHTS["link_density"] * ld
    total += _METRIC_WEIGHTS["link_density"]

    score += _METRIC_WEIGHTS["tag_weight"] * _TAG_WEIGHTS.get(node.tag, 0.5)
    total += _METRIC_WEIGHTS["tag_weight"]

    score += _METRIC_WEIGHTS["class_id_weight"] * max(0.0, _class_id_weight(node))
    total += _METRIC_WEIGHTS["class_id_weight"]

    score += _METRIC_WEIGHTS["text_length"] * math.log(text_len + 1)
    total += _METRIC_WEIGHTS["text_length"]

    return (score / total) if total > 0 else 0.0


def _link_text_len(node) -> int:
    """块内锚文本总长。

    **与上游的口径差异(刻意的,已实测)**:crawl4ai 用
    `node.find_all("a", recursive=False)` + `a.string`,
    ① 只看直接子级的 <a> ② 只在 <a> 内容是单个纯文本节点时才计数。
    真实的导航/侧栏几乎全是 <div><ul><li><a>…</a></li></ul></div> 这种嵌套,
    上游口径下这类块的 link_text_len 恒为 0 → link_density 满分 → 该删的不删。
    这里改成统计**全部后代锚文本**,这才是"链接密度"这个指标的本意。
    """
    n = 0
    for a in node.iter("a"):
        t = _text_of(a)
        if t:
            n += len(t)
    return n


def _is_preserved(node, preserve_tags, preserve_classes) -> bool:
    if preserve_tags and node.tag in preserve_tags:
        return True
    if preserve_classes:
        cls = set((node.get("class") or "").split())
        if cls & set(preserve_classes):
            return True
    return False


def prune_tree(node, threshold: float = PRUNE_THRESHOLD, mode: str = "dynamic",
               preserve_tags=_PRESERVE_TAGS, preserve_classes=()) -> None:
    """自顶向下打分剪枝,**就地修改**这棵树。

    高于阈值 → 递归下钻子节点;低于阈值 → 整枝砍掉。
    dynamic 模式按节点性质动态调阈值(移植自 crawl4ai):
      重要标签(article/main/p/h1…)阈值 ×0.8   —— 宁可留
      文本占比 > 0.4 阈值 ×0.9                  —— 文字多的更宁可留
      链接占比 > 0.6 阈值 ×1.2                  —— 链接堆更严格
    """
    if node is None or not isinstance(node.tag, str):
        return
    if _is_preserved(node, preserve_tags, preserve_classes):
        return

    text = _text_of(node)
    text_len = len(text)
    tag_len = _inner_html_len(node)
    link_len = _link_text_len(node)

    score = _composite_score(node, text_len, tag_len, link_len)

    if mode == "fixed":
        remove = score < threshold
    else:
        th = threshold
        if _TAG_IMPORTANCE.get(node.tag, 0.7) > 1:
            th *= 0.8
        if tag_len > 0 and (text_len / tag_len) > 0.4:
            th *= 0.9
        if (link_len / text_len if text_len > 0 else 1.0) > 0.6:
            th *= 1.2
        remove = score < th

    if remove:
        _drop(node)
        return
    for child in list(node):
        if isinstance(child.tag, str):
            prune_tree(child, threshold, mode, preserve_tags, preserve_classes)


def prune_html(html, threshold: float = PRUNE_THRESHOLD, mode: str = "dynamic") -> str:
    """HTML 字符串 → 剪枝后的 HTML 字符串(第二套算法的对外入口)。"""
    tree = _to_tree(html)
    if tree is None:
        return ""
    for tag in _EXCLUDED_TAGS:
        for el in list(tree.iter(tag)):
            _drop(el)
    for c in tree.xpath("//comment()"):
        _drop(c)
    body = tree.find("body") if tree.tag != "body" else tree
    if body is None:
        body = tree
    prune_tree(body, threshold, mode)
    return _tostring(tree)


# ═══════════════════════════════════════════════════════════════════════════
# 4. 结构化数据 —— 链接集合
# ═══════════════════════════════════════════════════════════════════════════

_SKIP_SCHEME = ("javascript:", "data:", "about:", "#")


def extract_links(src, base_url: str = "", same_host_only: bool = False):
    """抽全页链接 → [{"url","text","rel"}],绝对化 + 去重 + 保序。

    rel: internal(同域) / external(外域) / mailto / tel。
    base href 优先于传入的 base_url —— 页面自己声明的基准比调用方猜的准。
    """
    tree = _to_tree(src)
    if tree is None:
        return []
    base = base_url or ""
    b = tree.find(".//base")
    if b is not None and b.get("href"):
        base = urljoin(base, b.get("href"))
    host = urlparse(base).netloc.lower() if base else ""

    out, seen = [], set()
    for a in tree.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_SCHEME):
            continue
        url = urljoin(base, href) if base else href
        if url in seen:
            continue
        seen.add(url)
        p = urlparse(url)
        if p.scheme in ("mailto", "tel"):
            rel = p.scheme
        elif host and p.netloc.lower() == host:
            rel = "internal"
        elif not p.netloc:
            rel = "internal"
        else:
            rel = "external"
        if same_host_only and rel != "internal":
            continue
        out.append({"url": url, "text": _text_of(a)[:200], "rel": rel})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 5. 结构化数据 —— 表格(is_data_table 打分,移植自 crawl4ai + CJK 折算)
# ═══════════════════════════════════════════════════════════════════════════

TABLE_SCORE_THRESHOLD = 7.0


def score_table(table) -> float:
    """区分"数据表"与"布局表"的打分制(阈值 7),移植自 crawl4ai is_data_table。

    ── 本机实测(2026-09-02,同一份四列三行方剂剂量表)。**加第 12 项之前**的分数:
       中文·带 thead/th   10.00 收   text_ratio 原始 1.59 → CJK 加权 3.98
       英文·带 thead/th   10.00 收   text_ratio 6.27(无需加权)
       中文·裸 tr/td       4.00 弃   text_ratio 原始 1.75 → CJK 加权 4.37
       英文·裸 tr/td       4.00 弃   text_ratio 6.90
       布局表 role=presentation  -1.00 弃
       布局表 嵌套 table         -1.00 弃
       布局表 图文两列            4.00 弃
    ── 这组数字推翻了我动手前的两个预判,如实记 ────────────────────────────
    ① 「中文表会因 text_ratio 拿不到加分而被丢弃」——**不成立**。裸表中英**同为 4.00**,
       差的不是语言,是 thead/th 的有无。加权把中文 ratio 抬了 2.5 倍
       (1.75→4.37)但离 10 那一档仍远,所以对短单元格的中文表**加权几乎不起作用**。
       加权仍然保留:单元格平均 ≥7 个汉字时(古籍原文表、医案叙述表)能跨过 10 档,
       那时它是真起作用的。这是它的有效边界,不是万能药。
    ② 4.00 这一档里**真数据表和图文布局表同分**,靠原打分体系分不开 ——
       这才是真正的坑,而且与语言无关。

    ── 我们自己加的第 12 项(语言无关,不是 crawl4ai 的)────────────────────
    「无 img、≥3 列、≥2 行、每格平均有实义文本」的纯文字网格 → +3。
    语义明确:布局表几乎总是两列、或者格子里塞图;三列以上的纯文字网格是数据表。
    +3 这个数值是**由上游阈值 7 反推**的(4+3=7 刚好跨过),依据就是上面那张对照表。
    加完之后本机实跑的分数(自测 ⑤ 每次运行都会重新打印,别信这行字、看输出):
       中文带thead 13.00 收 / 英文带thead 13.00 收 / 中文裸表 7.00 收 / 英文裸表 7.00 收
       布局表 role -1.00 弃 / 嵌套 -1.00 弃 / 图文两列 4.00 弃
    刻意不放宽到两列:两列恰恰是布局表最常见的形态,宁可漏掉两列数据表,
    也不能把布局表收进来(平台铁律:货不对板很严重)。
    """
    score = 0.0
    has_thead = len(table.xpath(".//thead")) > 0
    has_tbody = len(table.xpath(".//tbody")) > 0
    if has_thead:
        score += 2
    if has_tbody:
        score += 1

    if len(table.xpath(".//th")) > 0:
        score += 2
        if has_thead or table.xpath(".//tr[1]/th"):
            score += 1

    if len(table.xpath(".//table")) > 0:
        score -= 3                      # 嵌套表 = 典型的布局表
    if (table.get("role") or "").lower() in ("presentation", "none"):
        score -= 3                      # 作者自己声明了这是排版用的

    rows = table.xpath(".//tr")
    if not rows:
        return -99.0
    col_counts = [len(r.xpath(".//td|.//th")) for r in rows]
    if col_counts:
        avg = sum(col_counts) / len(col_counts)
        var = sum((c - avg) ** 2 for c in col_counts) / len(col_counts)
        if var < 1:
            score += 2                  # 各行列数整齐 = 真表格

    if table.xpath(".//caption"):
        score += 2
    if table.get("summary"):
        score += 1

    # ↓↓↓ 中文适配点:文本量用 CJK 加权,而不是裸字符数
    total_text = sum(
        cjk_weighted_len("".join(cell.itertext()).strip())
        for r in rows for cell in r.xpath(".//td|.//th")
    )
    total_tags = sum(1 for _ in table.iterdescendants())
    text_ratio = total_text / (total_tags + 1e-5)
    if text_ratio > 20:
        score += 3
    elif text_ratio > 10:
        score += 2

    score += sum(0.5 for a in table.attrib if a.startswith("data-"))

    if col_counts and len(rows) >= 2 and (sum(col_counts) / len(col_counts)) >= 2:
        score += 2

    # 第 12 项:纯文字网格救援(我们自己加的,语言无关)。见函数 docstring 的实测表。
    n_cells = sum(col_counts)
    if (n_cells and len(rows) >= 2 and (sum(col_counts) / len(col_counts)) >= 3
            and len(table.xpath(".//img")) == 0
            and (total_text / n_cells) >= 2.0):
        score += 3
    return score


def extract_table_data(table) -> dict:
    """把一张 table 拆成 {headers, rows, caption, summary, metadata}。

    colspan 按重复 N 次展开、thead 缺失时退回首行当表头、行按 max_columns
    截断补空对齐(移植自 crawl4ai extract_table_data)。
    """
    cap = table.xpath(".//caption/text()")
    caption = cap[0].strip() if cap else ""
    summary = (table.get("summary") or "").strip()

    headers = []
    thead_rows = table.xpath(".//thead/tr")
    src_cells = []
    if thead_rows:
        src_cells = thead_rows[0].xpath(".//th")
    else:
        first = table.xpath(".//tr[1]")
        if first:
            src_cells = first[0].xpath(".//th|.//td")
    for cell in src_cells:
        try:
            span = int(cell.get("colspan", 1))
        except ValueError:
            span = 1
        headers.extend([_text_of(cell)] * max(1, span))

    rows = []
    skip_first = not thead_rows and bool(table.xpath(".//tr[1]/th"))
    trs = table.xpath(".//tr[not(ancestor::thead)]")
    for i, r in enumerate(trs):
        if skip_first and i == 0:
            continue                    # 首行已当表头用掉,别再当数据行重复一遍
        data = []
        for cell in r.xpath(".//td"):
            try:
                span = int(cell.get("colspan", 1))
            except ValueError:
                span = 1
            data.extend([_text_of(cell)] * max(1, span))
        if data:
            rows.append(data)

    maxc = len(headers) if headers else (max((len(r) for r in rows), default=0))
    aligned = [r[:maxc] + [""] * max(0, maxc - len(r)) for r in rows]
    if not headers and maxc > 0:
        headers = ["列 %d" % (i + 1) for i in range(maxc)]

    return {
        "headers": headers,
        "rows": aligned,
        "caption": caption,
        "summary": summary,
        "metadata": {
            "row_count": len(aligned),
            "column_count": maxc,
            "has_headers": bool(thead_rows) or bool(table.xpath(".//tr[1]/th")),
            "has_caption": bool(caption),
            "has_summary": bool(summary),
            "id": table.get("id") or "",
            "class": table.get("class") or "",
        },
    }


def table_to_markdown(data: dict) -> str:
    """结构化表格 → GFM 管道表格。

    中文列宽不需要对齐(CommonMark 管道表不看视觉宽度),所以**不做任何填充**;
    单元格里的 | 必须转义,否则整行错列。
    """
    heads = data.get("headers") or []
    rows = data.get("rows") or []
    if not heads and not rows:
        return ""

    def cell(s):
        return (s or "").replace("|", "\\|").replace("\n", " ").strip()

    out = []
    if data.get("caption"):
        out.append("**%s**\n" % cell(data["caption"]))
    out.append("| " + " | ".join(cell(h) for h in heads) + " |")
    out.append("| " + " | ".join("---" for _ in heads) + " |")
    for r in rows:
        out.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(out)


def extract_tables(src, min_score: float = TABLE_SCORE_THRESHOLD, with_markdown: bool = True):
    """抽全页的**数据表**(布局表被打分挡掉),带分数便于事后调阈值。"""
    tree = _to_tree(src)
    if tree is None:
        return []
    out = []
    for t in tree.iter("table"):
        s = score_table(t)
        if s < min_score:
            continue
        d = extract_table_data(t)
        d["score"] = round(s, 2)
        if with_markdown:
            d["markdown"] = table_to_markdown(d)
        out.append(d)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 6. 引用编号 ⟨N⟩ + 文末 References(移植自 crawl4ai convert_links_to_citations)
#
#    对 RAG 语料价值直接:正文里不再夹一堆长 URL 干扰 embedding,
#    但引用关系完整留在文末,做 grounding 时能回溯出处 ——
#    契合平台「AI 寻脉红线 = 文献主语、必须能指回出处」的取证要求。
# ═══════════════════════════════════════════════════════════════════════════

_LINK_PATTERN = re.compile(
    r'!?\[((?:[^\[\]]|\[(?:[^\[\]]|\[[^\]]*\])*\])*)\]\(((?:[^()\s]|\([^()]*\))*)(?:\s+"([^"]*)")?\)')


def cite_links(markdown: str, base_url: str = ""):
    """[文本](url) → 文本⟨N⟩,同一 URL 复用同一编号,末尾生成 References。

    返回 (改写后的正文, References 段)。纯正则 + 字典,零依赖。
    """
    if not markdown:
        return "", ""
    link_map, url_cache, parts = {}, {}, []
    last, counter = 0, 1
    for m in _LINK_PATTERN.finditer(markdown):
        parts.append(markdown[last:m.start()])
        text, url, title = m.groups()
        url = url or ""
        if base_url and not url.startswith(("http://", "https://", "mailto:")):
            if url not in url_cache:
                url_cache[url] = urljoin(base_url, url)
            url = url_cache[url]
        if url not in link_map:
            desc = []
            if title:
                desc.append(title)
            if text and text != title:
                desc.append(text)
            link_map[url] = (counter, ": " + " - ".join(desc) if desc else "")
            counter += 1
        num = link_map[url][0]
        parts.append("![%s⟨%d⟩]" % (text, num) if m.group(0).startswith("!")
                     else "%s⟨%d⟩" % (text, num))
        last = m.end()
    parts.append(markdown[last:])

    refs = ["\n\n## References\n\n"] if link_map else []
    refs.extend("⟨%d⟩ %s%s\n" % (num, url, desc)
                for url, (num, desc) in sorted(link_map.items(), key=lambda x: x[1][0]))
    return "".join(parts), "".join(refs)


# ═══════════════════════════════════════════════════════════════════════════
# 7. 按域名沉淀样板签名(借鉴 firecrawl OMCE 的**思路**,实现完全自写)
#
#    firecrawl 的 OMCE 要一个能按 hostname 查签名的常驻服务(Redis/DB),
#    违背我们「无服务器」硬约束。这里换成:一个仓内 JSON 词典。
#    零服务、零 Redis、跟着代码走版本控制,Actions 上 clone 下来就能用。
#
#    适用场景很明确:鹰眼天天抓**同一批固定站点**(trending / 各 awesome 榜单站),
#    这些站的样板每天都一样,没必要每页现算。
# ═══════════════════════════════════════════════════════════════════════════

DOMAIN_RULES_PATH = os.path.join(_HERE, "domain_rules.json")
_DOMAIN_RULES_CACHE = None


def load_domain_rules(path: str = "") -> dict:
    """读 {hostname: [xpath, ...]} 词典。文件不存在就是空字典,不报错。"""
    global _DOMAIN_RULES_CACHE
    p = path or DOMAIN_RULES_PATH
    if not path and _DOMAIN_RULES_CACHE is not None:
        return _DOMAIN_RULES_CACHE
    rules = {}
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                rules = json.load(f) or {}
        except Exception:                                          # noqa: BLE001
            rules = {}
    if not path:
        _DOMAIN_RULES_CACHE = rules
    return rules


def _node_signature(node) -> str:
    """给节点造一个可复用的 XPath 签名。

    只用 tag + id + 第一个 class —— 签名要**稳**,越具体越容易被站点小改版打碎。
    """
    tag = node.tag
    eid = (node.get("id") or "").strip()
    if eid and " " not in eid:
        return "//%s[@id='%s']" % (tag, eid.replace("'", ""))
    cls = (node.get("class") or "").split()
    if cls:
        return "//%s[contains(concat(' ',normalize-space(@class),' '),' %s ')]" % (
            tag, cls[0].replace("'", ""))
    return ""


def learn_domain_boilerplate(htmls, min_repeat: int = 2, min_chars: int = 12) -> list:
    """从同一域名的多个页面里学出"样板块"的选择器。

    判据是一条统计事实,不需要模型:**同域名不同页面里逐字重复出现的块 = 样板**
    (导航条、页脚、侧栏在每页都一模一样;正文每页都不同)。
    min_repeat=2 意思是至少在 2 个页面里一字不差地出现过。
    min_chars 挡掉「首页」这种两个字的碎片(签名太泛会误删)。

    返回去重后的 XPath 列表,交给 save/apply 用。**不联网、不落盘**,
    调用方自己决定要不要写进 domain_rules.json —— 学出来的规则会删内容,
    落盘前该由人过一眼(平台铁律:删除永远排在验证之后)。
    """
    tally = {}
    for h in htmls:
        tree = _to_tree(h)
        if tree is None:
            continue
        seen_here = set()
        for node in tree.iter():
            if not isinstance(node.tag, str) or node.tag in ("html", "body", "head"):
                continue
            sig = _node_signature(node)
            if not sig:
                continue
            txt = re.sub(r"\s+", " ", _text_of(node))
            if len(txt) < min_chars:
                continue
            key = (sig, txt)
            if key in seen_here:
                continue
            seen_here.add(key)
            tally[key] = tally.get(key, 0) + 1
    out, seen = [], set()
    for (sig, _txt), n in sorted(tally.items(), key=lambda kv: -kv[1]):
        if n >= min_repeat and sig not in seen:
            seen.add(sig)
            out.append(sig)
    return out


def apply_domain_rules(tree, url: str, rules: dict = None) -> int:
    """按 hostname 套用已学到的样板选择器,返回删掉的节点数。"""
    if tree is None or not url:
        return 0
    host = urlparse(url).netloc.lower()
    if not host:
        return 0
    rules = rules if rules is not None else load_domain_rules()
    xps = rules.get(host) or rules.get(host.lstrip("www.")) or []
    n = 0
    for xp in xps:
        try:
            for node in tree.xpath(xp):
                _drop(node)
                n += 1
        except Exception:                                          # noqa: BLE001
            continue                    # 站点改版后旧签名会失效,失效就跳过,不阻断
    return n


# ═══════════════════════════════════════════════════════════════════════════
# 8. 兜底 markdown 发射器(**只在 trafilatura 缺席时用**)
#
#    主路径一律走 trafilatura 的发射器 —— 它已经把 CommonMark 边角
#    (代码围栏冲突、转义幂等、段首块语法误判、嵌套列表缩进)全踩过了,
#    重写一份只会重新踩一遍。这里这份是"没有 trafilatura 时不至于空手"的降级件,
#    刻意做得简单,并且**绝不折行**(html2text 的 BODY_WIDTH=78 按字符硬折,
#    对 CJK 无感知,会把中文段落切成碎行 —— 这个坑在这里天然不存在)。
# ═══════════════════════════════════════════════════════════════════════════

_MD_ESCAPE = re.compile(r"([\\`*_\[\]<>])")


def _md_inline(node) -> str:
    parts = [_MD_ESCAPE.sub(r"\\\1", node.text or "")]
    for ch in node:
        if not isinstance(ch.tag, str):
            continue
        inner = _md_inline(ch)
        if ch.tag == "a" and ch.get("href"):
            parts.append("[%s](%s)" % (inner, ch.get("href")))
        elif ch.tag in ("strong", "b"):
            parts.append("**%s**" % inner)
        elif ch.tag in ("em", "i"):
            parts.append("*%s*" % inner)
        elif ch.tag == "code":
            parts.append("`%s`" % (ch.text_content() or ""))
        elif ch.tag == "br":
            parts.append("\n")
        else:
            parts.append(inner)
        parts.append(_MD_ESCAPE.sub(r"\\\1", ch.tail or ""))
    return "".join(parts)


def _fallback_markdown(tree) -> str:
    if tree is None:
        return ""
    body = tree.find("body") if tree.tag != "body" else tree
    body = body if body is not None else tree
    out = []
    for node in body.iter():
        if not isinstance(node.tag, str):
            continue
        t = node.tag
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out.append("#" * int(t[1]) + " " + _md_inline(node).strip())
        elif t == "p":
            s = _md_inline(node).strip()
            if s:
                out.append(s)
        elif t == "li":
            s = _md_inline(node).strip()
            if s:
                out.append("- " + s)
        elif t == "pre":
            out.append("```\n" + (node.text_content() or "").strip("\n") + "\n```")
        elif t == "blockquote":
            s = _text_of(node)
            if s:
                out.append("> " + s)
        elif t == "table":
            md = table_to_markdown(extract_table_data(node))
            if md:
                out.append(md)
    return "\n\n".join(x for x in out if x)


# ═══════════════════════════════════════════════════════════════════════════
# 9. 主入口
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Extracted:
    """正文层的统一产出。

    带 engine / chars / notes 是哨兵基因(与 eagle_fetch.FetchResult 同一套约定):
    调用方零成本就能把"这次到底提到多少字、走的哪条路"落进台账,
    绿勾装死无处藏身(平台血证:2026-08-16 绿勾零产出没人发现)。
    """
    url: str = ""
    title: str = ""
    markdown: str = ""
    text: str = ""
    references: str = ""
    links: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    engine: str = "none"          # trafilatura / trafilatura+prune / fallback / none
    cjk_ratio: float = 0.0
    min_size: int = 250
    chars: int = 0
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "engine": self.engine,
                "chars": self.chars, "cjk_ratio": round(self.cjk_ratio, 3),
                "min_size": self.min_size, "n_links": len(self.links),
                "n_tables": len(self.tables), "notes": self.notes,
                "markdown": self.markdown, "text": self.text,
                "references": self.references,
                "links": self.links, "tables": self.tables}


def _tra_run(html: str, url: str, min_size: int, recall: bool = False):
    """跑一次 trafilatura,同时要 markdown 与纯文本两种产出。"""
    if trafilatura is None:
        return "", ""
    cfg = _tra_config(min_size)
    kw = dict(url=url or None, include_comments=False, include_tables=True,
              include_links=True, include_images=False, include_formatting=True,
              favor_recall=recall, config=cfg)
    try:
        md = trafilatura.extract(html, output_format="markdown", **kw) or ""
    except Exception:                                              # noqa: BLE001
        md = ""
    try:
        txt = trafilatura.extract(html, output_format="txt", **kw) or ""
    except Exception:                                              # noqa: BLE001
        txt = ""
    return md, txt


def _looks_structureless(md: str, tree) -> bool:
    """页面里明明有 h1/h2/li,产出里却一个 markdown 标记都没有 → 结构被抹平了。

    这正是【坑一】的现场特征:baseline 兜底只吐扁平段落。
    判据语言无关(数标记,不看词),中英通用。
    """
    if not md:
        return True
    if re.search(r"(^|\n)#{1,6} |(^|\n)[-*] |(^|\n)\d+\. |\|", md):
        return False
    try:
        has_struct = bool(tree.xpath(".//h1|.//h2|.//h3|.//li|.//table"))
    except Exception:                                              # noqa: BLE001
        has_struct = False
    return has_struct


def _prefer(a_md: str, a_txt: str, b_md: str, b_txt: str, tree):
    """两套算法择优。

    判据借自 trafilatura._prefer_readability 里**语言无关**的那几条
    (长度倍数关系、结构是否残缺),刻意不用它那条绑死 readability-lxml 的实现,
    也不用 justext —— justext 靠停用词密度和空格分词,官方 JUSTEXT_LANGUAGES
    表里明写着中文/日文无停用词表,那一级兜底在中文上是死的。
    """
    if not b_txt:
        return "A", a_md, a_txt
    if not a_txt:
        return "B", b_md, b_txt
    # B 明显更长 → 说明 A 欠抽了(正文容器没定位到)
    if len(b_txt) > 2 * len(a_txt):
        return "B", b_md, b_txt
    # A 结构被抹平而 B 保住了 → 要 B(这是【坑一】的典型现场)
    if _looks_structureless(a_md, tree) and not _looks_structureless(b_md, tree):
        return "B", b_md, b_txt
    return "A", a_md, a_txt


def extract(html, url: str = "", *, second_opinion: str = "auto",
            include_links: bool = True, include_tables: bool = True,
            cite: bool = False, use_domain_rules: bool = True,
            table_min_score: float = TABLE_SCORE_THRESHOLD) -> Extracted:
    """任意网页 HTML → Extracted(正文 markdown + 纯文本 + 链接集合 + 表格)。

    second_opinion:
      "auto"(默认)—— 只在第一套算法看起来欠抽/结构被抹平时,才跑第二套(剪枝+再抽)。
                       正常页面零额外开销。
      "always"      —— 两套都跑再择优(慢一倍,用于调参和对账)。
      "never"       —— 只跑 trafilatura。

    不联网:html 从哪来是 fetch 层的事(见 scripts/intel_radar/eagle_fetch.py 的三级降级)。
    """
    res = Extracted(url=url)
    tree = _to_tree(html)
    if tree is None:
        res.notes.append("HTML 解析失败或为空")
        return res

    if use_domain_rules and url:
        n = apply_domain_rules(tree, url)
        if n:
            res.notes.append("按域名样板签名删掉 %d 个块" % n)

    # 标题:优先 <title>,其次第一个 h1
    t = tree.find(".//title")
    res.title = _text_of(t) if t is not None else ""
    if not res.title:
        h1 = tree.find(".//h1")
        res.title = _text_of(h1) if h1 is not None else ""

    # 先量一次中文占比 —— 后面所有按英文定的阈值都靠它折算。
    # **只量散文节点(p/h1-h3/li),不量整页**:整页文本被导航、参考文献、
    # 版权声明里的大量 ASCII 稀释,量出来的比例是假的。真页面实测(2026-09-02,过代理):
    #   zh.wikipedia 桂枝汤   整页 0.134(→门槛 208) | 散文 0.974(→门槛 102)
    #   zh.wikipedia 伤寒论   整页 0.267(→门槛 179) | 散文 0.729(→门槛 119)
    #   github.com/trending  整页 0.002(→门槛 249) | 散文 0.008(→门槛 247)  英文页两种口径一致
    # 即:这个口径只在中文页上纠偏,对英文页零影响。
    page_text = _text_of(tree)
    prose = " ".join(_text_of(n) for n in tree.xpath(".//p|.//h1|.//h2|.//h3|.//li"))
    res.cjk_ratio = cjk_ratio(prose if len(_WS_RE.sub("", prose)) >= 40 else page_text)
    res.min_size = min_extracted_size_for(res.cjk_ratio)

    cleaned_html = _tostring(tree)

    a_md, a_txt = _tra_run(cleaned_html, url, res.min_size)
    engine = "trafilatura" if a_txt or a_md else "none"

    # 结构抢救:页面明明有 h1/h2/li,产出却一个 markdown 标记都没有 →
    # 说明踩进了 baseline 兜底(【坑一】的现场特征)。这时**才**把门槛降到地板重试。
    # 触发条件是"观测到的结构丢失",不是常年生效的低阈值 —— 正常页面不受影响。
    if _looks_structureless(a_md, tree) and res.min_size > MIN_SIZE_FLOOR:
        r_md, r_txt = _tra_run(cleaned_html, url, MIN_SIZE_FLOOR)
        if r_md and not _looks_structureless(r_md, tree):
            a_md, a_txt = r_md, r_txt
            engine = "trafilatura(结构抢救)"
            res.notes.append("结构被 baseline 抹平,门槛降到 %d 重抽" % MIN_SIZE_FLOOR)
            res.min_size = MIN_SIZE_FLOOR

    md, txt = a_md, a_txt

    need_b = second_opinion == "always" or (
        second_opinion == "auto" and (
            not a_txt
            or len(a_txt) < res.min_size
            or _looks_structureless(a_md, tree)
            or (page_text and len(a_txt) < 0.12 * len(page_text))))

    if second_opinion != "never" and need_b:
        pruned = prune_html(cleaned_html)
        # 剪枝已经把导航/侧栏砍掉了,这一遍让 trafilatura 走召回优先:
        # 它不必再自己纠结"这块是不是噪声",噪声在上一步就没了。
        b_md, b_txt = _tra_run(pruned, url, res.min_size, recall=True)
        pick, md, txt = _prefer(a_md, a_txt, b_md, b_txt, tree)
        if pick == "B":
            engine = "trafilatura+prune"
            res.notes.append("第一套欠抽/结构被抹平,改用剪枝后的第二套结果")
        else:
            res.notes.append("跑了第二套但第一套更好,维持第一套")

    if not txt and not md:
        md = _fallback_markdown(tree)
        txt = re.sub(r"\n{3,}", "\n\n", re.sub(r"[#*`>|-]", " ", md)).strip()
        engine = "fallback" if md else "none"
        if md:
            res.notes.append("trafilatura 未产出,走自带兜底发射器(结构较粗)")

    res.markdown, res.text, res.engine = md, txt, engine
    res.chars = len(txt or md)

    if include_links:
        res.links = extract_links(tree, base_url=url)
    if include_tables:
        res.tables = extract_tables(tree, min_score=table_min_score)

    if cite and res.markdown:
        res.markdown, res.references = cite_links(res.markdown, url)

    return res


def to_markdown(html, url: str = "", **kw) -> str:
    """便捷入口:只要 markdown。"""
    return extract(html, url, **kw).markdown


def extract_text(html, url: str = "", **kw) -> str:
    """便捷入口:只要干净纯文本。

    这个签名是给 scripts/intel_radar/{eagle_fetch,arsenal_enrich,daily_report_v3}.py
    做**平替**用的 —— 它们现在各自裸调 trafilatura.extract(参数各不相同,
    且全部踩着上面两个中文坑)。迁移过来后正文逻辑就只剩这一份。
    """
    return extract(html, url, include_links=False, include_tables=False, **kw).text


# ═══════════════════════════════════════════════════════════════════════════
# 10. 自测 —— 中文样本必须过。跑法:
#     python scripts/crawl_core/extract.py           离线自测(不联网)
#     python scripts/crawl_core/extract.py --live    额外真抓几个线上页面
# ═══════════════════════════════════════════════════════════════════════════

_FIXTURE_CN = """<html><head><title>桂枝汤方义浅析 - 某某中医网</title></head><body>
<header id="site-header"><a href="/">首页</a><a href="/f">方剂</a><a href="/y">医案</a></header>
<nav class="main-nav"><ul><li><a href="/a">伤寒</a></li><li><a href="/b">金匮</a></li>
<li><a href="/c">温病</a></li><li><a href="/d">本草</a></li></ul></nav>
<div class="sidebar"><h3>热门推荐</h3><ul>
<li><a href="/1">小柴胡汤</a></li><li><a href="/2">四逆汤</a></li>
<li><a href="/3">白虎汤</a></li><li><a href="/4">理中丸</a></li></ul></div>
<article class="article-content">
<h1>桂枝汤方义浅析</h1>
<p>桂枝汤由桂枝、芍药、生姜、大枣、炙甘草五味组成,为群方之魁,主治太阳中风表虚证。
桂枝辛温解肌发表,芍药酸寒敛阴和营,一散一收,调和营卫,乃仲景组方之典范。</p>
<p>参见<a href="/x">伤寒论太阳病篇</a>与<a href="/y">柯琴伤寒来苏集</a>。</p>
<h2>组成与用量</h2>
<table><thead><tr><th>药名</th><th>用量</th><th>性味</th><th>方中之职</th></tr></thead>
<tbody>
<tr><td>桂枝</td><td>三两</td><td>辛甘温</td><td>君</td></tr>
<tr><td>芍药</td><td>三两</td><td>苦酸微寒</td><td>臣</td></tr>
<tr><td>炙甘草</td><td>二两</td><td>甘平</td><td>佐使</td></tr>
</tbody></table>
<h2>煎服法要点</h2>
<ul><li>微火煮取三升</li><li>适寒温服一升</li><li>啜热稀粥以助药力</li></ul>
<pre><code>剂量换算:汉一两 ≈ 13.8g(李时珍折算取一说)</code></pre>
<blockquote>太阳中风,阳浮而阴弱,啬啬恶寒,淅淅恶风,翕翕发热,桂枝汤主之。</blockquote>
</article>
<div class="related-posts"><h3>相关阅读</h3><ul>
<li><a href="/r1">麻黄汤</a></li><li><a href="/r2">葛根汤</a></li>
<li><a href="/r3">大青龙汤</a></li></ul></div>
<footer class="site-footer">版权所有 某某中医网 · 编辑:某小编 · 备案号12345</footer>
</body></html>"""

# 短条目样本:正文只有 100 字出头,正好卡在 trafilatura 默认 250 门槛的窄带里。
# 平台语料里大量是这种(方剂条 / 医案短篇 / 古籍片段)。
_FIXTURE_CN_SHORT = """<html><head><title>四逆汤</title></head><body>
<nav><a href="/">首页</a><a href="/f">方剂</a></nav>
<article class="post-body">
<h1>四逆汤</h1>
<p>甘草二两,干姜一两半,附子一枚。以水三升,煮取一升二合,去滓,分温再服。</p>
<p>参见<a href="/x">伤寒论少阴篇</a>与<a href="/y">郑钦安医理真传</a>。</p>
<h2>主治</h2>
<ul><li>少阴病,脉沉者</li><li>四肢厥逆,恶寒蜷卧</li></ul>
</article>
<footer>编辑:某小编</footer></body></html>"""

_FIXTURE_EN = """<html><head><title>Guizhi Decoction</title></head><body>
<nav><a href="/">Home</a><a href="/f">Formulas</a><a href="/c">Cases</a></nav>
<article class="article-content"><h1>Guizhi Decoction</h1>
<p>Guizhi decoction consists of cinnamon twig, peony, ginger, jujube and honey-fried
licorice. It harmonizes ying and wei and treats the taiyang wind-stroke pattern with
an exterior deficiency presentation, and is regarded as the chief of all formulas.</p>
<p>See <a href="/x">Shanghanlun taiyang chapter</a> and <a href="/y">Ke Qin annotations</a>.</p>
<h2>Composition</h2><ul><li>Cinnamon twig 3 liang</li><li>Peony 3 liang</li></ul>
</article><footer>Copyright some TCM site</footer></body></html>"""

_TABLE_CN = ("<table><thead><tr><th>药名</th><th>用量</th><th>性味</th><th>方中之职</th></tr></thead>"
             "<tbody><tr><td>桂枝</td><td>三两</td><td>辛甘温</td><td>君</td></tr>"
             "<tr><td>芍药</td><td>三两</td><td>苦酸微寒</td><td>臣</td></tr>"
             "<tr><td>甘草</td><td>二两</td><td>甘平</td><td>佐使</td></tr></tbody></table>")
_TABLE_CN_BARE = (_TABLE_CN.replace("<thead>", "").replace("</thead>", "")
                  .replace("<tbody>", "").replace("</tbody>", "")
                  .replace("<th>", "<td>").replace("</th>", "</td>"))
_TABLE_EN = ("<table><thead><tr><th>Herb</th><th>Dose</th><th>Nature</th><th>Role</th></tr></thead>"
             "<tbody><tr><td>Cinnamon twig</td><td>3 liang</td><td>acrid sweet warm</td><td>sovereign</td></tr>"
             "<tr><td>White peony</td><td>3 liang</td><td>bitter sour cool</td><td>minister</td></tr>"
             "<tr><td>Licorice</td><td>2 liang</td><td>sweet neutral</td><td>envoy</td></tr></tbody></table>")
_TABLE_EN_BARE = (_TABLE_EN.replace("<thead>", "").replace("</thead>", "")
                  .replace("<tbody>", "").replace("</tbody>", "")
                  .replace("<th>", "<td>").replace("</th>", "</td>"))
# 三种典型布局表(排版用,不是数据):作者自己标了 role / 嵌套 table / 图文两列
_TABLE_LAYOUT = ("<table role='presentation'><tr><td><img src='/logo.png'></td>"
                 "<td><a href='/'>首页</a></td></tr></table>")
_TABLE_LAYOUT_NEST = ("<table><tr><td><table><tr><td>菜单</td></tr></table></td>"
                      "<td><a href='/'>首页</a><a href='/f'>方剂</a></td></tr></table>")
_TABLE_LAYOUT_IMG = ("<table><tr><td><img src='/a.png'></td><td>广告位</td></tr>"
                     "<tr><td><img src='/b.png'></td><td>广告位二</td></tr></table>")


def _selftest() -> int:
    fails = []
    total = [0]

    def check(name, cond, detail=""):
        total[0] += 1
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               ("  ← " + detail) if detail else ""))
        if not cond:
            fails.append(name)

    print("环境:trafilatura=%s  lxml=%s  CJK补丁=%s"
          % (getattr(trafilatura, "__version__", "缺失") if trafilatura else "缺失",
             etree.__version__, "已装" if _CJK_PATCH_ON else "未装"))

    # ── ① 中文长文:导航/侧栏/页脚必须剥净,结构必须保住 ──────────────────
    print("\n① 中文长文正文提取")
    r = extract(_FIXTURE_CN, url="https://example.cn/fangji/guizhi", cite=False)
    print("   engine=%s chars=%d cjk_ratio=%.2f min_size=%d 链接%d 表格%d"
          % (r.engine, r.chars, r.cjk_ratio, r.min_size, len(r.links), len(r.tables)))
    print("   markdown 前 3 行:")
    for line in [x for x in r.markdown.split("\n") if x.strip()][:3]:
        print("     " + line[:70])
    noise = [w for w in ("热门推荐", "相关阅读", "版权所有", "某小编", "备案号") if w in r.markdown]
    check("导航/侧栏/页脚/相关阅读全部剥净", not noise, "残留:%s" % noise if noise else "")
    check("一级标题保住", r.markdown.startswith("# 桂枝汤方义浅析"),
          repr(r.markdown[:24]))
    check("二级标题保住", "## 组成与用量" in r.markdown)
    check("列表保住", "- 微火煮取三升" in r.markdown)
    check("交叉引用段(短中文锚)没被链接密度吞掉", "伤寒论太阳病篇" in r.markdown)
    check("表格进 markdown", "| 药名 |" in r.markdown or bool(r.tables))
    check("结构化表格抽到 1 张", len(r.tables) == 1,
          "score=%s" % (r.tables[0]["score"] if r.tables else "无"))
    if r.tables:
        check("表格 3 行 4 列", r.tables[0]["metadata"]["row_count"] == 3
              and r.tables[0]["metadata"]["column_count"] == 4,
              str(r.tables[0]["metadata"]))

    # ── ② 中文短条目:MIN_EXTRACTED_SIZE 动态下调的效果(单变量对照)────────
    print("\n② 中文短条目(卡在 250 门槛窄带里的那一类)")
    r2 = extract(_FIXTURE_CN_SHORT, url="https://example.cn/f/sini")
    base_md = ""
    if trafilatura is not None:
        base_md = trafilatura.extract(_FIXTURE_CN_SHORT, output_format="markdown",
                                      include_links=True, include_tables=True,
                                      include_comments=False) or ""
    print("   上游默认(250)输出 %d 字,含 markdown 结构标记=%s"
          % (len(base_md), bool(re.search(r"(^|\n)#{1,6} ", base_md))))
    print("   本模块(min=%d)输出 %d 字,engine=%s" % (r2.min_size, r2.chars, r2.engine))
    print("   本模块 markdown:" + repr(r2.markdown[:60]))
    check("短条目触发了结构抢救(门槛降到地板)", r2.min_size == MIN_SIZE_FLOOR,
          "min_size=%d notes=%s" % (r2.min_size, r2.notes))
    check("短条目仍保住一级标题", r2.markdown.lstrip().startswith("# 四逆汤"),
          repr(r2.markdown[:20]))
    check("短条目导航/编辑署名剥净", "首页" not in r2.markdown and "某小编" not in r2.markdown)
    check("上游默认配置在同一页确实丢结构(对照组)",
          not re.search(r"(^|\n)#{1,6} ", base_md), repr(base_md[:30]))

    # ── ③ 英文不回归:CJK 补丁对纯 ASCII 必须字节级等价 ────────────────────
    print("\n③ 英文对照(补丁不许影响英文产线)")
    if _tra_hp is not None and _orig_collect_link_info is not None:
        _tra_hp.collect_link_info = _orig_collect_link_info
        en_off = trafilatura.extract(_FIXTURE_EN, output_format="markdown",
                                     include_links=True, include_comments=False,
                                     config=_tra_config(250)) or ""
        _tra_hp.collect_link_info = _cjk_collect_link_info
        en_on = trafilatura.extract(_FIXTURE_EN, output_format="markdown",
                                    include_links=True, include_comments=False,
                                    config=_tra_config(250)) or ""
        print("   补丁前 %d 字 / 补丁后 %d 字" % (len(en_off), len(en_on)))
        check("英文页面补丁前后逐字节相同", en_off == en_on)
    re_ = extract(_FIXTURE_EN, url="https://example.com/guizhi")
    check("英文页 min_size 仍是上游默认 250", re_.min_size >= 240, str(re_.min_size))
    check("英文页正文提出来了", "harmonizes ying and wei" in re_.text, re_.engine)

    # ── ④ 链接密度补丁的单变量函数级对照(这是【坑二】的直接证据)──────────
    print("\n④ 链接密度补丁 · 函数级单变量对照(True = 判定链接农场→整段删)")
    if _tra_hp is not None and _orig_collect_link_info is not None:
        def mk(a, b):
            root = lhtml.fromstring(
                "<body><p>参见<a href='/x'>%s</a>与<a href='/y'>%s</a>。</p><p>后文</p></body>" % (a, b))
            p = root.find(".//p")
            for el in p.findall(".//a"):
                el.tag = "ref"
            return p
        cases = [("中文 7 字锚 x2", "伤寒论太阳病篇", "柯琴伤寒来苏集", True),
                 ("英文 8 字符锚 x2", "Shanghan", "KeQinNot", False),
                 ("英文 23 字符锚 x2", "Shanghanlun taiyang ch.", "Ke Qin Laisuji annotat", False)]
        for label, a, b, expect_rescue in cases:
            p = mk(a, b)
            txt = _tra_trim(p.text_content())
            _tra_hp.collect_link_info = _orig_collect_link_info
            r0 = _tra_hp.link_density_test(p, txt, False)[0]
            _tra_hp.collect_link_info = _cjk_collect_link_info
            r1 = _tra_hp.link_density_test(p, txt, False)[0]
            print("   %-16s 段长%2d  补丁前删=%-5s 补丁后删=%-5s" % (label, len(txt), r0, r1))
            if expect_rescue:
                check("中文短锚交叉引用段被救回", r0 is True and r1 is False)
            else:
                check("%s 行为不变" % label, r0 == r1)
        _tra_hp.collect_link_info = _cjk_collect_link_info

    # ── ⑤ 表格打分:中英对照 + 布局表必须挡掉 ─────────────────────────────
    print("\n⑤ 表格 is_data_table 打分(阈值 %.1f)" % TABLE_SCORE_THRESHOLD)

    def tscore(h):
        return score_table(_to_tree("<html><body>%s</body></html>" % h).find(".//table"))

    cards = (("中文·带thead", _TABLE_CN), ("中文·裸tr/td", _TABLE_CN_BARE),
             ("英文·带thead", _TABLE_EN), ("英文·裸tr/td", _TABLE_EN_BARE),
             ("布局表·role=presentation", _TABLE_LAYOUT),
             ("布局表·嵌套table", _TABLE_LAYOUT_NEST),
             ("布局表·图文两列", _TABLE_LAYOUT_IMG))
    for label, h in cards:
        s = tscore(h)
        print("   %-26s 分数 %5.2f  %s" % (label, s,
                                          "收" if s >= TABLE_SCORE_THRESHOLD else "弃"))
    check("中英裸表同分(证明裸表掉分与语言无关,是缺 thead/th)",
          abs(tscore(_TABLE_CN_BARE) - tscore(_TABLE_EN_BARE)) < 0.01,
          "中 %.2f / 英 %.2f" % (tscore(_TABLE_CN_BARE), tscore(_TABLE_EN_BARE)))
    check("裸数据表被第 12 项救回(≥3列纯文字网格)",
          tscore(_TABLE_CN_BARE) >= TABLE_SCORE_THRESHOLD, "%.2f" % tscore(_TABLE_CN_BARE))
    for label, h in (("role=presentation", _TABLE_LAYOUT), ("嵌套table", _TABLE_LAYOUT_NEST),
                     ("图文两列", _TABLE_LAYOUT_IMG)):
        check("布局表(%s)被挡掉" % label, tscore(h) < TABLE_SCORE_THRESHOLD, "%.2f" % tscore(h))
    md_t = table_to_markdown(extract_table_data(
        _to_tree("<html><body>%s</body></html>" % _TABLE_CN).find(".//table")))
    print("   中文表 markdown 首两行:\n     " + "\n     ".join(md_t.split("\n")[:2]))
    check("中文管道表格表头分隔行正确", md_t.split("\n")[1].startswith("| --- |"))

    # ── ⑥ 链接集合 ────────────────────────────────────────────────────────
    print("\n⑥ 链接集合")
    links = extract_links(_FIXTURE_CN, base_url="https://example.cn/fangji/guizhi")
    ext = [l for l in links if l["rel"] == "external"]
    print("   共 %d 条,internal=%d external=%d,前 3 条:%s"
          % (len(links), len(links) - len(ext), len(ext),
             [l["url"] for l in links[:3]]))
    check("链接已绝对化", all(l["url"].startswith("http") for l in links))
    check("链接去重保序", len({l["url"] for l in links}) == len(links))

    # ── ⑦ 引用编号 ⟨N⟩ + References ──────────────────────────────────────
    print("\n⑦ 引用编号 + References(喂 LLM / RAG 用)")
    rc = extract(_FIXTURE_CN, url="https://example.cn/fangji/guizhi", cite=True)
    print("   正文片段:" + repr(
        [x for x in rc.markdown.split("\n") if "⟨" in x][:1]))
    print("   References:" + repr(rc.references[:80]))
    check("正文里链接换成了 ⟨N⟩", "⟨1⟩" in rc.markdown)
    check("正文里不再有裸 URL 括号", "](/x)" not in rc.markdown)
    check("文末 References 生成", "## References" in rc.references
          and "https://example.cn/x" in rc.references)

    # ── ⑧ 剪枝算法单独可用(第二意见)──────────────────────────────────────
    print("\n⑧ PruningContentFilter(第二套独立算法,不分词,中文安全)")
    pruned = prune_html(_FIXTURE_CN)
    kept = [w for w in ("桂枝汤由桂枝", "热门推荐", "相关阅读", "版权所有") if w in pruned]
    print("   剪枝后 %d 字节;保留标记:%s" % (len(pruned), kept))
    check("剪枝保住正文", "桂枝汤由桂枝" in pruned)
    check("剪枝砍掉侧栏/页脚", "热门推荐" not in pruned and "版权所有" not in pruned)

    # ── ⑨ 域名样板学习(firecrawl OMCE 思路的自写轻量版)────────────────────
    print("\n⑨ 域名样板签名学习(自写,零服务)")
    page2 = _FIXTURE_CN.replace("桂枝汤方义浅析", "麻黄汤方义浅析").replace(
        "桂枝汤由桂枝、芍药、生姜、大枣、炙甘草五味组成", "麻黄汤由麻黄、桂枝、杏仁、炙甘草四味组成")
    sigs = learn_domain_boilerplate([_FIXTURE_CN, page2])
    print("   学到 %d 条签名,前 3 条:%s" % (len(sigs), sigs[:3]))
    check("学到了样板签名", len(sigs) >= 1)
    tree = _to_tree(_FIXTURE_CN)
    n = apply_domain_rules(tree, "https://example.cn/x", {"example.cn": sigs})
    left = _text_of(tree)
    check("套用后侧栏/页脚被删而正文还在",
          "桂枝汤由桂枝" in left and "热门推荐" not in left, "删了 %d 个块" % n)

    # ── ⑩ 健壮性 ─────────────────────────────────────────────────────────
    print("\n⑩ 健壮性")
    for label, bad in (("空串", ""), ("非HTML", "not html at all"),
                       ("残缺标签", "<div><p>孤立段落"),
                       ("带 encoding 声明", '<?xml version="1.0" encoding="gbk"?><html><body><p>甲乙丙</p></body></html>')):
        try:
            rr = extract(bad, url="https://x.cn/a")
            print("   %-16s → engine=%s chars=%d" % (label, rr.engine, rr.chars))
        except Exception as e:                                     # noqa: BLE001
            check("%s 不抛异常" % label, False, "%s: %s" % (type(e).__name__, e))
        else:
            check("%s 不抛异常" % label, True)

    print("\n══ 离线自测:%d 项断言,通过 %d,失败 %d ══"
          % (total[0], total[0] - len(fails), len(fails)))
    if fails:
        print("   失败清单:" + " / ".join(fails))
    return 1 if fails else 0


def _live(urls):
    """真抓线上页面走一遍全链路(用来验证不是只在自造样本上好看)。

    取网直接复用已有的 fetch 层(scripts/intel_radar/eagle_fetch.py 三级降级),
    **不在这里新造一份取网实现**(平台铁律:同一份逻辑只许有一份实现)。
    国内直连 github 会超时,靠环境变量里的代理。
    """
    import urllib.request
    print("\n══ 真抓线上页面 ══ 代理=%s" % (os.environ.get("HTTPS_PROXY") or "无"))
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
            raw = urllib.request.urlopen(req, timeout=45).read()
            html = raw.decode("utf-8", "replace")
        except Exception as e:                                     # noqa: BLE001
            print("  %-52s 取网失败 %s: %s" % (u[:52], type(e).__name__, str(e)[:60]))
            continue
        r = extract(html, url=u, cite=False)
        head = [x for x in r.markdown.split("\n") if x.strip()][:2]
        print("  %-52s HTML %6d 字节 → 正文 %5d 字 engine=%s cjk=%.2f min=%d 链接%d 表%d"
              % (u[:52], len(html), r.chars, r.engine, r.cjk_ratio, r.min_size,
                 len(r.links), len(r.tables)))
        print("      标题:%s" % r.title[:60])
        for line in head:
            print("      | " + line[:78])


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    rc = _selftest()
    if "--live" in sys.argv:
        _live([
            "https://github.com/trending?since=daily",
            "https://zh.wikipedia.org/wiki/%E6%A1%82%E6%9E%9D%E6%B1%A4",
            "https://www.zhongyoo.com/name/guizhitang_1.html",
        ])
    sys.exit(rc)
