#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adopt 算子 —— GitHub 热门应用 → 进化我们的技术

立此因(2026-08-04 创始人):
  「你先实现 GitHub 热门应用推送进化我们的技术,这个就已经是成功了第一步」
  「他怎么会修改代码,谁来实现,谁审核?」

断点在哪(实测):
  `arsenal_radar.py` 每天在扫 GitHub 高星仓,`reports/arsenal/candidates.json` 里
  **躺着 60 个现成候选**(freeCodeCamp 45万星、iflytek/astron-rpa 5897 星…),
  而它自己在代码里写着「**不做采不采用的判断**,判断在 SueAI 议事会」——
  **那个议事会从来没建**。于是情报采回来了,没有任何东西把它变成动作。
  同一个毛病还有第二处:`evolve_candidates` 里灌的全是 Cloudflare 博客标题和
  workers-sdk 发版说明 —— 噪声,不是能改进我们系统的东西。

本算子补的就是"议事会":把每个候选**判到我们的具体模块上**。
  判据不是"这个仓好不好"(高星仓当然好),而是:
    ① 它能改进**我们哪个模块**(必须从真实模块清单里选,选不出就判"不相关");
    ② 预计改善**哪个指标**(说不出指标 = 没想清楚 = 不采纳);
    ③ 工作量多大;④ 采纳 / 观望 / 不要。
  说不清"改哪个模块、动哪个指标"的一律判 skip —— **这条判据本身就是过滤器**,
  防止又产出一份"很有启发"但没人能动手的情报日报。

谁改代码、谁审核(创始人直接问的,写死在这里):
  · 配置级(阈值/路由表/开关)→ 系统自己改,确定性判据兜底,全自动
  · 提示词级                  → 系统自己改,A/B 真成绩裁决,全自动(improve.py)
  · **真代码级**              → 系统**只有写权、没有合权**:
      在 Actions 里写 patch → 开 PR → CI(74 个测试 + 零 LIST 守卫 + build)
      → 红线审查 → **人 merge**。绝不允许自动 push main。
  本算子产出的是**提案**,不是改动;采纳入口是它开的 Issue,由创始人/CTO 点。

