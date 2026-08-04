#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资格闸 + 字段级连续分 —— 判分函数(**本地零调用、零随机、可复现**)

立此因(2026-08-04 Claude 大哥评审,两条纠正我的):

【纠正一 · 职能】C 不当排名分,当**资格闸**(过/不过)。
  我 v1 想的是"70% 确定性 + 30% 模型分" —— 那等于把刚赶出去的噪声从窗户请回来,
  而且 30% 这个配比没有任何依据。
  **入场券没法"卷"**:形式指标当闸用时不产生形式主义的进化压力,只会取消资格。

【纠正二 · 能力】我 v1 写「确定性只能量形式、量不了编造」—— **这句是错的**。
  量不量得了内容,取决于**有没有可核对的参照物**,不取决于代码懂不懂语义。
  `_smiles.py` 抓出 18 条造假结构式就是铁证:**确定性对账能量内容真假**。
  三类参照物:跨字段自洽 / 引文回验 / 金标命中。

【第二个病 · 刻度触底】(我完全漏了,大哥补的)
  四个候选里三个并列 0.0% —— **就算噪声全消掉,绝对分也永远排不出序**。
  二值判据 + 高门槛 = 把全体候选压在地板上,**进化引擎结构性无法晋升**。
  这两个多月挑战者一直 0% 上不来,一部分是这个原因,不全是提示词不行。
  所以本模块**必须给部分分**:逐项扣分明细 + 字段级连续分,**禁止整题过/不过的二值**。

