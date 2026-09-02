#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自适应定位层 —— 对方页面改版了,我们的解析也不会挖到 0 条。

═══════════════════════════════════════════════════════════════════════════
【技术来源与许可】

本模块的算法思路与关键实现移植自 **D4Vinci/Scrapling**(https://github.com/D4Vinci/Scrapling),
许可 **BSD-3-Clause** —— 允许在注明来源的前提下抄代码。对应的上游位置:

    scrapling/core/utils/_utils.py  _StorageTools.element_to_dict   → 本文件 element_fingerprint()
    scrapling/parser.py             __calculate_similarity_score    → 本文件 similarity()
    scrapling/parser.py             __calculate_dict_diff           → 本文件 _dict_similarity()(已改写)
    scrapling/parser.py             relocate                        → 本文件 relocate()
    scrapling/parser.py             find_similar / __are_alike      → 本文件 find_similar()
    scrapling/core/mixins.py        _general_selection              → 本文件 generate_css_selector()
    scrapling/core/storage.py       SQLiteStorageSystem             → 本文件 FingerprintStore(已换载体)

为什么是抄成自己的 ~500 行、而不是 `pip install scrapling`:
  ① 平台铁律「同一份逻辑只许有一份实现」—— 装了库,将来社媒线/古籍线一定会各自再包一层;
  ② 上游的指纹仓是 SQLite 单例(见下方"改写说明"),在 GitHub Actions 上等于每轮清零,
     必须换成"落进仓库、commit 回去"的 JSON,这一处非改不可;
  ③ 上游 relocate 的父节点打分有分母 bug(见 similarity() 注释),照抄会让 <html> 根节点虚高。
依赖足迹核实过:整条 adaptive 链路 **零浏览器、零 LLM、零 GPU**,只用 lxml + stdlib difflib。
符合运行环境铁律(本地禁算力/批量走 Actions/AI 只走内部免费池 —— 这里一次模型调用都没有)。

═══════════════════════════════════════════════════════════════════════════
【它治什么病】

scripts/intel_radar/arsenal_mine.py L524 有一条硬编码正则:

    _TREND_ITEM = re.compile(r'<h2[^>]*class="[^"]*lh-condensed[^"]*"...')

GitHub 哪天把 `lh-condensed` 这个 class 改名(前端改版最常见的一种),这条正则当场返回 0 条。
更坏的是它 **不会报错**:mine_trending() 只会打印一行"解析出 0 条",鹰眼那天就当"今天没有
趋势项目",跟真的没有分不开 —— 这正是 arsenal_mine 开头自己写的那种"结构性的瞎"。
(平台铁律:凡是只写进日志的缺口一律视为没人看,所以本模块另外给出 raise_drift_issue()。)

治法不是"把正则写得更宽松"(宽松正则会把导航链接当项目挖回来,arsenal_mine L522 有明确
注释拒绝这条路),而是:**正常命中时顺手把那个元素的结构指纹存下来;哪天挖到 0 条,
拿旧指纹去新页面里把同一个元素找回来。**

═══════════════════════════════════════════════════════════════════════════
【三态编排 —— 这才是"招牌能力"的骨架,不是每次都跑相似度】

    ① 正常:选择器/正则命中     → 顺手刷新指纹(指纹永远是最新形态)
    ② 降级:命中 0 条           → 才启动 relocate(),用旧指纹全树找回
    ③ 自愈:relocate 找回来了   → 立刻把新元素的指纹存回去,并打出"新选择器长什么样"

少了 ③ 就只能扛一次改版,第二次照样抓瞎 —— 这一环是白送的,别省。

═══════════════════════════════════════════════════════════════════════════
【与既有实现的边界:本模块不碰 HTTP】

抓取、退避重试、配额闸门在 arsenal_mine.py 里已经有一份成熟实现
(_get() 读 Retry-After / X-RateLimit-Reset,BudgetExhausted 配额闸门),
同目录的 crawl_core/fetch.py 则是队友交的通用抓取层(get_text / github_api_get)。
本模块 **一行生产 HTTP 都不写**,只吃调用方给的 HTML 文本 —— 这样才不会出现第二份抓取逻辑;
自测取样也优先调 crawl_core.fetch.get_text,拿不到才退回一段最小 urllib。

═══════════════════════════════════════════════════════════════════════════
【怎么接进去 —— arsenal_mine.mine_trending() 一处改动,不动解析流程】

    hits = set()
    for m in _TREND_ITEM.findall(html):          # 主解析原样不动,仍是那条精确正则
        r = _clean_repo_ref(m.strip())
        if r:
            hits.add(r)
    # ↓ 原来这里只是 print("解析出 0 条 —— 页面结构可能变了")
    from crawl_core.adaptive import harvest_trending
    hits2, state, hint = harvest_trending(html, primary=hits)
    hits = set(hits2)
    if state == "relocated":
        # 缺口必须上升,别只躺在 run log(平台铁律)
        raise_drift_issue(drift_markdown("github.com", "trending:repo_row",
                                         state, len(hits), hint, ""))

接线前先跑一次 `python adaptive.py bootstrap`,把当前真实形态的种子指纹 commit 进仓库
—— 没有"曾经成功过一次",自愈无从谈起。

═══════════════════════════════════════════════════════════════════════════
【真跑出来的数,不是"应该能行"】(2026-09-02,本机 Python 3.12)

  A. 真实 github.com/trending?since=daily(实抓 615,960 字节,当日 14 行,3398 个元素):
        改版强度                                   旧正则   本模块恢复   耗时
        ① 不改版                                    14/14     14/14      —
        ② class 改名 + article→div                   0/14     14/14     1.2s
        ③ ②之上再把 h2→h3(标签也变)                 0/14     14/14     1.2s
        ④ class 整个删掉 + h2→div(锚点特征抹干净)    0/14     14/14     2.2s
     (④ 只有配上"指纹链 + 结果校验"才成立,只锚 h2 时是 0/14 —— 见 fingerprint_chain)

  B. 通用层不是 GitHub 专用:中文古籍站风格列表页(6 本书),
     类名全换 + h3→h2 + li→article、老 CSS 选择器命中 0 → 自适应找回 **6/6**(60.8 分),
     并反推出新选择器 `#wrap > main > ul > article:nth-of-type(1) > h2`。
"""
import io
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher

try:
    import lxml.html as _LH
    from lxml import etree as _ET
except ImportError:                                              # pragma: no cover
    raise SystemExit("adaptive.py 需要 lxml(pip install lxml);它是 Scrapling 唯一的硬依赖,很轻")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# 指纹落盘位置。**必须是仓库里的文件、且要 commit 回去** —— 这是移植时最容易漏的一处:
# 上游用 SQLite 单例存本地,而我们的主战场是 GitHub Actions,runner 每轮全新,
# 本地文件不跨 run 存活。指纹不跨 run 活下来,"改版了也不失效"在我们的环境里等于零。
FP_PATH = os.environ.get("CRAWL_FP_PATH") or os.path.join(HERE, "fingerprints.json")

# relocate 的及格线(百分制)。上游默认 40,实测后沿用不动 —— 理由是真数字,不是"感觉":
# 本机实测 2026-09-02,真实 github.com/trending?since=daily,全页 3398 个元素
# (实扫 3077 个,script/style 那 321 个跳过),当日 14 行:
#     场景                                 目标元素得分   全场第二名   余量
#     ① 不改版                                100.00        70.13     29.87
#     ② class 改名 + article→div               72.01        54.77     17.24
#     ③ ②之上再把 h2→h3(标签也变)             59.79        54.77      5.02  ← 最险
# 三种场景目标都稳居第一、且都远高于 40。往下调只会放进更多误配(场景 ③ 的第二名已有
# 54.77 分),往上调到 60 会让场景 ③ 当场失手。**40 是站在这三组实测样本上的,不是拍的。**
DEFAULT_THRESHOLD = float(os.environ.get("ADAPTIVE_THRESHOLD", "40"))

# 指纹里 text 字段的截断长度。上游不截断,我们截断,理由是实打实的性能:
# SequenceMatcher 是 O(n²),而页面里 <script> 的 .text 动辄 100KB,
# 一旦拿它去跟目标文本比对,单个候选就能把一轮 relocate 拖到分钟级。
_TEXT_CAP = 500

# 全树扫描时直接跳过的标签。它们从不承载我们要找的内容,
# 但 <script>/<style> 的文本体量极大,不跳过纯属白烧 CPU。
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "path"}

_TAG_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


# ---------------------------------------------------------------------------
# 一、元素指纹:**纯结构字典,不是 hash**
# ---------------------------------------------------------------------------

def parse_html(html):
    """HTML 文本 → lxml 元素树根。已经是元素的原样返回,方便调用方复用同一棵树。"""
    if hasattr(html, "iter") and not isinstance(html, (str, bytes)):
        return html
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    return _LH.fromstring(html)


def _clean_attrs(el):
    """属性字典清洗:空串 / 纯空格的属性直接丢掉。

    为什么要洗:`class=""` `data-x=" "` 这类噪声属性在页面上遍地都是,
    留着会让后面的属性相似度被无意义的键值稀释。上游同样做了这一步。
    """
    out = {}
    for k, v in el.attrib.items():
        if not isinstance(k, str):
            continue
        v = (v or "").strip()
        if v:
            out[str(k)] = v
    return out


def _tag_of(el):
    t = getattr(el, "tag", None)
    return t if isinstance(t, str) else None


def _element_path(el):
    """从根到自己的标签名序列,如 ['html','body','div','article','h2']。

    刻意**只存标签名、不存 nth 下标、不存 class**:改版会动 class 和层数,
    但"我在 html>body>...>h2 这条脉络上"这件事大概率还成立。
    """
    chain = []
    for anc in el.iterancestors():
        t = _tag_of(anc)
        if t:
            chain.append(t)
    chain.reverse()
    t = _tag_of(el)
    if t:
        chain.append(t)
    return chain


def element_fingerprint(el):
    """把一个元素压成 8 字段的结构指纹(JSON 可直接序列化)。

    关键设计(照搬上游,这是整套自适应的地基):
      **一个选择器字符串都不存**。存的全是"改版后大概率还在"的粗特征 ——
      标签名、直接文本、祖先标签链、父节点、兄弟标签、子标签。
      class 全串不做等值比对(只进属性字典参与模糊打分),因为 class 正是改版必动的东西。

    移植改动:上游返回 tuple,这里一律 list —— tuple 不能直接进 json.dump,
    而我们必须把指纹写进仓库文件带过 run 边界。SequenceMatcher 吃 list 与吃 tuple 等效。
    """
    parent = el.getparent()
    fp = {
        "tag": _tag_of(el) or "",
        "attributes": _clean_attrs(el),
        "text": ((el.text or "").strip())[:_TEXT_CAP],
        "path": _element_path(el),
        "parent_name": _tag_of(parent) if parent is not None else None,
        "parent_attribs": _clean_attrs(parent) if parent is not None else {},
        "parent_text": (((parent.text or "").strip())[:_TEXT_CAP]
                        if parent is not None else ""),
        "siblings": ([_tag_of(c) for c in parent
                      if c is not el and _tag_of(c)] if parent is not None else []),
        "children": [_tag_of(c) for c in el if _tag_of(c)],
    }
    return fp


def fingerprint_chain(el, levels=2):
    """锚点元素 + 它的 N 级祖先,各存一份指纹 —— **这是上游没有的一层,是实测逼出来的。**

    实测(2026-09-02,真实 trending 页,当日 14 行,四种改版强度):
        场景                                  只锚 h2 时          加了指纹链之后
        ① 不改版                              100.00 分 · 14/14   同左
        ② class 改名 + article→div             72.01 分 · 14/14   同左(第 0 层就够)
        ③ ②之上再把 h2→h3(标签也变)           59.79 分 · 14/14   同左(第 0 层就够)
        ④ class **整个删掉** + h2→div          71.45 分 ·  0/14   97.20 分 · 14/14 ← 治的就是它
    场景 ④ 里目标元素掉到第 3 名(65.56 分),被一个无关的 div 以 71.45 分挤掉 ——
    因为 h2 的两个最强信号(标签名、class)被同时抹掉了,剩下的弱信号拼不过。

    但同一时刻,它的父节点 `<article class="Box-row">` **纹丝没动**。
    所以治法不是把阈值往下调(那只会让误配更多),而是**多存几个锚**:
    锚点找不回来,就用它父辈找回来,再从父辈往下取内容。
    配合 adaptive_select 的 validate 校验(能不能真抠出 owner/repo),
    场景 ④ 的恢复率从 0/14 变成 14/14。
    """
    out = [element_fingerprint(el)]
    cur = el.getparent()
    for _ in range(max(0, levels)):
        if cur is None:
            break
        t = _tag_of(cur)
        if not t or t in ("html", "body"):      # 再往上就没有区分度了
            break
        out.append(element_fingerprint(cur))
        cur = cur.getparent()
    return out


# ---------------------------------------------------------------------------
# 二、相似度打分:8 路弱信号求平均
# ---------------------------------------------------------------------------

def _ratio(a, b):
    """两段文本的相似度。空对空算满分 —— "两边都没有"本身就是一致的证据。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _seq_ratio(a, b):
    """两个标签名序列的相似度。按**元素**比不是按字符比,所以 ['div','h2'] 与
    ['div','h3'] 只错一项,而不是被拆成字符去比。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, list(a), list(b)).ratio()


def _dict_similarity(a, b):
    """属性字典的相似度。**这一路是改写过的,不是照抄。**

    上游做法:把 dict 拆成 tuple(keys) 和 tuple(values) 各跑一次 SequenceMatcher,
    各乘 0.5。这个拆法的动机是对的(SequenceMatcher 不吃 dict),但实现有两处会吃亏:
      ① tuple 比对顺序敏感 —— 属性重新排一下顺序就掉分,而属性顺序毫无语义;
      ② 值是整项全等判断 —— `class="product"` 改成 `class="product new-class"` 直接算 0 分,
         可这恰恰是**最常见的一种改版形态**(追加一个类名),白白丢分。

    所以这里改成:
      键 → 集合 Jaccard(彻底消除顺序敏感)
      值 → 按键配对,逐个 SequenceMatcher 求平均(追加类名能拿到部分分)
    两路仍各占 0.5,与上游的权重直觉保持一致。
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    ka, kb = set(a), set(b)
    key_sim = len(ka & kb) / float(len(ka | kb))
    keys = ka | kb
    val_sim = sum(_ratio(a.get(k, ""), b.get(k, "")) for k in keys) / float(len(keys))
    return 0.5 * key_sim + 0.5 * val_sim


# 单独开一路细比的属性。为什么要在属性字典之外再给它们各一票:
# 页面整体结构大改时(path 全变、父子全变),往往只剩 class/id 名字还有一半像 ——
# 多给这几路权重,就能在结构全崩的情况下把元素捞回来。上游注释里写明了这个动机。
_KEY_ATTRS = ("class", "id", "href", "src")


def similarity(fp, el, _cand=None):
    """旧指纹 fp 与候选元素 el 的相似度,0~100。

    8 路弱信号取算术平均(不加权、不向量化)—— 为什么不上向量/不做加权:
    弱信号求平均**可解释、可打日志、可人工复核**,出问题能一眼看出是哪一路崩了;
    而任何单一强特征(class 名、xpath 下标)都恰好是改版必动的那个。
    GitHub 改版通常只动 class 和层级,tag / 子标签 / 兄弟结构 / 父子关系不会同时全变。

    ★ 修了上游一个分母 bug:上游只在"原元素和候选**都有**父节点"时才 checks+=1,
      候选没有父节点(即根节点 <html>)时 score 和 checks 都不加 → 分母变小、分数虚高,
      结果 <html> 可能意外拿到高分。上游自己写了 `# score -= 0.1` 的惩罚又注释掉了。
      这里显式给"缺父节点的候选"补 checks(score 不加),让它老实拿 0 分。
    """
    cand = _cand if _cand is not None else element_fingerprint(el)
    score = 0.0
    checks = 0

    # ① 标签名
    checks += 1
    if fp.get("tag") and fp["tag"] == cand.get("tag"):
        score += 1.0

    # ② 直接文本(只在原元素有文本时才算 —— 原本就没文本的元素,
    #    拿"两边都空"当证据会让全页一大片空元素同分)
    if fp.get("text"):
        checks += 1
        score += _ratio(fp["text"], cand.get("text") or "")

    # ③ 属性字典整体
    checks += 1
    score += _dict_similarity(fp.get("attributes") or {}, cand.get("attributes") or {})

    # ④ class / id / href / src 各一路细比
    fa = fp.get("attributes") or {}
    ca = cand.get("attributes") or {}
    for k in _KEY_ATTRS:
        if k in fa or k in ca:
            checks += 1
            score += _ratio(fa.get(k, ""), ca.get(k, ""))

    # ⑤ 祖先标签链
    checks += 1
    score += _seq_ratio(fp.get("path") or [], cand.get("path") or [])

    # ⑥ 父节点三路(名字 / 属性 / 文本)
    if fp.get("parent_name"):
        checks += 3
        if cand.get("parent_name"):                       # ← 缺父节点时只加 checks 不加 score
            if fp["parent_name"] == cand["parent_name"]:
                score += 1.0
            score += _dict_similarity(fp.get("parent_attribs") or {},
                                      cand.get("parent_attribs") or {})
            score += _ratio(fp.get("parent_text") or "", cand.get("parent_text") or "")

    # ⑦ 兄弟标签
    checks += 1
    score += _seq_ratio(fp.get("siblings") or [], cand.get("siblings") or [])

    # ⑧ 子标签(上游 element_to_dict 存了 children 却没拿它打分,这里补上一路:
    #    "我底下挂着一个 <a>" 这条特征在列表页里区分度很高,而且改版一般不动它)
    checks += 1
    score += _seq_ratio(fp.get("children") or [], cand.get("children") or [])

    if not checks:
        return 0.0
    return round(score / checks * 100.0, 2)


# ---------------------------------------------------------------------------
# 三、relocate:全树扫描 + 分数分桶 + 取最高分那一桶
# ---------------------------------------------------------------------------

def relocate(root, fp, threshold=None, debug=False):
    """拿旧指纹在新页面里把元素找回来。**返回 list,不是单个元素,也不是 None。**

    两个刻意的设计:
      · 即使某个候选拿了 100 分也**不提前退出** —— 列表页里几十行结构近似,
        并列最高分的往往就是整份列表,提前 break 只能捞回第一行。
      · 返回整个最高分桶(list)。上游踩过"返回 None 导致调用方 IndexError"的坑
        (tests/parser/test_adaptive.py::test_relocation_auto_save_no_match_above_threshold),
        所以找不到时返回 [] 而不是 None,调用方一律用真值判断。

    复杂度 O(n·depth):每个候选都要重算一次指纹,而祖先链是向上递归。
    实测(本机 Python 3.12,真实 github.com/trending 页,扫 3077 个元素):**1.16~1.36 秒/趟**,
    指纹链最多试 3 层 → 最坏约 4 秒。而且它**只在主解析挖到 0 条时才跑**,
    正常日子一次都不跑。这点纯 CPU 活跑在 Actions runner 上完全无感,
    不违反"本地禁算力"(生产本来就在云端)。
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else threshold
    buckets = {}
    for el in root.iter():
        tag = _tag_of(el)
        if not tag or tag in _SKIP_TAGS:
            continue
        s = similarity(fp, el)
        buckets.setdefault(s, []).append(el)
    if not buckets:
        return []
    ranked = sorted(buckets.keys(), reverse=True)
    if debug:
        for s in ranked[:5]:
            sample = buckets[s][0]
            print("    relocate 候选 %6.2f 分 ×%d  %s" %
                  (s, len(buckets[s]), generate_css_selector(sample)))
    best = ranked[0]
    if best < threshold:
        return []
    return buckets[best]


# ---------------------------------------------------------------------------
# 四、find_similar:一个锚点 → 拉出整列表的同构兄弟行
# ---------------------------------------------------------------------------

def _attr_alike(a, b, ignore, threshold, match_text):
    """两个元素"像不像"。

    分母取 max(len(a属性), len(b属性)) 而不是 len(a) —— 这是为了**惩罚多带属性的候选**:
    否则一个只有 1 个属性的候选,只要那 1 个属性对上就是满分,凭分母小虚高。
    """
    aa = {k: v for k, v in _clean_attrs(a).items() if k not in ignore}
    bb = {k: v for k, v in _clean_attrs(b).items() if k not in ignore}
    if not aa and not bb:
        s = 1.0
    else:
        total = sum(_ratio(v, bb.get(k, "")) for k, v in aa.items())
        s = total / float(max(len(aa), len(bb)))
    if match_text:
        s = (s + _ratio((a.text or "").strip(), (b.text or "").strip())) / 2.0
    return s >= threshold


def find_similar(el, ignore_attributes=("href", "src"), threshold=0.2,
                 match_text=False, limit=500):
    """给一个锚点元素,把同一列表里的其余行全部拉出来(含锚点自己)。

    这是治 trending 的**主武器**,比 relocate 更对症:
      relocate 是"把那一个找回来",find_similar 是"拿一行找出其余 N 行"。
      github.com/trending 是一张同构列表(实测 2026-09-02 当日 14 行),
      组合用法 = relocate 找回锚点行 → find_similar 展开全列表 → 每行内部再取 a[href]。
      全程不依赖 `lh-condensed` 这个 class 名。

    粗筛用一条构造出来的 XPath:同深度 + 同三级标签路径(祖父/父/自己)。
    细筛才用属性相似度。默认忽略 href/src,因为列表里每行的 URL 天然都不同、当特征不可靠;
    阈值 0.2 很松是有意的 —— 粗筛那条 XPath 已经足够硬。
    """
    ignore = set(ignore_attributes or ())
    tag = _tag_of(el)
    if not tag or not _TAG_OK.match(tag):
        return [el]
    depth = len(list(el.iterancestors()))
    parent = el.getparent()
    gp = parent.getparent() if parent is not None else None
    chain = [t for t in (_tag_of(gp) if gp is not None else None,
                         _tag_of(parent) if parent is not None else None, tag)
             if t and _TAG_OK.match(t)]
    xp = "//" + "/".join(chain) + "[count(ancestor::*) = %d]" % depth
    root = el.getroottree()
    try:
        cands = root.xpath(xp)
    except _ET.XPathEvalError:
        return [el]
    out = [c for c in cands[:limit]
           if c is el or _attr_alike(el, c, ignore, threshold, match_text)]
    return out or [el]


# ---------------------------------------------------------------------------
# 五、反向生成选择器:把"新页面的正确选择器"交到人手上
# ---------------------------------------------------------------------------

def _selector_parts(el, full_path=False):
    parts = []
    cur = el
    while cur is not None:
        tag = _tag_of(cur)
        if not tag or tag == "html":
            break
        parent = cur.getparent()
        if parent is None:
            parts.append((tag, 0, None))
            break
        eid = (cur.get("id") or "").strip()
        if eid and not full_path and " " not in eid:
            parts.append((tag, 0, eid))          # 有 id 直接短路,不必再往上走
            break
        same = [c for c in parent if _tag_of(c) == tag]
        idx = (same.index(cur) + 1) if len(same) > 1 else 0
        parts.append((tag, idx, None))
        cur = parent
    parts.reverse()
    return parts


def generate_css_selector(el, full_path=False):
    """从元素倒推一条 CSS 选择器,如 `div > article:nth-of-type(3) > h2`。

    **刻意不用 class**:上游注释里写明理由 —— 很多网站在毫不相干的元素之间共用同一份 class,
    拿 class 生成的选择器看着漂亮,实际会一次选中一大片。
    这条选择器的用途不是拿去当生产解析器,而是 **relocate 成功后打进日志/Issue**,
    让人一眼看到"改版后的正确形态长什么样",顺手把 arsenal_mine 里那条硬编码正则升级掉,
    而不是靠相似度一直兜底 —— 兜底是应急,不是终态。
    """
    out = []
    for tag, idx, eid in _selector_parts(el, full_path):
        if eid:
            out.append("#" + eid)
        elif idx:
            out.append("%s:nth-of-type(%d)" % (tag, idx))
        else:
            out.append(tag)
    return " > ".join(out)


def generate_xpath(el, full_path=False):
    """同上,XPath 版。"""
    out = []
    for tag, idx, eid in _selector_parts(el, full_path):
        if eid:
            out.append("%s[@id='%s']" % (tag, eid))
        elif idx:
            out.append("%s[%d]" % (tag, idx))
        else:
            out.append(tag)
    return "//" + "/".join(out)


# ---------------------------------------------------------------------------
# 六、指纹仓:载体从 SQLite 换成"仓库里的 JSON"
# ---------------------------------------------------------------------------

def site_key(url_or_host):
    """站点键。上游用 tld 包提主域,我们不引这个依赖 —— 抓的域就 github.com 这几个,
    直接用主机名(去掉端口和 www.)当键即可,少一个第三方包少一处装不上的风险。"""
    s = str(url_or_host or "").strip().lower()
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s or "unknown"


class FingerprintStore(object):
    """按「站点 + identifier」两级键存指纹的 JSON 仓。

    为什么不照抄上游的 SQLiteStorageSystem:
      ① **载体不对**。Actions runner 每轮全新,SQLite 文件不跨 run 存活 →
         指纹必须落成仓库里的 json 并 commit 回去,否则整套自适应在我们的环境里等于零。
      ② 上游硬性要求存储类被 @lru_cache 包装(`if not hasattr(storage,'__wrapped__'): raise`),
         还在 __del__ 里关连接 —— 解释器关闭期调用 __del__ 有风险,这两处照抄是埋雷。
      ③ orjson 换 stdlib json;指纹里的序列一律 list(见 element_fingerprint 注释)。

    落盘格式刻意 sort_keys + indent=1:这个文件要进 git,diff 必须能看懂
    ——"哪个站的哪个选择器在哪天变成了什么形态"本身就是改版史。
    """

    def __init__(self, path=None):
        self.path = path or FP_PATH
        self.data = {"version": 1, "sites": {}}
        self.dirty = False
        if os.path.isfile(self.path):
            try:
                d = json.load(io.open(self.path, encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("sites"), dict):
                    self.data = d
            except Exception as e:                                # noqa: BLE001
                # 指纹仓读坏了不许静默当空的用 —— 静默会让"自愈失效"变成隐形故障,
                # 正是本模块要治的那种瞎。出声,但不阻断抓取。
                print("  [adaptive] 指纹仓读取失败(按空仓继续):%s" % str(e)[:80])

    def get(self, site, identifier):
        return self.entry(site, identifier).get("fingerprint")

    def get_chain(self, site, identifier):
        """主指纹 + 祖先兜底指纹,按"先试锚点、锚点没了再试父辈"的顺序返回。"""
        e = self.entry(site, identifier)
        chain = [e["fingerprint"]] if e.get("fingerprint") else []
        chain.extend([f for f in (e.get("fallbacks") or []) if isinstance(f, dict)])
        return chain

    def entry(self, site, identifier):
        return ((self.data.get("sites") or {}).get(site_key(site)) or {}).get(identifier) or {}

    def put(self, site, identifier, el_or_fp, **meta):
        rec = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "updated_by": meta.pop("updated_by", "adaptive.py")}
        if isinstance(el_or_fp, dict):
            rec["fingerprint"] = el_or_fp
        else:
            chain = fingerprint_chain(el_or_fp)
            rec["fingerprint"] = chain[0]
            rec["fallbacks"] = chain[1:]     # 祖先兜底,见 fingerprint_chain 的实测表
            # 顺手记下"这一刻它的选择器长什么样",供人工对照改版前后的差别
            rec["hint_css"] = generate_css_selector(el_or_fp)
            rec["hint_xpath"] = generate_xpath(el_or_fp)
        rec.update({k: v for k, v in meta.items() if v is not None})
        self.data.setdefault("sites", {}).setdefault(site_key(site), {})[identifier] = rec
        self.dirty = True
        return rec

    def save(self, force=False):
        """原子写(先写 .tmp 再 replace)。半截文件比没文件更难查。"""
        if not (self.dirty or force):
            return False
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = self.path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        self.dirty = False
        return True


# ---------------------------------------------------------------------------
# 七、三态编排(通用入口)
# ---------------------------------------------------------------------------

class AdaptiveResult(object):
    """一次定位的结果。state ∈ primary / relocated / lost。

    刻意做成对象而不是裸 list:调用方**必须**能区分"今天真的没有内容"和"解析崩了",
    这两件事在旧实现里是同一个空列表,分不开 —— 那正是要治的病。
    """

    __slots__ = ("elements", "state", "score", "hint_css", "hint_xpath", "note")

    def __init__(self, elements, state, score=0.0, hint_css="", hint_xpath="", note=""):
        self.elements = elements
        self.state = state
        self.score = score
        self.hint_css = hint_css
        self.hint_xpath = hint_xpath
        self.note = note

    def __bool__(self):
        return bool(self.elements)

    def __len__(self):
        return len(self.elements)

    def __repr__(self):
        return "<AdaptiveResult %s n=%d score=%.1f>" % (self.state, len(self.elements), self.score)


def adaptive_select(html, store, site, identifier, css=None, xpath=None, picker=None,
                    threshold=None, expand=False, verbose=True, debug=False,
                    validate=None):
    """通用三态编排。给任何站点的任何一处解析用,不只 trending。

    参数三选一决定"正常路径"怎么选元素:
        css     CSS 选择器(需要 cssselect,已在用)
        xpath   XPath
        picker  一个 callable(root) -> [元素],用于"正常路径其实是正则/自定义逻辑"的场景
                (arsenal_mine 的 trending 正是这一类 —— 它的主解析是正则,不是选择器)

    expand=True 时,relocate 找回锚点后再用 find_similar 展开成整列表。
    validate 是一个 callable(elements) -> bool:**"捞回来了"不等于"捞对了"**。
        实测场景 ④(见 fingerprint_chain 注释)里,relocate 高高兴兴返回了 3 个 71.45 分的元素,
        可那 3 个元素里一个 owner/repo 都抠不出来 —— 没有校验就会把垃圾当成果交出去。
        校验不过就换下一个兜底指纹(祖先),都不过才算 lost。
    """
    root = parse_html(html)
    els = []
    try:
        if picker is not None:
            els = list(picker(root) or [])
        elif css:
            els = list(root.cssselect(css))
        elif xpath:
            els = list(root.xpath(xpath))
    except Exception as e:                                        # noqa: BLE001
        if verbose:
            print("  [adaptive] 主解析抛异常(转入自愈):%s" % str(e)[:100])
        els = []

    # ① 正常命中 → 顺手刷新指纹,让指纹永远是最新形态
    if els:
        store.put(site, identifier, els[0], source=css or xpath or "picker",
                  hits=len(els), state="primary")
        return AdaptiveResult(els, "primary", 100.0,
                              generate_css_selector(els[0]), generate_xpath(els[0]))

    # ② 命中 0 条 → 才启动 relocate(不是每次都跑相似度:又慢又容易误配)
    chain = store.get_chain(site, identifier)
    if not chain:
        if verbose:
            print("  [adaptive] %s/%s 挖到 0 条,且**没有历史指纹**可用 —— "
                  "自愈这次帮不上,得先有一次成功命中把指纹存下来" % (site, identifier))
        return AdaptiveResult([], "lost", 0.0, note="no-fingerprint")

    thr = DEFAULT_THRESHOLD if threshold is None else threshold
    for level, fp in enumerate(chain):
        got = relocate(root, fp, threshold=threshold, debug=debug)
        if not got:
            if verbose:
                print("  [adaptive] %s/%s 第 %d 层指纹:全场最高分低于阈值 %.0f"
                      % (site, identifier, level, thr))
            continue
        best_score = similarity(fp, got[0])
        if expand:
            wide = find_similar(got[0])
            if len(wide) > len(got):
                seen, merged = set(), []
                for e in list(got) + list(wide):    # 保文档序、去重(用 id 判同一节点)
                    if id(e) not in seen:
                        seen.add(id(e))
                        merged.append(e)
                got = merged
        if validate is not None and not validate(got):
            if verbose:
                print("  [adaptive] %s/%s 第 %d 层指纹捞回 %d 个但**校验没过**(捞错了),"
                      "换下一层兜底" % (site, identifier, level, len(got)))
            continue

        hint_css = generate_css_selector(got[0])
        hint_xpath = generate_xpath(got[0])
        # ③ 自愈:把改版后的新形态存回去。少了这一步只能扛一次改版。
        store.put(site, identifier, got[0], state="relocated", score=best_score,
                  hits=len(got), fp_level=level, hint_css=hint_css, hint_xpath=hint_xpath)
        if verbose:
            print("  [adaptive] %s/%s 主解析 0 条 → 自适应找回 %d 个"
                  "(第 %d 层指纹,最高 %.1f 分)"
                  % (site, identifier, len(got), level, best_score))
            print("  [adaptive] 建议把硬编码解析升级成:%s" % hint_css)
        return AdaptiveResult(got, "relocated", best_score, hint_css, hint_xpath,
                              note="fp_level=%d" % level)

    if verbose:
        print("  [adaptive] %s/%s %d 层指纹全试过,都没能把内容找回来 —— "
              "这是必须有人看的缺口,别只留在 run log 里" % (site, identifier, len(chain)))
    return AdaptiveResult([], "lost", 0.0, note="all-fingerprints-failed")


# ---------------------------------------------------------------------------
# 八、落地场景:github.com/trending
# ---------------------------------------------------------------------------

TRENDING_SITE = "github.com"
TRENDING_IDENT = "trending:repo_row"

# 只用来从恢复出来的行里认 /owner/repo 形式的链接。**不是**第二份仓名清洗逻辑 ——
# 真正的清洗一律交给 arsenal_mine._clean_repo_ref(见 _cleaner())。
_HREF_REPO = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)/?$")

_CLEAN = None


def _cleaner():
    """复用 arsenal_mine 里那份 owner/repo 清洗逻辑,**不再写第二份**。

    (平台铁律:同一份逻辑只许有一份实现。历史血证 —— CJK 正则曾经有过五份互相打架的副本。)
    懒加载:让本模块在不依赖 intel_radar 的场景下也能单独用。
    """
    global _CLEAN
    if _CLEAN is None:
        p = os.path.join(ROOT, "scripts", "intel_radar")
        if p not in sys.path:
            sys.path.insert(0, p)
        try:
            from arsenal_mine import _clean_repo_ref            # noqa: PLC0415
            _CLEAN = _clean_repo_ref
        except Exception as e:                                   # noqa: BLE001
            print("  [adaptive] 取不到 arsenal_mine._clean_repo_ref(%s),"
                  "本轮只做格式校验、不做清洗" % str(e)[:60])
            _CLEAN = False
    return _CLEAN


def _rows_to_repos(rows):
    """从恢复出来的行元素里抠 owner/repo。每行只取**第一个真正是仓库**的链接。

    ★ 这里有个实测踩出来的坑,别再改回去:
      trending 里"作者开了 GitHub Sponsors"的行,**第一个链接是 `/sponsors/VoltAgent`** ——
      它长得完全符合 `/owner/repo` 的形状,但不是仓库。
      arsenal_mine._clean_repo_ref 认得它(owner 落在 _NOT_OWNER 名单里)并正确返回 None,
      可我最初写成"形状一匹配就 break",于是这类行整行被跳过。
      实测(2026-09-02 当日 14 行):**14 行里有 3 行栽在这**(VoltAgent/awesome-design-md、
      affaan-m/ECC、unclecode/crawl4ai),恢复率 11/14 →   改成"被拒就往下找"后 14/14。
      教训:复用别人的清洗函数时,**要接住它说"不"的那一路**,别只接它说"是"的。
    """
    clean = _cleaner()
    out = []
    seen = set()
    for row in rows:
        hrefs = []
        if _tag_of(row) == "a" and row.get("href"):
            hrefs.append(row.get("href"))
        hrefs.extend(row.xpath(".//a/@href"))
        for h in hrefs:
            m = _HREF_REPO.match((h or "").strip())
            if not m:
                continue
            r = clean(m.group(1)) if clean else m.group(1)
            if not r:
                continue                      # ← 被清洗函数拒了(/sponsors/、/topics/ 之类),继续往下找
            if r.lower() not in seen:
                seen.add(r.lower())
                out.append(r)
            break                             # 本行已经拿到仓库,不再往下看贡献者头像链接
    return out


def _anchor_for(root, repo):
    """在树里定位"某个仓那一行"的锚点元素。

    锚点取 <a> 的**父节点**(实测就是 <h2 class="h3 lh-condensed">)而不是 <a> 本身:
    父节点的 class 在整张列表里是共享的,find_similar 靠它一拉就是全列表;
    而 <a> 的 href 每行都不同,天然是弱特征(所以 find_similar 默认忽略 href/src)。
    """
    for a in root.xpath('//a[@href="/%s"]' % repo):
        p = a.getparent()
        return p if p is not None else a
    return None


def harvest_trending(html, primary=None, store=None, verbose=True,
                     threshold=None, save=True):
    """trending 的自适应收割。**挂在 arsenal_mine.mine_trending() 现成的分支上**。

    arsenal_mine L555-563 已经有现成挂点:

        hits = set(); [ ... _TREND_ITEM.findall(html) ... ]
        if not hits and verbose:
            print("  trending %s/%s: 解析出 0 条 —— 页面结构可能变了,该查正则")

    把那一段换成:

        from crawl_core.adaptive import harvest_trending
        hits2, state, hint = harvest_trending(html, primary=hits)
        hits = set(hits2)

    正则本身留在 arsenal_mine 不动(本模块一行都不复制它)—— 主解析仍是那条精确正则,
    宽松兜底只在它挖到 0 条时才启动,不会把导航链接当项目挖回来。

    返回 (repos:list[str], state:str, hint_css:str)。
    """
    own = store is None
    store = store or FingerprintStore()
    root = parse_html(html)

    # ① 正常命中:顺手刷新指纹。这一步是自愈能成立的前提 ——
    #    指纹必须是"改版前最后一刻的真实形态",而不是几个月前写死的样子。
    if primary:
        first = sorted(primary)[0] if isinstance(primary, (set, frozenset)) else list(primary)[0]
        el = _anchor_for(root, str(first))
        if el is not None:
            store.put(TRENDING_SITE, TRENDING_IDENT, el, state="primary", hits=len(primary))
            if save and own:
                store.save()
            if verbose:
                print("  [adaptive] trending 正常命中 %d 条,指纹已刷新(%s)"
                      % (len(primary), generate_css_selector(el)))
        return list(primary), "primary", (generate_css_selector(el) if el is not None else "")

    # ② + ③ 降级 + 自愈。validate 这一条不能省:实测有过"relocate 返回 3 个高分元素、
    #    但一个 owner/repo 都抠不出来"的情况,没校验就会把垃圾当成果交出去。
    res = adaptive_select(root, store, TRENDING_SITE, TRENDING_IDENT,
                          picker=lambda r: [], threshold=threshold,
                          expand=True, verbose=verbose,
                          validate=lambda els: bool(_rows_to_repos(els)))
    repos = _rows_to_repos(res.elements) if res.elements else []
    if save and own:
        store.save()
    if verbose and repos:
        print("  [adaptive] trending 自愈成功,恢复出 %d 个仓(前 3:%s)"
              % (len(repos), ", ".join(repos[:3])))
    return repos, res.state, res.hint_css


# ---------------------------------------------------------------------------
# 九、缺口上升:别只躺在 run log 里
# ---------------------------------------------------------------------------

def drift_markdown(site, identifier, state, n, hint_css, hint_xpath, extra=""):
    """生成一段可以直接贴进 Issue 的正文。

    平台铁律:「凡是只写进日志的产线,一律视为没人看」(血证:pan-register 连喊 12 天零响应)。
    页面改版是**必须有人处理**的缺口 —— 自愈只是买时间,真正该做的是把硬编码解析升级掉。
    """
    lines = [
        "## 抓取解析漂移:%s / %s" % (site, identifier),
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 状态 | `%s` |" % state,
        "| 本轮恢复条数 | %d |" % n,
        "| 建议 CSS | `%s` |" % (hint_css or "(无)"),
        "| 建议 XPath | `%s` |" % (hint_xpath or "(无)"),
        "| 时间 | %s |" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "自适应层已把数据兜住(本轮没漏采),但**硬编码解析已经失效**,",
        "请照上面的建议选择器把它升级掉 —— 兜底是应急,不是终态。",
    ]
    if extra:
        lines += ["", extra]
    return "\n".join(lines)


def raise_drift_issue(body, repo=None, label="crawl-drift",
                      title="抓取解析漂移(自适应层已兜住)"):
    """把漂移升级成常驻 Issue。**默认不启用**,要显式给 repo 或设 ADAPTIVE_DRIFT_REPO。

    复用 scripts/gh_issue.py 那份"复用同一个 Issue 且真的发评论"的规范实现,
    不在这里再写一份 —— 那个文件开头列了仓里已经手抄四遍的血证。
    """
    repo = repo or os.environ.get("ADAPTIVE_DRIFT_REPO") or ""
    if not repo:
        return False
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import gh_issue                                           # noqa: PLC0415
        gh_issue.upsert(repo, label, "[抓取漂移]", title, body)
        return True
    except Exception as e:                                        # noqa: BLE001
        print("  [adaptive] 升 Issue 失败(不阻断抓取):%s" % str(e)[:120])
        return False


# ---------------------------------------------------------------------------
# 自测:拿真实 github.com/trending 跑一遍"改版前 → 改版后"
# ---------------------------------------------------------------------------

def _demo_fetch(url):
    """自测/bootstrap 取样用的取页。**优先走同目录的 crawl_core.fetch**(队友那份带
    退避、限流分类、代理、配额闸门的实现),它不在时才退回一段最小 urllib ——
    退回那段只是为了让本模块在单独拷走时也能自测,不是第二份抓取实现。
    生产路径上本模块一行 HTTP 都不发:HTML 由调用方(arsenal_mine)递进来。
    """
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import fetch as _fetch                                    # noqa: PLC0415
        t = _fetch.get_text(url)
        if t:
            return t
        print("  [adaptive] crawl_core.fetch 没取到内容,退回最小 urllib 取样")
    except Exception as e:                                        # noqa: BLE001
        print("  [adaptive] crawl_core.fetch 用不了(%s),退回最小 urllib 取样"
              % str(e)[:60])
    import urllib.request                                         # noqa: PLC0415
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def _simulate_redesign(html, level=2):
    """模拟 GitHub 前端改版,按强度分三档。

    改的都是真实改版里最常动、且硬编码正则唯一依赖的东西 ——
    改完之后 arsenal_mine 的 _TREND_ITEM 必然挖到 0 条,那才是有意义的测试起点。

        level=2  类名换掉 + 外层 article→div        (最常见)
        level=3  再把 h2→h3                        (连标签都变)
        level=4  class **整个删掉** + h2→div        (锚点特征被抹干净,最狠)
    """
    h = html.replace('class="h3 lh-condensed"', 'class="TrendingRepo-title"')
    h = h.replace('<article class="Box-row">', '<div class="TrendingRepo-row">')
    h = h.replace("</article>", "</div>")
    if level >= 3:
        h = h.replace('<h2 class="TrendingRepo-title">', '<h3 class="TrendingRepo-title">')
        h = h.replace("</h2>", "</h3>")
    if level >= 4:
        # 注意这一档故意**不动** article.Box-row —— 真实改版里外层容器往往比标题稳,
        # 这正是指纹链(祖先兜底)存在的意义。
        h = html.replace('class="h3 lh-condensed"', "")
        h = h.replace("<h2 ", "<div ").replace("</h2>", "</div>")
    return h


def _self_test():
    # ★ 顺序有讲究,别调换 —— 本轮踩了两次的 Windows stdout 双重包装坑:
    #   arsenal_mine 在 **import 时**执行 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`
    #   (仓里多个脚本都是这个写法)。两处先后各包一层的后果实测有两种,都很阴:
    #     ① 先被丢弃的那个 wrapper 被 GC 时把底层 buffer 关掉 → 后面所有 print 直接抛
    #        "ValueError: I/O operation on closed file";
    #     ② 更阴的一种:不报错,但**写进旧 wrapper 还没 flush 的文本被静默丢掉** ——
    #        本自测的第一行"实抓 N 字节"就这么凭空消失过,查了半天才发现不是没执行。
    #   所以:先把 arsenal_mine 导进来(让它把 stdout 换完),**之后**再 reconfigure、再 print。
    #   (这也正是平台铁律"显示层不等于数据层"的一个活样本:少了一行输出 ≠ 那一步没跑。)
    p = os.path.join(ROOT, "scripts", "intel_radar")
    if p not in sys.path:
        sys.path.insert(0, p)
    # 复用 arsenal_mine 那条**唯一**的 trending 正则,不在这里再写一条
    from arsenal_mine import _TREND_ITEM                          # noqa: PLC0415
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                             # noqa: BLE001
        pass
    cache = os.environ.get("ADAPTIVE_HTML") or ""
    if cache and os.path.isfile(cache):
        html = io.open(cache, encoding="utf-8").read()
        print("自测:用缓存页面 %s(%d 字节)" % (cache, len(html)))
    else:
        html = _demo_fetch("https://github.com/trending?since=daily")
        print("自测:实抓 github.com/trending(%d 字节)" % len(html))

    store = FingerprintStore(os.environ.get("ADAPTIVE_FP_TEST")
                             or os.path.join(HERE, "fingerprints.json"))

    print("\n===== 第 1 步:改版前(主解析正常)=====")
    hits = sorted({m.strip() for m in _TREND_ITEM.findall(html)})
    print("  正则命中 %d 个:%s ..." % (len(hits), ", ".join(hits[:3])))
    assert hits, "正则一条都没命中,说明 GitHub 已经改版 —— 那正好该走自愈,但自测需要一个基线"
    repos1, st1, hint1 = harvest_trending(html, primary=hits, store=store)
    print("  状态=%s  指纹已存" % st1)

    a = set(hits)
    names = {2: "class 改名 + article→div",
             3: "再把 h2→h3(连标签都变)",
             4: "class 整个删掉 + h2→div(锚点特征抹干净)"}
    results = []
    last_hint, last_state, last_broken = "", "", ""
    for lv in (2, 3, 4):
        print("\n===== 第 2.%d 步:模拟改版 level=%d(%s)=====" % (lv, lv, names[lv]))
        # 每一档都从"改版前的干净指纹"重新起跑 —— 上一档自愈时把指纹更新成了改版后的形态,
        # 不重置的话下一档就是在占便宜,测出来的数字不作数。
        harvest_trending(html, primary=hits, store=store, verbose=False)
        broken = _simulate_redesign(html, level=lv)
        n_regex = len(_TREND_ITEM.findall(broken))
        print("  arsenal_mine 那条正则在改版后命中 %d 个  ← 旧实现在这里就断了" % n_regex)
        assert n_regex == 0, "模拟改版 level=%d 没生效" % lv

        t0 = time.time()
        repos, st, hint = harvest_trending(broken, primary=None, store=store, verbose=True)
        dt = time.time() - t0
        b = set(repos)
        print("  耗时 %.2f 秒,状态=%s | 改版前 %d / 恢复 %d / 交集 %d / 漏 %d / 多 %d"
              % (dt, st, len(a), len(b), len(a & b), len(a - b), len(b - a)))
        if a - b:
            print("  漏掉:%s" % ", ".join(sorted(a - b)[:5]))
        if b - a:
            print("  多出:%s" % ", ".join(sorted(b - a)[:5]))
        results.append((lv, len(a & b), len(a), dt))
        last_hint, last_state, last_broken = hint, st, broken

    print("\n===== 第 3 步:选择器反推(交到人手上,别让人自己去猜新结构)=====")
    root = parse_html(last_broken)
    repos_last, _, _ = harvest_trending(last_broken, primary=None, store=store, verbose=False)
    el = _anchor_for(root, repos_last[0]) if repos_last else None
    if el is not None:
        print("  CSS  : %s" % generate_css_selector(el))
        print("  XPath: %s" % generate_xpath(el))

    print("\n===== 第 4 步:漂移报告(平台铁律:缺口必须上升,别只躺在 run log)=====")
    print(drift_markdown(TRENDING_SITE, TRENDING_IDENT, last_state, len(repos_last),
                         last_hint, generate_xpath(el) if el is not None else ""))

    print("\n===== 结论 =====")
    ok = True
    for lv, got, tot, dt in results:
        good = got >= max(1, int(tot * 0.9))
        ok = ok and good
        print("  level=%d  恢复 %d/%d  %.2fs  %s" % (lv, got, tot, dt, "PASS" if good else "FAIL"))
    print("  总判据:每档恢复率 ≥ 90%% → %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _bootstrap():
    """把**当前真实页面**的指纹落进仓库里的 fingerprints.json。

    为什么需要这个入口:自适应的前提是"曾经成功命中过一次"。
    如果 GitHub 恰好在我们第一次部署之前就改版了,仓里没有任何指纹,自愈根本起不来
    (adaptive_select 会老老实实报 no-fingerprint,而不是假装找到了)。
    所以接线时先跑一次 `python adaptive.py bootstrap`,把种子指纹 commit 进仓库。
    之后每天正常命中都会自动刷新它,不用再手工跑。
    """
    p = os.path.join(ROOT, "scripts", "intel_radar")
    if p not in sys.path:
        sys.path.insert(0, p)
    from arsenal_mine import _TREND_ITEM                          # noqa: PLC0415
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                             # noqa: BLE001
        pass
    cache = os.environ.get("ADAPTIVE_HTML") or ""
    html = (io.open(cache, encoding="utf-8").read() if cache and os.path.isfile(cache)
            else _demo_fetch("https://github.com/trending?since=daily"))
    hits = sorted({m.strip() for m in _TREND_ITEM.findall(html)})
    if not hits:
        print("bootstrap 失败:主解析当场就是 0 条 —— GitHub 可能已经改版,"
              "此刻没有可信的形态能存。先人工确认页面结构。")
        return 1
    store = FingerprintStore()
    repos, state, hint = harvest_trending(html, primary=hits, store=store)
    store.save(force=True)
    print("bootstrap 完成:%d 条命中,指纹已写入 %s" % (len(hits), store.path))
    print("  当前形态:%s" % hint)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
        sys.exit(_bootstrap())
    sys.exit(_self_test())