铁律:只走内部免费池网关;不碰生产数据;不自动改任何代码。
"""
import os, sys, json, time, hashlib, argparse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q, ask, parse_json                      # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CAND_JSON = os.path.join(REPO_ROOT, "reports", "arsenal", "candidates.json")

# 我们系统的**真实模块清单**。判定必须落到这里面的某一个 ——
#   这是整个算子最关键的设计:不给它自由发挥的余地,它就编不出"很有启发"的空话。
MODULES = [
    ("检索路由",     "/api/retrieve 统一检索入口,按意图分派到 wiki/graph/volume/FTS"),
    ("RAG向量检索",  "tcm-rag-768 索引 183 万向量,古籍与医案的语义召回"),
    ("知识图谱星图", "sue_graph_nodes/edges 6.9 万节点,2D/3D 力导向可视化"),
    ("AI寻脉",       "古籍辨证参阅报告,RAG grounding + 多段结构化输出"),
    ("导读生成",     "古籍逐卷导读,PageIndex 式层级摘要"),
    ("内容工厂",     "本草对照 / 生物计算条目的自动生成与复核"),
    ("OCR产线",      "古籍扫描件转文字,云端 Actions 批量跑"),
    ("采集下载线",   "海外馆藏抓取、对账、归档"),
    ("自进化闭环",   "方案槽 / A-B 分流 / 适应度裁决 / 改进算子"),
    ("前台阅读器",   "沉浸式阅读、分页、字号、会员门禁"),
    ("免费模型池",   "网关多供应商容错链、探活、配额调度"),
]
MODULE_NAMES = [m[0] for m in MODULES]

SYS_ADOPT = (
    "你是「古方 AI 星图」平台的**技术选型评审员**。给你一个 GitHub 开源项目,"
    "判断它**能不能用来改进我们某个具体模块**。\n"
    "我们的模块清单(只能从这里选,不许自创):\n"
    + "\n".join(f"  · {n} —— {d}" for n, d in MODULES) + "\n\n"
    "判定规则(从严):\n"
    "  · 说不出它改进**哪一个**模块 → verdict=\"skip\";\n"
    "  · 说不出预计改善**哪个可量化指标** → verdict=\"skip\";\n"
    "  · 只是「很有启发」「值得关注」而没有具体接法 → verdict=\"skip\";\n"
    "  · 通用基础设施(语言、框架、教程仓、awesome 列表)一律 skip;\n"
    "  · **绝不因为星多就采纳** —— 星数不是判据,能不能接进我们的模块才是。\n"
    "verdict 三档:adopt(该接,给出接法) / watch(方向对但现在不动) / skip(不相关)。\n"
    "只输出 JSON:{\"module\":\"清单里的模块名或空\",\"verdict\":\"adopt|watch|skip\","
    "\"metric\":\"预计改善的指标,如「检索召回率」「导读生成耗时」\",\"how\":\"40字以内具体接法\","
    "\"effort\":\"小|中|大\",\"why\":\"30字以内理由\"}"
)


GAP_JSON = os.path.join(REPO_ROOT, "reports", "arsenal", "gap_candidates.json")

# ── 确定性预筛词表 ──────────────────────────────────────────────
# 创始人 2026-08-04:「要多采集,然后筛选自己需要的」。
#   广采的前提是**筛得起**:每个仓一次模型调用,几百个候选就是几百次调用。
#   所以先用零成本的确定性预筛砍掉明显无关的,模型只判剩下的。
#   实测第一轮 30 个里 20 个是 freeCodeCamp / TypeScript 这类 —— 这些根本不该花模型调用。
NOISE = ("awesome-", "tutorial", "curriculum", "interview", "roadmap", "cheatsheet",
         "bootcamp", "learn-", "-course", "study-", "examples", "boilerplate",
         "starter", "template", "demo", "playground", "hello-world", "free-programming")
# 命中任一即进入模型精判 —— 覆盖我们 11 个模块真正吃的技术面
SIGNAL = ("rag", "retriev", "embed", "vector", "rerank", "hybrid search", "bm25",
          "knowledge graph", "graphrag", "graph visual", "force-directed", "force graph",
          "edge bundl", "louvain", "community detect", "layout",
          "ocr", "document parsing", "pdf extract", "layout analysis", "handwrit",
          "hallucinat", "groundedness", "citation", "attribution", "fact check", "self-rag",
          "eval", "benchmark", "gold set", "llm-as-judge", "prompt optim", "dspy",
          "agent", "workflow orchestr", "self-improv", "evolutionary", "genetic",
          "tcm", "traditional chinese medicine", "herb", "chinese medic", "classical chinese",
          "ancient text", "cjk", "chemistry", "molecul", "smiles", "pubchem", "bioinformatic",
          "llm gateway", "proxy", "load balanc", "free api", "model router", "fallback",
          "crawler", "scraper", "iiif", "digital librar", "archive")


def _prefilter(r):
    """零成本预筛。返回 (是否进模型精判, 原因)。"""
    name = str(r.get("repo") or r.get("full_name") or "").lower()
    blob = " ".join([name, str(r.get("description") or ""),
                     " ".join(r.get("topics") or [])]).lower()
    if any(k in name for k in NOISE):
        return False, "教程/模板/awesome 类"
    # 缺口驱动的必须**先于**信号词检查放行 —— 它是拿我们自己的短板去定向搜回来的,
    #   相关性由检索式保证,不该再要求它的英文描述里出现我们的词表。
    #   (自测抓到:这句原来写在信号词检查之后,等于把最该留的一路当噪声砍掉。)
    if str(r.get("found_by") or "").startswith("gap:"):
        return True, "缺口定向"
    hits = [k for k in SIGNAL if k in blob]
    if not hits:
        return False, "描述里没有任何我们吃得上的技术面"
    return True, "命中:" + "/".join(hits[:3])


def load_repo_candidates(limit=400):
    """两条采集路线合流:缺口驱动(优先)+ GitHub 热门(兜底)。

    合流之后能直接对比两条路的采纳率 —— 哪条标准更好不靠嘴说,靠下一轮数字。
    """
    rows = []
    for p, tag in ((GAP_JSON, "缺口驱动"), (CAND_JSON, "热门榜")):
        if not os.path.exists(p):
            print(f"  [采集] 缺 {os.path.basename(p)}({tag}),跳过", flush=True)
            continue
        d = json.load(open(p, encoding="utf-8"))
        rs = d if isinstance(d, list) else (d.get("rows") or d.get("candidates") or [])
        print(f"  [采集] {tag}:{len(rs)} 个", flush=True)
        rows += rs
    seen, out = set(), []
    for r in rows:
        n = str(r.get("repo") or r.get("full_name") or "")
        if n and n not in seen:
            seen.add(n)
            out.append(r)
    return out[:limit]


def already_judged():
    try:
        return {r["title"] for r in d1("SELECT title FROM evolve_candidates "
                                       "WHERE kind='repo' AND status!='new'")}
    except Exception:
        return set()


def judge(r):
    name = str(r.get("repo") or r.get("full_name") or "").strip()
    desc = str(r.get("description") or "")[:400]
    topics = ",".join(r.get("topics") or [])[:200]
    user = (f"项目:{name}\n星数:{r.get('stars')}\n语言:{r.get('lang')}\n"
            f"topics:{topics}\n简介:{desc}")
    txt, model = ask(SYS_ADOPT, user, max_tokens=500)
    try:
        o = parse_json(txt)
    except Exception:
        # 首轮实测 30 个里 3 个解析失败(10%)—— 模型没吐 JSON。重试一次再放弃。
        txt, model = ask(SYS_ADOPT, user + "\n\n【必须只输出 JSON,不要任何别的字】",
                         max_tokens=500)
        try:
            o = parse_json(txt)
        except Exception as e:
            return None, f"解析失败 {type(e).__name__}(重试后仍失败)"
    v = str(o.get("verdict") or "skip").lower()
    if v not in ("adopt", "watch", "skip"):
        v = "skip"
    mod = str(o.get("module") or "").strip()
    # 硬闸:模块必须在清单里,指标必须写了 —— 否则一律降到 skip。
    #   这条不问模型,确定性判据;不然它会自创模块名把话说圆。
    if v in ("adopt", "watch") and (mod not in MODULE_NAMES or not str(o.get("metric") or "").strip()):
        v = "skip"
        o["why"] = f"(自动降级)模块「{mod}」不在清单或未给出指标 · 原判 {o.get('verdict')}"
    o["verdict"], o["module"], o["_model"] = v, mod, model
    return o, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="本轮评审几个候选")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    rows = load_repo_candidates()
    done = already_judged()
    fresh = [r for r in rows if str(r.get("repo") or r.get("full_name") or "") not in done]

    # 两级筛:① 零成本确定性预筛 → ② 模型精判(只花在过了预筛的身上)
    kept, dropped = [], 0
    for r in fresh:
        ok, why = _prefilter(r)
        if ok:
            r["_pre"] = why
            kept.append(r)
        else:
            dropped += 1
    todo = kept[:a.limit]
    print(f"=== Adopt 算子 · 采到 {len(rows)} · 已判 {len(done)} · "
          f"预筛砍掉 {dropped}(零成本)· 本轮精判 {len(todo)} ===", flush=True)

    out, now = {"adopt": [], "watch": [], "skip": 0, "fail": 0,
                "prefilter_dropped": dropped, "harvested": len(rows)}, int(time.time())
    for r in todo:
        name = str(r.get("repo") or "")
        o, err = judge(r)
        if err:
            out["fail"] += 1
            print(f"  ✗ {name}: {err}", flush=True)
            continue
        v = o["verdict"]
        line = {"repo": name, "stars": r.get("stars"), "url": r.get("url"),
                "module": o.get("module"), "metric": o.get("metric"),
                "how": o.get("how"), "effort": o.get("effort"), "why": o.get("why")}
        if v == "skip":
            out["skip"] += 1
        else:
            out[v].append(line)
            print(f"  {'🎯' if v=='adopt' else '👀'} {name}({r.get('stars')}★)"
                  f" → {o.get('module')} · {o.get('metric')} · {o.get('effort')}", flush=True)
        if a.dry_run:
            continue
        cid = "c_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
        note = json.dumps({k: o.get(k) for k in ("module", "metric", "how", "effort", "why")},
                          ensure_ascii=False)[:900]
        try:
            d1("INSERT OR REPLACE INTO evolve_candidates "
               "(cand_id,kind,title,url,radar_run,score,status,verdict_note,created_at,updated_at) "
               f"VALUES ({q(cid)},'repo',{q(name)},{q(str(r.get('url') or '')[:300])},"
               f"{q(str(now))},{q(r.get('stars') or 0)},{q(v)},{q(note)},{now},{now})")
        except Exception as e:
            print(f"    写库失败:{str(e)[:90]}", flush=True)

    print(f"\n=== 本轮:采纳 {len(out['adopt'])} · 观望 {len(out['watch'])} · "
          f"不相关 {out['skip']} · 失败 {out['fail']} ===", flush=True)
    if a.report:
        json.dump(out, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
