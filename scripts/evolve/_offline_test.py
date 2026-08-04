#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线单元测试 —— **真调用函数体**,不只 import。

立此因(2026-08-04 第二次白跑一轮):`gate_score` 没导入,
  但它在 `run_one` 的函数体里,冒烟 import 执行不到那一行 → 照样绿灯 → 云端才炸。
  **import 通过 ≠ 函数能跑**。凡是判据类代码,必须有一条真调用的用例。
零网络、零 D1,几十毫秒。
"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
sys.path.insert(0, HERE)

ok = fail = 0
def t(name, fn):
    global ok, fail
    try:
        fn(); print(f"  ✓ {name}"); ok += 1
    except Exception as e:
        print(f"  ✗ {name}: {type(e).__name__}: {e}"); fail += 1

from gate import score, qualify, SCORER_VERSION

GOOD_BIO = {"name_cn": "黄芩苷", "name_en": "Baicalin", "formula": "C21H18O11",
            "pubchem_cid": "64982", "mol_weight": 446.4,
            "mechanism": "体外研究提示对多条炎症通路有调节作用。" * 4,
            "tcm_link": "黄芩为清热燥湿之品。" * 6, "targets": ["NF-kB", "COX-2"]}
GOOD_HERB = {"name_cn": "沙塔瓦里", "name_en": "Shatavari",
             "name_latin": "Asparagus racemosus",
             "traditional_use": "阿育吠陀用于滋养。" * 8,
             "tcm_compare": "与中医天冬功用有相通处。" * 6,
             "indications": "文献记载用于体虚。" * 4,
             "tcm_refs": "《本草纲目》相关记载"}

t("run_one 的判分函数真能被调到(治上次白跑)", lambda: (
    __import__("evaluate"),
    None))
t("biocomp 好条目能过闸且分数 > 0.3", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError(f"过闸={r['qualified']} 分={r['score']}"))
     if not (r["qualified"] and r["score"] > 0.3) else None)(score("biocomp", GOOD_BIO))))
t("herb 好条目能过闸", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError(str(r["reasons"])))
     if not r["qualified"] else None)(score("herb", GOOD_HERB))))
t("无分子式 → biocomp 失格", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError("竟然过闸"))
     if r["qualified"] else None)(score("biocomp", {"name_cn": "X"}))))
t("无拉丁学名 → herb 失格", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError("竟然过闸"))
     if r["qualified"] else None)(score("herb", {"name_cn": "X"}))))
t("剂量 → 失格(合规红线)", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError("剂量竟然放行"))
     if r["qualified"] else None)(score("biocomp", dict(GOOD_BIO, mechanism="每日 9 克水煎服")))))
t("**给部分分而非二值**:瘦条目 0 < 分 < 好条目", lambda: (
    (lambda thin, good: (_ for _ in ()).throw(
        AssertionError(f"瘦={thin} 好={good}"))
     if not (0 < thin < good) else None)(
        score("biocomp", {"name_cn": "某物", "formula": "C10H10", "mechanism": "有作用"})["score"],
        score("biocomp", GOOD_BIO)["score"])))
t("**可复现**:同一输入跑 5 次同一个分", lambda: (
    (lambda xs: (_ for _ in ()).throw(AssertionError(str(xs)))
     if len(set(xs)) != 1 else None)(
        tuple(score("biocomp", GOOD_BIO)["score"] for _ in range(5)))))
t("造假结构式被抓(反编造项真起作用)", lambda: (
    (lambda r: (_ for _ in ()).throw(AssertionError("造假没被扣分"))
     if not any("不符" in b["why"] for b in r["breakdown"]) else None)(
        score("biocomp", dict(GOOD_BIO, smiles="CCO")))))

print(f"\n离线测试:{ok} 过 / {fail} 败  (判分函数版本 {SCORER_VERSION})")
sys.exit(1 if fail else 0)
