#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harvest 算子 —— **缺口驱动的采集标准**:从我们自己的短板出发去抓,而不是捡热门

立此因(2026-08-04 创始人):「所以要标准去抓有用的信息!变成促进进化的依据」

这句话点的是 Adopt 环刚暴露的真问题。第一轮实测:
    GitHub 热门候选 30 个 → 采纳 3 · 观望 4 · **不相关 20(67% 是垃圾)**
  垃圾里是 freeCodeCamp(45 万星)、microsoft/TypeScript(11 万星)这类 ——
  星数极高,跟我们**一点关系没有**。
  根因不在判定环,在**采集标准**:按 GitHub 的「热门」采,采回来的自然是全世界的热门,
  不是我们的有用。判定环只能在事后把垃圾筛掉,筛掉 67% 的成本已经花掉了。

正确的标准是**倒过来**:
    ❌ GitHub 热门 → 逐个判"能不能用"        (以别人的热度为主)
    ✅ **我们的模块 + 真实缺口 → 生成检索式 → 定向去抓**   (以我为主)

缺口不是拍脑袋写的,是**系统自己量出来的**(2026-08-04 实测):
    · 内容工厂/herb    复核放行率 2.4%(n=42)
    · 内容工厂/biocomp 复核放行率 9.1%(n=22)
    · 本草前台仅 19 条 / 生物计算 296 条
    · 知识图谱 69555 节点,星图只画得出 3%
  短板越大的模块,越该多花检索预算去找解药 —— 这就是"促进进化的依据"。

产出直接进 Adopt 环判定,两条采集路线的**采纳率可以直接对比**,
  好不好用不靠我说,靠下一轮的数字。

铁律:GitHub Search 用 Actions 自带 GITHUB_TOKEN(零成本);判定走内部免费池网关。
"""
import os, sys, json, time, urllib.parse, urllib.request, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, ask, parse_json                          # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT_JSON = os.path.join(REPO_ROOT, "reports", "arsenal", "gap_candidates.json")


def measure_gaps():
    """量出**真实缺口** —— 能从库里读的一律读真值,读不到的才用写死的已知短板。

    这一步决定了后面所有检索的方向,所以宁可少列几条,也不许凭印象编缺口。
    """
    gaps = []
    try:
        for r in d1("SELECT scope,fitness,sample_n FROM evolve_variants WHERE status='active'"):
            f = r.get("fitness")
            if f is None:
                continue
            gaps.append({
                "module": "内容工厂",
                "metric": f"{r['scope']} 复核放行率",
                "now": f"{f:.1%}(n={r['sample_n']})",
                "pain": f"生成的条目 {1-f:.0%} 被复核按住,产能全卡在质量上",
                "weight": round(1 - f, 3),      # 越差权重越高 → 分到越多检索预算
            })
    except Exception as e:
        print(f"  [缺口] 读内容工厂指标失败:{str(e)[:80]}", flush=True)

    try:
        n = d1("SELECT COUNT(*) c FROM sue_graph_nodes")[0]["c"]
        gaps.append({"module": "知识图谱星图", "metric": "可视化节点覆盖率",
                     "now": f"{n} 节点,前台只画得出约 3%",
                     "pain": "大规模图无法在浏览器里读,降噪与层级布局是瓶颈",
                     "weight": 0.9})
    except Exception:
        pass

    # 读不到实测数的已知短板 —— 明写"未量化",不冒充实测值
    gaps += [
        {"module": "RAG向量检索", "metric": "召回率", "now": "未量化",
         "pain": "没有 gold set,不知道召回率多少,改了也不知道有没有变好", "weight": 0.8},
        {"module": "AI寻脉", "metric": "断言有据率", "now": "影子模式 groundedness 0.663",
         "pain": "34 条断言里 12 条无支撑(35%),幻觉仍在", "weight": 0.75},
        {"module": "自进化闭环", "metric": "换代成功率", "now": "两代 0 次成功换冠军",
         "pain": "机制转通了但还没证明能让质量真变好", "weight": 0.7},
    ]
    gaps.sort(key=lambda g: -g["weight"])
    return gaps


SYS_QUERY = (
    "你是开源技术侦察员。给你一个**具体的工程短板**,你要写出 5 条 GitHub 仓库搜索式,"
    "用来找可能解决它的开源项目。\n"
    "规则:\n"
    "  · 用**英文技术术语**(GitHub 上的项目主要用英文描述),不要用中文词;\n"
    "  · 每条聚焦一个具体技术手段,不要写宽泛的大词(如 \"AI\" \"LLM\" \"framework\");\n"
    "  · 可以带限定词如 stars:>50、language:Python;\n"
    "  · 三条要**互相不同的角度**,不要同义改写。\n"
    "只输出 JSON:{\"queries\":[\"...\",\"...\",\"...\"]}"
)


def gh_search(q, per_page=12):
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc&per_page=%d"
           % (urllib.parse.quote(q), per_page))
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gufangai-harvest"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    try:
        j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40).read())
        return j.get("items") or []
    except Exception as e:
        print(f"    检索失败({str(e)[:60]}):{q[:60]}", flush=True)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-gaps", type=int, default=99, help="本轮针对前几个缺口找(默认全部)")
    ap.add_argument("--per-query", type=int, default=30)
    ap.add_argument("--n-queries", type=int, default=5, help="每个缺口生成几条检索式")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    gaps = measure_gaps()
    print("=== 本轮缺口(按严重度排序,权重决定检索预算) ===", flush=True)
    for g in gaps:
        print(f"  [{g['weight']}] {g['module']} · {g['metric']} = {g['now']}", flush=True)

    seen, rows = set(), []
    for g in gaps[:a.top_gaps]:
        user = (f"模块:{g['module']}\n指标:{g['metric']}\n当前:{g['now']}\n痛点:{g['pain']}")
        try:
            qs = (parse_json(ask(SYS_QUERY, user, max_tokens=400)[0]) or {}).get("queries") or []
        except Exception as e:
            print(f"  [{g['module']}] 检索式生成失败:{type(e).__name__}", flush=True)
            continue
        print(f"\n  [{g['module']}] 检索式:{qs}", flush=True)
        for qq in qs[:a.n_queries]:
            for it in gh_search(str(qq), a.per_query):
                name = it.get("full_name") or ""
                if not name or name in seen:
                    continue
                seen.add(name)
                rows.append({
                    "repo": name, "url": it.get("html_url"),
                    "description": it.get("description") or "",
                    "stars": it.get("stargazers_count") or 0,
                    "topics": it.get("topics") or [],
                    "lang": it.get("language") or "",
                    # 记下它是**为哪个缺口**抓回来的 —— 判定环据此追问"到底解没解决"
                    "found_by": f"gap:{g['module']}/{g['metric']}",
                    "gap_query": str(qq),
                })
            time.sleep(2)          # GitHub search 限流,温和一点(广采靠多角度检索式,不靠加速)

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(rows, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n=== 缺口驱动抓到 {len(rows)} 个候选 → {OUT_JSON} ===", flush=True)
    for r in rows[:12]:
        print(f"  {r['repo']}({r['stars']}★) ← {r['found_by']}", flush=True)
    if a.report:
        json.dump({"gaps": gaps[:a.top_gaps], "n": len(rows),
                   "repos": [{"repo": r["repo"], "stars": r["stars"], "found_by": r["found_by"]}
                             for r in rows]},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
