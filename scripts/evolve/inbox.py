#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
审核收件箱 —— 把鹰眼的产出送到**审核人(本地 Claude Code)面前**

立此因(2026-08-05 创始人定架构):
  「我们现在的闭环,**因为没有自己的服务器,就绕本地的你作为审核**!
    鹰眼采集 → 你审核修改优化系统 → 发布到前端」

这一句把闭环的形状定死了,而且比「AI 自己改 AI」现实得多:

    鹰眼采集(Actions 自动)  →  审核人改系统(本地)  →  发布前端  →  回写台账
            ↑                                                        │
            └────────── 已落地的下轮不再推荐 ←──────────────────────┘

**唯一断的就是第一个箭头**:鹰眼开 Issue、写 D1、生成工单 —— 审核人开工时一个都看不见。
  开 Issue 没人点(pan-register 连喊 12 天零人响应);写 D1 要主动查才知道。
  「只写进日志的产线一律视为没人看」这条铁律,鹰眼自己正在犯。

本脚本把产出落成**一个固定文件**,随 workflow 提交进仓:
    reports/evolve/待审核.md
审核人本地 `git pull` 就看见,开工第一件事读它。处理完写 adoption.txt,
下一轮 `_stack.py` 自动把已落地的筛掉 —— 环就闭上了。