判分函数版本 = 冻结条件之一。改这个文件 = **换赛季**,跨赛季分数作废。
"""
import os, re, sys, json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _smiles import smiles_matches_formula                    # noqa: E402

# 改这个常数 = 换赛季。注册进「指标受控注册表」。
# 改判分逻辑必须 bump 这个版本 —— 它进 evaluation_dataset 复合键,
#   一改历史分数自动不可比(三冻结:冻题/冻考官/冻判分函数)。
#   v2:修了剂量正则的  漏洞(中文单位后面  不成立,红线静默失效过)。
SCORER_VERSION = "sc-v2-2026-08-04"

# 反编造项权重**压过**格式项 —— 18 条造假结构式的信号:
#   主要失分在编造,不在格式缺失。闸里量的是真假,卷不出来。
W_FABRICATION = 3.0
W_COMPLIANCE = 3.0
W_FORMAT = 1.0


def _s(v):
    return str(v or "").strip()


def _arr(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except Exception:
            return []
    return []


# ── 资格闸:硬约束,不过就取消资格(不参与排名) ────────────────────────
def qualify(target, obj):
    """返回 (合格?, [失格理由])。**只判过/不过,不给分。**"""
    bad = []
    if not isinstance(obj, dict) or not obj:
        return False, ["产出不是对象"]

    name = _s(obj.get("name_cn")) or _s(obj.get("name_en"))
    if not name:
        bad.append("无名称")

    # 版块定义级硬约束 —— 这不是苛求,是"这条内容属不属于这个版块"
    if target == "biocomp" and not _s(obj.get("formula")):
        bad.append("无分子式:不是分子层面的活性成分")
    if target == "herb" and not _s(obj.get("name_latin")):
        bad.append("无拉丁学名:阿育吠陀药材必须有学名")

    # 合规红线 —— 命中即失格,零容忍,不打折不给部分分
    blob = json.dumps(obj, ensure_ascii=False)
    # 【2026-08-04 离线测试当场抓出的真漏洞】原来剂量正则写成
    #     r"[0-9]+\s*(?:克|g|钱|两|毫升|ml)\b"
    #   —— 「每日 9 克水煎服」**竟然放行**。根因:`\b` 是「词字符/非词字符」边界,
    #   而中文「克」和后面的「水」在 Python 正则里**都是词字符**,边界压根不存在。
    #   中文单位不能用 \b 收尾;只有拉丁单位(g/ml)才需要它防止误吃 "great"/"mlxx"。
    #   合规红线上的正则写错 = 红线静默失效,这是最危险的一类 bug —— 它不报错,只是不拦。
    for pat, why in ((r"[0-9]+\s*(?:克|钱|两|毫升|升|斤)", "出现剂量(中文单位)"),
                     (r"[0-9]+\s*(?:g|mg|ml|L)\b", "出现剂量(拉丁单位)"),
                     (r"水煎服|煎汤服|分[二三两]次服", "出现用法"),
                     (r"治愈率|根治|特效|包治|药到病除", "疗效承诺"),
                     (r"(?:你|您)(?:应该|需要|可以)服用", "医嘱口吻")):
        if re.search(pat, blob):
            bad.append(f"合规红线:{why}")
    return (len(bad) == 0), bad


# ── 字段级连续分:过闸之后才算,给部分分 ──────────────────────────────
def _score_fabrication(target, obj):
    """反编造:**跨字段自洽**类内容闸。有参照物就能零随机地判真假。"""
    items = []          # (得分0~1, 权重, 说明)

    if target == "biocomp":
        smi, formula = _s(obj.get("smiles")), _s(obj.get("formula"))
        if smi and formula:
            ok, why = smiles_matches_formula(smi, formula)
            items.append((1.0 if ok else 0.0, 1.0,
                          "结构式↔分子式自洽" if ok else f"结构式与分子式不符:{str(why)[:40]}"))
        elif formula and not smi:
            items.append((0.7, 0.5, "留空结构式(诚实,但信息量少)"))

        cid = _s(obj.get("pubchem_cid"))
        if cid:
            items.append((1.0 if cid.isdigit() else 0.0, 0.6,
                          "PubChem CID 形态合法" if cid.isdigit() else f"CID 不是数字:{cid[:20]}"))

        mw = obj.get("mol_weight")
        if mw not in (None, "", 0):
            try:
                v = float(mw)
                okmw = 50 <= v <= 2000
                items.append((1.0 if okmw else 0.0, 0.4,
                              "分子量在合理区间" if okmw else f"分子量离谱:{v}"))
            except Exception:
                items.append((0.0, 0.4, f"分子量不是数字:{str(mw)[:20]}"))

    if target == "herb":
        latin = _s(obj.get("name_latin"))
        if latin:
            # 双名法:属名首字母大写 + 种加词小写。形态可确定性判,不需要懂植物学。
            okl = bool(re.match(r"^[A-Z][a-z]+ [a-z][a-z\-]+", latin))
            items.append((1.0 if okl else 0.3, 1.0,
                          "学名符合双名法" if okl else f"学名形态可疑:{latin[:30]}"))
        refs = _s(obj.get("tcm_refs"))
        if refs:
            # 引文回验的**弱形态版**:至少要指得出书名。0A 锚点上线后升级为区间+哈希回验。
            okr = bool(re.search(r"《[^》]{2,20}》", refs))
            items.append((1.0 if okr else 0.2, 0.8,
                          "出处指到具体典籍" if okr else "出处没有具体书名,疑似泛指"))

    return items


def _score_format(target, obj):
    """格式/完整度 —— 权重最低。它是卫生条件,不是价值。"""
    items = []
    fields = (("name_cn", 0.5), ("name_en", 0.3)) + (
        (("mechanism", 1.0), ("tcm_link", 0.8), ("targets", 0.6)) if target == "biocomp"
        else (("traditional_use", 1.0), ("tcm_compare", 0.8), ("indications", 0.6)))
    for f, w in fields:
        v = obj.get(f)
        n = len(_s(v)) if not isinstance(v, list) else sum(len(_s(x)) for x in v)
        # 连续分:0 字 0 分,>=120 字满分,中间线性 —— 这就是"部分分",不是有/无二值
        items.append((min(n / 120.0, 1.0), w, f"{f} 长度 {n}"))
    return items


def score(target, obj, gold=None):
    """判分主入口。**同一份输入永远得同一个分**(零随机、零外部调用)。

    返回 dict:
      qualified  是否过闸
      reasons    失格理由(未过闸时)
      score      0~1 连续分(过闸才算)
      breakdown  逐项扣分明细 —— 大哥要求的"部分分",也是给改进者的燃料
    """
    ok, reasons = qualify(target, obj)
    if not ok:
        return {"qualified": False, "reasons": reasons, "score": 0.0,
                "breakdown": [], "scorer": SCORER_VERSION}

    groups = [(_score_fabrication(target, obj), W_FABRICATION, "反编造"),
              (_score_format(target, obj), W_FORMAT, "格式完整")]
    if gold:
        groups.append((_score_gold(obj, gold), W_COMPLIANCE, "金标命中"))

    total_w = 0.0
    total_s = 0.0
    breakdown = []
    for items, gw, gname in groups:
        for s_, w_, why in items:
            ww = w_ * gw
            total_w += ww
            total_s += s_ * ww
            breakdown.append({"group": gname, "score": round(s_, 3),
                              "weight": round(ww, 2), "why": why})
    final = (total_s / total_w) if total_w else 0.0
    return {"qualified": True, "reasons": [], "score": round(final, 4),
            "breakdown": breakdown, "scorer": SCORER_VERSION}


def _score_gold(obj, gold):
    """金标命中:回归集给的标准要点,命中率代码可算。

    gold = {"must_mention": ["..."], "must_not": ["..."]}
    需要领域专家出题 —— 这是全套判据里**唯一必须外部投入**的一环,没出题就自动跳过。
    """
    blob = json.dumps(obj, ensure_ascii=False)
    items = []
    must = gold.get("must_mention") or []
    if must:
        hit = sum(1 for k in must if k in blob)
        items.append((hit / len(must), 1.0, f"金标要点命中 {hit}/{len(must)}"))
    never = gold.get("must_not") or []
    if never:
        bad = sum(1 for k in never if k in blob)
        items.append((0.0 if bad else 1.0, 1.0,
                      f"踩到禁述 {bad} 处" if bad else "无禁述"))
    return items


if __name__ == "__main__":
    # 自检:证明判分是可复现的、且能给出部分分(不是二值)
    demo = {"name_cn": "黄芩苷", "name_en": "Baicalin", "formula": "C21H18O11",
            "smiles": "OC1C(O)C(OC(C1O)C(O)=O)Oc1cc(O)c2c(c1)oc(cc2=O)-c1ccccc1",
            "pubchem_cid": "64982", "mol_weight": 446.4,
            "mechanism": "体外研究提示其对多种炎症通路有调节作用" * 3,
            "tcm_link": "黄芩为常用清热药" * 5, "targets": ["NF-kB"]}
    a = score("biocomp", demo)
    b = score("biocomp", demo)
    print(f"过闸={a['qualified']}  得分={a['score']}  可复现={'是' if a==b else '否'}")
    for x in a["breakdown"]:
        print(f"  [{x['group']}] {x['score']:.2f} ×{x['weight']:.1f}  {x['why']}")
    thin = {"name_cn": "某成分", "formula": "C10H10", "mechanism": "有作用"}
    c = score("biocomp", thin)
    print(f"\n瘦条目 过闸={c['qualified']} 得分={c['score']}  ← 不是 0,给了部分分")
