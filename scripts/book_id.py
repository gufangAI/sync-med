# -*- coding: utf-8 -*-
"""book_id 命名规范 —— **全仓唯一一套**，谁都从这里取，不许再写第二份。

═══════════════════════════════════════════════════════════════════════════
立此因（创始人 2026-08-28）：「一定要规范命名规则，只有统一的一套」

在此之前它有两份：
    scripts/pan_register.py  的 to_book_id()      —— 每天真正往 D1 写的那份
    scripts/pan_inventory.py 的 to_book_id()      —— 我刚建清点脚本时**照抄**的

我抄的时候还在 commit 里写了「故意复制而非 import」，理由是 pan_register 在
import 期就读环境变量并可能 sys.exit。**理由成立，做法错了** —— 正确解法是把
规则单独抽成一个零依赖的小模块（就是本文件），而不是复制一份。

规则有两份的代价不是"不整洁"：清点脚本算出的「能对上 D1 的数」会和真实注册
行为对不上，而这两个数正是用来判断"缺口有多大"的。一边改了另一边没改，
缺口数就成了假的，而没有人会发现。

═══════════════════════════════════════════════════════════════════════════
统一的命名约定（2026-07-14 起的两层结构）

    123 上一个卷目录命名为   "<book_id> <书名>"，**第一个空格前的整段就是 book_id**

    在这个前提下，book_id 允许三种写法，按顺序匹配：

      ① 中文类目前缀 + 番号      別024-0001-01  →  bie024-0001-01
         类目对照：子→zi 史→shi 別/别→bie 集→ji 經/经→jing
      ② 裸番号（三位数字开头）    301-0027-01    →  zi301-0027-01
         （内阁文库的子部番号，历史上省略了"子"）
      ③ 其余                     原样返回

    第 ③ 条是**兜底，不是规范**。落到它的名字几乎必然对不上 D1。
    实测样本 `01-0022680`（两位数字开头）就落在这里：
    规则 ① 要中文前缀、规则 ② 要三位数字，两条都不命中 → 原样返回 → D1 里没有。
    2026-08-27 那轮 pan-register 报的 `not-in-D1=14,273`，大概率主要就是这一类。

═══════════════════════════════════════════════════════════════════════════
改这个文件要注意什么

  · 放宽规则 = 把更多目录名映射成某个 book_id。**映射错了比映射不上更糟**：
    映射不上只是"这本没入库"，映射错了是"这本的图挂到了另一本书上"。
    所以任何新规则都要先在清点清单上跑一遍，看它新命中了哪些、有没有撞车，
    再动 pan_register。tests/test_book_id.py 钉着现有行为，改了就会红。
  · 零依赖是刻意的：不读环境变量、不联网、不 import 任何本仓模块。
    这样任何脚本都能在任何环境下 import 它，不会像 pan_register 那样
    在 import 期就因为缺环境变量而 sys.exit。
"""
import re

# 中文类目 → 拼音前缀。D1 里的 book_id 用拼音。
CATALOG_PREFIX = {
    "子": "zi",
    "史": "shi",
    "別": "bie",
    "别": "bie",
    "集": "ji",
    "經": "jing",
    "经": "jing",
}

# ① 中文类目前缀 + 番号
#
# 刻意**不写 CJK 区间字面量**（如 [一-鿿]）：类目前缀只有 CATALOG_PREFIX 里那 7 个
# 确定的字，用区间去匹配"任意汉字"再回头查集合，既多余又不准。
# 而且仓里 tests/test_cjk_charset.py 钉着「不许再写第二份 CJK 区间字面量」——
# 我第一版就是写了区间被它当场抓住的（2026-08-28，同类错误当晚第七次）。
# 直接把这 7 个字拼进字符类：精确、无歧义、也不需要任何豁免。
_RE_CN_CATALOG = re.compile(
    "^([" + "".join(CATALOG_PREFIX) + r"])(\d{2,3}-\d{4}.*)$")
# ② 裸番号，三位数字开头（内阁子部，历史上省了"子"）
_RE_BARE_ZI = re.compile(r"^\d{3}-")


def to_book_id(folder_name):
    """把 123 上的卷目录名转成 D1 里的 book_id。

    约定：目录名形如 "<book_id> <书名>"，取第一个空格前的整段。

    Returns:
        str —— 转换后的 book_id；无法识别时原样返回第一段（兜底，通常对不上 D1）。
    """
    name = str(folder_name)
    parts = name.split()
    tok = parts[0] if parts else name

    m = _RE_CN_CATALOG.match(tok)
    if m and m.group(1) in CATALOG_PREFIX:
        return CATALOG_PREFIX[m.group(1)] + m.group(2)

    if _RE_BARE_ZI.match(tok):
        return "zi" + tok

    return tok


def is_recognized(folder_name):
    """这个目录名是否被前两条**规范**规则识别（而不是落到兜底）。

    清点脚本用它区分「命名不合规范」与「真的是新书」——
    两者都表现为 not-in-D1，但处置完全不同：
    前者要改名或扩规则，后者要入库。
    """
    parts = str(folder_name).split()
    tok = parts[0] if parts else str(folder_name)
    m = _RE_CN_CATALOG.match(tok)
    if m and m.group(1) in CATALOG_PREFIX:
        return True
    return bool(_RE_BARE_ZI.match(tok))