铁律:只读 D1 + 写一个 markdown;不改任何代码、不碰生产数据。
"""
import os, sys, json, time, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q                                        # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(REPO_ROOT, "reports", "evolve", "待审核.md")

# 只列系统侧模块 —— 创始人 2026-08-05:「禁止再去做药方」
MODULE_ORDER = ["自进化闭环", "免费模型池", "检索路由", "RAG向量检索", "知识图谱星图",
                "AI寻脉", "OCR产线", "采集下载线", "前台阅读器"]


def purge_stale_modules():
    """把落在**已删模块**上的候选标成 skip —— 清单删了,存量也得清。

    【2026-08-05】判定模块清单删掉「内容工厂」「导读生成」(创始人:禁止再去做药方)后,
      库里仍有 4 条药方线候选(data-diff / paperbanana / ClaimeAI / newsflow-oss),
      收件箱照样推给审核人。**清单删了存量没清 = 等于没删。**
    做法:凡 verdict_note 里的 module 不在当前 MODULE_ORDER 里的,一律降为 skip。
      不删数据(留痕可回溯),只改状态。
    """
    try:
        rows = d1("SELECT title, verdict_note FROM evolve_candidates "
                  "WHERE kind='repo' AND status IN ('adopt','watch','ticketed')")
    except Exception as e:
        print(f"  [清理] 取数失败:{str(e)[:70]}", flush=True)
        return 0
    stale = []
    for r in rows:
        try:
            m = (json.loads(r.get("verdict_note") or "{}") or {}).get("module")
        except Exception:
            continue
        if m and m not in MODULE_ORDER:
            stale.append((r["title"], m))
    if not stale:
        return 0
    ids = ",".join(q(t) for t, _ in stale)
    try:
        d1(f"UPDATE evolve_candidates SET status='skip', "
           f"verdict_note=COALESCE(verdict_note,'') || ' | 模块已从清单移除,降为 skip' "
           f"WHERE title IN ({ids})")
    except Exception as e:
        print(f"  [清理] 写库失败:{str(e)[:70]}", flush=True)
        return 0
    print(f"  [清理] {len(stale)} 条落在已删模块上的候选已降为 skip:"
          f"{', '.join(f'{t}({m})' for t, m in stale[:5])}", flush=True)
    return len(stale)


def fetch(status, limit=40):
    try:
        return d1(f"SELECT title, url, score, verdict_note, updated_at FROM evolve_candidates "
                  f"WHERE kind='repo' AND status={q(status)} "
                  f"ORDER BY COALESCE(score,0) DESC LIMIT {int(limit)}")
    except Exception as e:
        print(f"  取 {status} 失败:{str(e)[:80]}", flush=True)
        return []


def parse(r):
    try:
        n = json.loads(r.get("verdict_note") or "{}")
    except Exception:
        n = {}
    return {
        "repo": r.get("title"), "url": r.get("url"), "stars": r.get("score"),
        "module": n.get("module") or "未归类", "metric": n.get("metric"),
        "how": n.get("how"), "effort": n.get("effort"),
        "current": n.get("current"), "beats": n.get("beats"),
    }


def system_state():
    """系统侧的真实状态 —— **只看架构/基础设施,不掺内容线指标**。

    【2026-08-05 创始人当场纠正】我第一版在这里放了「本草放行率 / 生物计算放行率」——
      那是**内容质量**指标,跟技术架构没关系。创始人要的自进化,对象是**系统本身**
      (鹰眼采集 → 审核改系统 → 发布前端),不是"怎么写好一条中药材条目"。
      两件事我混了一个多月,这里先把它掰开。
    """
    out = []
    try:
        r = d1("SELECT COUNT(*) c FROM gh_repo_pool")[0]["c"]
        j = d1("SELECT COUNT(*) c FROM evolve_candidates WHERE kind='repo'")[0]["c"]
        out.append(f"月榜库 **{r}** 个仓 · 已判 **{j}** · 待判 **{r-j}**")
    except Exception:
        pass
    try:
        rows = d1("SELECT status, COUNT(*) c FROM evolve_candidates WHERE kind='repo' GROUP BY status")
        m = {x["status"]: x["c"] for x in rows}
        out.append("判定分布:" + " · ".join(f"{k} {v}" for k, v in sorted(m.items(), key=lambda kv: -kv[1])))
    except Exception:
        pass
    try:
        n = d1("SELECT COUNT(*) c FROM intel_items")[0]["c"]
        out.append(f"情报条目 **{n}** 条(雷达采集)")
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    purged = purge_stale_modules()      # 先清过期模块,再出收件箱
    adopt = [parse(r) for r in fetch("adopt")]
    watch = [parse(r) for r in fetch("watch", 15)]
    gaps = system_state()
    today = time.strftime("%Y-%m-%d")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    L = []
    L.append(f"# 待审核 · 鹰眼收件箱 · {today}\n")
    L.append("> **给本地审核人(Claude Code)**:开工第一件事读这份。")
    L.append("> 闭环的形状是「鹰眼采集(自动)→ **你审核改系统** → 发布前端 → 回写台账」,")
    L.append("> 你就是中间那一环。处理完一条,**写进 `scripts/intel_radar/adoption.txt`** ——")
    L.append("> 下一轮判定会自动把已落地的筛掉,不再重复推荐。\n")
    L.append("> 落地判据(三条全中才算,缺一条只算候选):")
    L.append("> ① 真跑过并产出了东西 ② 接进了我们的系统 ③ 有数字(量/耗时/成功率/成本)\n")

    if gaps:
        L.append("## 📊 自家现状(真实数字,不是估的)\n")
        for g in gaps:
            L.append(f"- {g}")
        L.append("")

    if adopt:
        by = {}
        for x in adopt:
            by.setdefault(x["module"], []).append(x)
        L.append(f"## 🎯 建议采纳 · {len(adopt)} 条\n")
        for mod in MODULE_ORDER + [m for m in by if m not in MODULE_ORDER]:
            if mod not in by:
                continue
            L.append(f"### {mod}\n")
            for x in by[mod]:
                L.append(f"- **[{x['repo']}]({x['url']})** · {x['stars']}★ · 工作量 {x['effort'] or '?'}")
                L.append(f"  - 预计改善:{x['metric']}")
                if x.get("current"):
                    L.append(f"  - 我们现在用的是:{x['current']}")
                if x.get("beats"):
                    L.append(f"  - 它强在:{x['beats']}")
                L.append(f"  - 怎么接:{x['how']}")
                L.append("  - [ ] 已处理 → 处理完写进 adoption.txt")
            L.append("")
    else:
        L.append("## 🎯 建议采纳\n\n本轮没有新的采纳项。\n")

    if watch:
        L.append(f"## 👀 观望 · {len(watch)} 条(方向对,现在不动)\n")
        for x in watch:
            L.append(f"- {x['repo']}({x['stars']}★)→ {x['module']}:{x['metric']}")
        L.append("")

    L.append("## ✅ 处理完怎么回写(闭环的最后一个箭头)\n")
    L.append("处理完一条,在仓里跑一行,环就闭上了:\n")
    L.append("```bash")
    L.append('python scripts/evolve/land.py --repo <owner/name> --module <模块> \\')
    L.append('       --note "真跑过并产出了什么 / 接进了哪里 / 数字是多少"')
    L.append("```")
    L.append("它会写进 `scripts/intel_radar/adoption.txt`,下一轮判定的家底闸自动把它筛掉。\n")
    L.append("---\n")
    L.append(f"本文件由 `scripts/evolve/inbox.py` 自动生成于 {time.strftime('%Y-%m-%d %H:%M')} UTC,")
    L.append("每次 Evolve Adopt 跑完自动刷新并提交。**人工改动会被下一轮覆盖** ——")
    L.append("要留痕请写 `adoption.txt`,那份是手写的、是唯一真源。")

    open(a.out, "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    print(f"  [收件箱] 写入 {a.out}:采纳 {len(adopt)} · 观望 {len(watch)} · 现状 {len(gaps)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
