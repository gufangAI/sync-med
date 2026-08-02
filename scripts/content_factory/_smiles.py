#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SMILES ↔ 分子式 对账 —— 确定性判据,零依赖

立此因(2026-08-03 实测):生物计算被独立复核按住的 192 条里,**SMILES/结构式造假占 19%,是第一大主因**;
  典型拦截语「SMILES结构式明显伪造,分子式C21H28O11与所给超长SMILES严重不符」
  「SMILES含[N+](C)(C)C,但汉防己乙素是中性生物碱,无季铵结构」。

我先试过在提示词里写「不能确信逐个原子都对就留空字符串」—— **实测完全无效**:
  v1 带 SMILES 52%,加了这条的 v2 反而升到 65%。
  根因:我让模型**自我评估「我这个结构式对不对」**,这是它做不到的判断。
  ——「该用确定性判据的地方就必须用确定性判据」,不能靠语义自省。

本模块做的事很朴素:**从 SMILES 数原子,跟它自己写的分子式对账,对不上就把 SMILES 清掉**。
  不引 rdkit(重依赖,且本机禁算力);只用正则做有机子集解析,够抓住"严重不符"这一类。
  H 不比对 —— 隐式氢要真正的价键推断才算得准,而造假多半在 C/N/O/S 上就露馅了。

判定取向:**宁可漏过少数真结构,不放过明显对不上的**。清掉 SMILES 只是不画结构图,
  条目本身仍可发布;而放一个错结构式上去,是硬伤。
"""
import re

# 有机子集里可以不写方括号的元素(两字母的必须排在前面,否则 Cl 会被拆成 C+l)
ORGANIC = ["Cl", "Br", "B", "C", "N", "O", "P", "S", "F", "I"]
_BRACKET = re.compile(r"\[([^\]]*)\]")
# 方括号里也可能是**小写的芳香原子**(如小檗碱的 [n+]) —— 只认大写会把氮漏掉
_INBRACKET_ELEM = re.compile(r"([A-Z][a-z]?|[bcnops])")
_FORMULA = re.compile(r"([A-Z][a-z]?)(\d*)")
# 只比对这些主族元素;H 不比(隐式氢算不准)
COMPARE = ("C", "N", "O", "S", "P", "F", "Cl", "Br", "I")


def atoms_from_smiles(smi):
    """数 SMILES 里的原子。返回 {元素: 个数};无法解析返回 None。"""
    if not smi or not isinstance(smi, str):
        return None
    s = smi.strip()
    if len(s) < 2 or len(s) > 600:
        return None
    # 括号必须配平 —— 不配平说明是截断或瞎编的串
    if s.count("(") != s.count(")") or s.count("[") != s.count("]"):
        return None

    counts = {}

    def bump(e, n=1):
        counts[e] = counts.get(e, 0) + n

    # 先吃掉方括号原子(含同位素/电荷/氢数),按里面第一个元素符号计
    def take_bracket(m):
        inner = m.group(1)
        em = _INBRACKET_ELEM.search(inner)
        if em:
            e = em.group(1)
            bump(e.upper() if len(e) == 1 else e)   # 芳香小写归一到元素符号
        return " "          # 用空格占位,避免残留字母被后面误判

    rest = _BRACKET.sub(take_bracket, s)

    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch.isalpha():
            two = rest[i:i + 2]
            if len(two) == 2 and two in ORGANIC and two[1].islower():   # Cl / Br
                bump(two); i += 2; continue
            up = ch.upper()
            if up in ("C", "N", "O", "P", "S", "B", "F", "I"):
                # 小写 = 芳香原子(c/n/o/s/p),同样计入对应元素
                bump(up); i += 1; continue
            return None                                     # 出现非有机子集字母 → 判为不可解析
        i += 1

    return counts or None


def atoms_from_formula(f):
    """把 'C21H18O3' 这类分子式拆成 {元素: 个数};拆不出返回 None。"""
    if not f or not isinstance(f, str):
        return None
    s = f.strip().replace(" ", "")
    # 季铵盐/阳离子生物碱的分子式会带电荷(如小檗碱 C20H18NO4+),去掉电荷再解析,
    # 否则真货会被当成"格式异常"误杀(实测第一版就栽在小檗碱上)
    # 只去掉**符号本身**和符号之后的数字(如 "Fe2+" 的写法差异),
    # 绝不能去掉符号**之前**的数字 —— 那是原子个数。
    # 血证(2026-08-03):第一版写成 `\d*[+\-−]+$`,把小檗碱 C20H18NO4+ 的 O4 吃成 O1,
    #   于是全库 131 条里"对不上"117 条,差点据此宣布"结构式大面积造假"——
    #   **89% 这种离谱比例,第一嫌疑永远是自己的判据错了,不是数据全坏。**
    s = re.sub(r"[+\-−]+\d*$", "", s)
    if not s or not re.fullmatch(r"(?:[A-Z][a-z]?\d*)+", s):
        return None
    out = {}
    for e, n in _FORMULA.findall(s):
        if not e:
            continue
        out[e] = out.get(e, 0) + (int(n) if n else 1)
    return out or None


def smiles_matches_formula(smi, formula):
    """返回 (是否一致, 理由)。任一侧解析不了 → (False, 原因),调用方据此清掉 SMILES。"""
    a = atoms_from_smiles(smi)
    if a is None:
        return False, "SMILES 无法解析(括号不配平/含非有机子集字符/长度异常)"
    b = atoms_from_formula(formula)
    if b is None:
        return False, "分子式缺失或格式异常,无法与 SMILES 对账"
    diffs = []
    for e in COMPARE:
        x, y = a.get(e, 0), b.get(e, 0)
        if x != y:
            diffs.append(f"{e}:SMILES {x} ≠ 分子式 {y}")
    if diffs:
        return False, "原子数对不上 —— " + "; ".join(diffs[:3])
    return True, "一致"
