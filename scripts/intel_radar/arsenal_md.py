#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把鹰眼的 GitHub 军火候选渲染成**人能直接看、直接挑**的 Markdown 报表。

立此因(创始人 2026-09-02):「marvis 每天生成爆款日报非常好,我们的鹰眼能不能也输出
md 文件?针对 GitHub 进行热度和排行,输出 md 或表格,那么我就可以知道有哪些我们可以
选择用的!」

问题不是鹰眼没数据 —— `candidates.json` 每条候选带 24 个字段(星数/日均涨星/许可/
能力描述/可吸收形式/落哪条产线),`star_history.json` 有 1893 个仓的星数轨迹。
问题是**这些全埋在 JSON 里,人打不开也不会去打开**。这个脚本就是那层"人看得见"。

产出:`reports/arsenal/军火榜_<日期>.md`
  ① 热度榜 —— 按日均涨星排(不是总星数,总星数偏袒老仓,日均涨星才看得出"现在热")
  ② 按产线分组 —— 直接告诉你"这条能用在 RAG/OCR/内容产线的哪一条"
  ③ 可吸收形式 + 许可 —— 一眼看出能不能进闭源生产(copyleft 会传染,标红)
  ④ 新发现 vs 已在军火库 —— 只看新的就够

零外部依赖,纯 stdlib;读的是鹰眼跑完就写好的 JSON,不重复抓 GitHub。
"""
import io
import json
import os
import sys
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ARSENAL = os.path.join(ROOT, "reports", "arsenal")
CAND = os.path.join(ARSENAL, "candidates.json")

# 产线中文名(enums 里是英文 key)
LINE_CN = {
    "rag": "RAG 检索线", "ocr": "古籍 OCR 线", "video": "视频产线",
    "image": "图文产线", "formula": "方剂/知识线", "modelpool": "AI 免费池",
    "unknown": "未归类",
}
FORM_CN = {
    "skill": "技能包", "mcp": "MCP 服务", "library": "代码库",
    "architecture": "架构参考", "deploy": "可部署服务", "dataset": "数据集",
    "unknown": "未定",
}
LIC_MARK = {
    "permissive": "宽松",          # MIT/Apache,可进闭源
    "copyleft": "⚠️传染",          # GPL/AGPL,碰了要开源
    "none": "无许可",              # 无 LICENSE = 默认保留全部权利,不能用
    "unknown": "未知",
}


def load():
    if not os.path.isfile(CAND):
        print("arsenal_md: 找不到 %s —— 先让鹰眼(arsenal_radar)跑一轮" % CAND)
        return None
    return json.load(io.open(CAND, encoding="utf-8"))


def row(c):
    """一条候选 → 表格行。日均涨星是热度的真判据(总星数偏袒老仓)。"""
    spd = c.get("stars_per_day_lifetime")
    spd_s = ("%.1f" % spd) if isinstance(spd, (int, float)) else "—"
    lic = LIC_MARK.get(c.get("license_family") or "unknown", "未知")
    lic_s = "%s %s" % (c.get("license") or "—", lic)
    forms = "/".join(FORM_CN.get(f, f) for f in (c.get("transfer_forms") or ["unknown"]))
    new = "🆕" if c.get("new_to_us") else ""
    return (
        "| [%s](%s) %s | %s | %s | %s | %s | %s |"
        % (c["repo"], c["url"], new, "{:,}".format(c.get("stars") or 0), spd_s,
           lic_s, forms, (c.get("capability") or c.get("description") or "")[:52])
    )


def main():
    d = load()
    if not d:
        return 1
    cands = d.get("candidates") or []
    gen = d.get("generated") or datetime.date.today().isoformat()
    if not cands:
        print("arsenal_md: candidates 为空,不生成(宁可不出,也不出一张空表)")
        return 0

    L = []
    L.append("# 🔭 GitHub 军火榜 · %s" % gen)
    L.append("")
    L.append("> 鹰眼当轮扫到的 **%d 个**可吸收候选。**按日均涨星排序**——"
             "总星数偏袒老仓,日均涨星才看得出「现在热不热」。" % len(cands))
    L.append("> 🆕 = 军火库里还没有的新发现 · ⚠️传染 = GPL/AGPL 许可,"
             "代码碰了我们的闭源生产就得开源,只能读架构不能抄代码。")
    L.append("")

    # ── ① 热度榜 ────────────────────────────────────────────────
    hot = sorted(cands, key=lambda c: (c.get("stars_per_day_lifetime") or 0), reverse=True)
    L.append("## 一、热度榜(按日均涨星)")
    L.append("")
    L.append("| 仓库 | 总星 | 日均涨星 | 许可 | 可吸收形式 | 能做什么 |")
    L.append("|---|---:|---:|---|---|---|")
    for c in hot:
        L.append(row(c))
    L.append("")

    # ── ② 按产线分组(直接告诉你能用在哪) ─────────────────────────
    by_line = {}
    for c in cands:
        for ln in (c.get("line_candidates") or ["unknown"]):
            by_line.setdefault(ln, []).append(c)
    L.append("## 二、按我们的产线分组(能用在哪)")
    L.append("")
    for ln in ["rag", "ocr", "video", "image", "formula", "modelpool", "unknown"]:
        items = by_line.get(ln)
        if not items:
            continue
        L.append("### %s(%d)" % (LINE_CN.get(ln, ln), len(items)))
        L.append("")
        for c in sorted(items, key=lambda x: -(x.get("stars") or 0)):
            spd = c.get("stars_per_day_lifetime")
            L.append("- **[%s](%s)** %s ★%s%s · %s · %s"
                     % (c["repo"], c["url"], "🆕" if c.get("new_to_us") else "",
                        "{:,}".format(c.get("stars") or 0),
                        (" · 日均+%.1f" % spd) if isinstance(spd, (int, float)) else "",
                        LIC_MARK.get(c.get("license_family") or "unknown", "未知"),
                        (c.get("capability") or "")[:70]))
        L.append("")

    # ── ③ 可直接用的(宽松许可 + 新发现),这是最该先看的一段 ──────
    pick = [c for c in cands
            if c.get("license_family") == "permissive" and c.get("new_to_us")]
    L.append("## 三、⭐ 建议优先看这几个")
    L.append("")
    if pick:
        L.append("宽松许可(可进闭源生产)+ 军火库里还没有的新发现:")
        L.append("")
        for c in sorted(pick, key=lambda x: -(x.get("stars_per_day_lifetime") or 0))[:8]:
            L.append("- **[%s](%s)** ★%s · %s · 落点:%s"
                     % (c["repo"], c["url"], "{:,}".format(c.get("stars") or 0),
                        (c.get("capability") or "")[:56],
                        "/".join(LINE_CN.get(x, x) for x in (c.get("line_candidates") or []))))
    else:
        L.append("(本轮没有「宽松许可 + 新发现」的候选 —— 如实说明,不硬凑。)")
    L.append("")
    L.append("---")
    L.append("")
    L.append("> 数据源:`reports/arsenal/candidates.json`(鹰眼 arsenal_radar 产出)。")
    L.append("> 本表只做客观呈现与排序,**不替你做采纳判断** —— 选哪个是你的事。")

    md = "\n".join(L) + "\n"
    out = os.path.join(ARSENAL, "军火榜_%s.md" % gen)
    io.open(out, "w", encoding="utf-8").write(md)
    # 同时写一份固定名的,方便直接收藏链接
    io.open(os.path.join(ARSENAL, "军火榜_最新.md"), "w", encoding="utf-8").write(md)
    print("arsenal_md: 已生成 %s(%d 候选 · %d 字符)" % (out, len(cands), len(md)))
    print("            同时写了 军火榜_最新.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
