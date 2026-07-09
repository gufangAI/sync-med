# -*- coding: utf-8 -*-
"""
情报鹰眼 · 周报猎手 v1 (MVP)
定位: 领先空地猎手 + 跨界移植引擎(照《情报鹰眼_重构说明_v1》§9 MVP 落地)
  三赛道: AI技术(arXiv) + 其他垂直行业AI(HN Algolia) + 政策/中文行业(Bing News RSS)
  打分:   领先度 × 可移植度(LLM, 免费池网关)
  产出:   每周 3 个领先空地 + 1 个动作建议 + 该忽略热点 → gh issue 推手机 + md 归档
铁律: 领先≠太早(每条标 现在做/埋着等/只是记录);诚实标注来源/时间;拿不准标"待验证"。
全程免费池网关 ai.gufangai.com,零付费 API。新建不覆盖 daily_report_v3(它照跑,验证周报价值后再谈裁撤)。
"""
import json, re, sys, time, datetime, subprocess, urllib.request, urllib.parse, xml.etree.ElementTree as ET

GW = "https://ai.gufangai.com/v1/chat/completions"
GW_KEY = "gufang"
TODAY = datetime.date.today().isoformat()

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "EagleEyeWeekly/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def llm(system, user, max_tokens=1600, timeout=120):
    # 2026-07-09 平台CTO: CF Workers AI 按Neuron计费·创始人铁令禁用;只走免费家 nvidia,失败重试同家。
    body = {"model": "nvidia", "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "max_tokens": max_tokens}
    for model in ("nvidia", "nvidia", "nvidia"):
        body["model"] = model
        try:
            req = urllib.request.Request(GW, data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {GW_KEY}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8", errors="replace"))
            return j["choices"][0]["message"]["content"]
        except Exception as e:
            last = str(e)[:120]; time.sleep(3)
    raise RuntimeError(f"LLM all failed: {last}")

def pick_json(s):
    m = re.search(r"\[.*\]|\{.*\}", s, re.S)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None

# ── 赛道① AI技术: arXiv 近7天 ──────────────────────────────────────────────
def lane_arxiv():
    out = []
    try:
        url = ("http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL"
               "&sortBy=submittedDate&sortOrder=descending&max_results=50")
        root = ET.fromstring(fetch(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            t = (e.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
            s = (e.findtext("a:summary", "", ns) or "").strip().replace("\n", " ")[:300]
            u = e.findtext("a:id", "", ns) or ""
            if t: out.append({"lane": "AI技术", "title": t, "brief": s, "url": u})
    except Exception as ex:
        print(f"[arxiv] 失败(如实): {str(ex)[:100]}", flush=True)
    print(f"[arxiv] {len(out)} 条", flush=True)
    return out

# ── 赛道② 别行业打法(跨界移植主矿脉·权重最高): 精选产品/增长/资本/垂直AI 深度源 RSS ──
# 教训: HN标题=创业新闻,不是"打法"。真正能搬的打法(出处溯源/留存/定价/护城河/垂直AI怎么做)
#       藏在这些深度源的文章里。用它们当主矿脉,才对得上"看中医玩家结构上不看的地方"。
PLAYBOOK_FEEDS = [
    ("a16z",       "https://a16z.com/feed/"),
    ("Lenny",      "https://www.lennysnewsletter.com/feed"),
    ("FirstRound", "https://review.firstround.com/feed"),
    ("Sequoia",    "https://www.sequoiacap.com/feed/"),
    ("Stratechery","https://stratechery.com/feed/"),
]
def lane_playbook():
    out, seen = [], set()
    for name, url in PLAYBOOK_FEEDS:
        try:
            root = ET.fromstring(fetch(url, timeout=25))
            for it in list(root.iter("item"))[:8]:   # 深度源发文少,取最近8篇,不卡7天窗口
                t = (it.findtext("title") or "").strip()
                if not t or t in seen: continue
                seen.add(t)
                desc = re.sub(r"<[^>]+>", " ", (it.findtext("description") or "")).strip()[:220]
                out.append({"lane": "别行业打法", "title": t,
                            "brief": f"[{name}] {desc}", "url": it.findtext("link") or ""})
        except Exception as ex:
            print(f"[playbook:{name}] 失败: {str(ex)[:80]}", flush=True)
        time.sleep(0.4)
    print(f"[playbook] {len(out)} 条", flush=True)
    return out

# ── 赛道③ 政策/中文行业: Bing News RSS ─────────────────────────────────────
def lane_policy():
    out, seen = [], set()
    for q in ["中医药 政策", "医疗数据 要素 政策", "人用经验 中药", "中医 人工智能"]:
        try:
            url = "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=rss"
            root = ET.fromstring(fetch(url))
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                if not t or t in seen: continue
                seen.add(t)
                out.append({"lane": "政策/中文", "title": t,
                            "brief": (it.findtext("description") or "")[:200],
                            "url": it.findtext("link") or ""})
        except Exception as ex:
            print(f"[policy:{q}] 失败: {str(ex)[:80]}", flush=True)
        time.sleep(0.6)
    print(f"[policy] {len(out)} 条", flush=True)
    return out

# ── 打分: 领先度×可移植度(LLM 批量) ────────────────────────────────────────
SCORE_SYS = """你是「古方AI星图」的情报打分器。背景:我们做中医古籍AI(学派思维分身/判断引擎/古籍出处溯源/育人/订阅变现)。
对每条情报打两个分(0-5):
- leading 领先度: 这事是否"别人还没看见/还没大规模做"(越早期越冷门越高;刷屏热点=低)
- transplant 可移植度: ★最高分给"别的垂直行业/别的行业的【产品打法·商业模式·信任机制】"——法律AI怎么做出处溯源、增长团队怎么做用户留存、资本怎么给按结果付费定价、别的专家型AI怎么复现判断,这些中医玩家结构上不看、却能直接搬进古方。'又一篇AI能力论文/新模型/新算法'除非直接解锁一个新产品能力,否则 transplant 压到≤2(那是人人都在看的红海)。纯硬件/无关=0。
只输出JSON数组:[{"i":序号,"leading":n,"transplant":n,"move":"一句移植点(没有则空)"}]"""

def score_items(items):
    scored = []
    B = 14
    for s in range(0, len(items), B):
        batch = items[s:s+B]
        lines = [f"{s+k}. [{it['lane']}] {it['title']} — {it['brief'][:120]}" for k, it in enumerate(batch)]
        try:
            arr = pick_json(llm(SCORE_SYS, "\n".join(lines), max_tokens=1400)) or []
            for a in arr:
                i = a.get("i")
                if isinstance(i, int) and s <= i < s + len(batch) and i < len(items):
                    items[i]["leading"] = int(a.get("leading", 0))
                    items[i]["transplant"] = int(a.get("transplant", 0))
                    items[i]["move"] = str(a.get("move", ""))[:120]
                    scored.append(items[i])
        except Exception as ex:
            print(f"[score] 批{s}失败: {str(ex)[:80]}", flush=True)
        print(f"[score] {min(s+B,len(items))}/{len(items)}", flush=True)
    return scored

# ── 深挖: 跨界移植分析(§3 那句话) ───────────────────────────────────────────
DEEP_SYS = """你是「古方AI星图」的跨界移植分析官。我们:中医古籍AI,资产=学派思维分身+判断引擎+古籍出处+海外孤本语料,变现=订阅/育人。
对给你的情报,回答重构说明的核心句:"X行业用Z方法解决了Y问题,中医行业玩家不会看到——能不能搬进古方?"
输出JSON:{"信号":"一句话说清是什么","透镜":"技术拐点/政策拐点/竞争盲区","为何是空地":"别人为什么还没看见/没做",
"跨界移植点":"在哪个行业已跑通→怎么搬进古方(具体)","建议动作":"下一步具体做什么(一句)",
"档位":"现在做/埋着等/只是记录","可忽略":"若该忽略写理由,否则空","置信":"高/中/待验证"}
铁律:领先≠太早,档位要诚实;拿不准标待验证;不编造。只输出JSON。"""

def deep_analyze(it):
    u = f"[{it['lane']}] {it['title']}\n{it['brief']}\n来源:{it.get('url','')}"
    j = pick_json(llm(DEEP_SYS, u, max_tokens=900))
    return j if isinstance(j, dict) else None

def main():
    print(f"=== 鹰眼周报猎手 v1 · {TODAY} ===", flush=True)
    items = lane_arxiv() + lane_playbook() + lane_policy()
    if len(items) < 10:
        print(f"[warn] 源太少({len(items)}),如实继续", flush=True)
    scored = score_items(items)
    scored.sort(key=lambda x: (x.get("leading", 0) * x.get("transplant", 0), x.get("transplant", 0)), reverse=True)
    # 赛道配额(把重构说明§4铁律"赛道5垂直行业AI权重最高"从口号变硬约束):
    # 优先垂直行业AI+政策(中医玩家结构上不看的地方),arXiv封顶1条,防周报退化成"又一堆AI论文"。
    by_lane = {}
    for x in scored:
        if x.get("transplant", 0) >= 2:
            by_lane.setdefault(x["lane"], []).append(x)
    cands = (by_lane.get("别行业打法", [])[:6] + by_lane.get("政策/中文", [])[:4]
             + by_lane.get("AI技术", [])[:3])
    print(f"[cand] 别行业打法={len(by_lane.get('别行业打法',[]))} 政策={len(by_lane.get('政策/中文',[]))} "
          f"AI技术={len(by_lane.get('AI技术',[]))}", flush=True)

    def toksig(t):   # 主题去重指纹:2字中文词 + 4+字英文词
        return set(re.findall(r"[一-鿿]{2}|[a-z]{4,}", t.lower()))

    signals, ai_used, picked = [], 0, []
    for it in cands:
        if len(signals) >= 3: break
        if it["lane"] == "AI技术" and ai_used >= 1:   # arXiv封顶1条,位置留给跨行业矿脉
            continue
        sg = toksig(it["title"])
        if any(len(sg & p) / max(len(sg | p), 1) > 0.45 for p in picked):   # 近重复(如同一场会3篇)跳过
            print(f"[dedup] 跳过近重复: {it['title'][:40]}", flush=True); continue
        try:
            d = deep_analyze(it)
            if d and d.get("信号") and not d.get("可忽略"):
                d["_src"] = it
                signals.append(d); picked.append(sg)
                if it["lane"] == "AI技术": ai_used += 1
                print(f"[signal] ✓ [{it['lane']}] {d['信号'][:46]}", flush=True)
        except Exception as ex:
            print(f"[deep] 失败: {str(ex)[:80]}", flush=True)

    # 该忽略热点: 高热度低移植
    noise = [x for x in scored if x.get("points", 0) >= 80 and x.get("transplant", 0) <= 1][:2]

    # ── 组周报 md ──
    L = [f"# 🦅 鹰眼周报 · {TODAY} | 领先空地 {len(signals)} 个", "",
         f"> 定位: 领先空地猎手+跨界移植引擎(重构v1·MVP三赛道) · 扫描 {len(items)} 条(arXiv/HN/政策RSS) · 免费池打分",
         "> 铁律: 领先≠太早,每条标档位;拿不准标待验证。", ""]
    for k, d in enumerate(signals, 1):
        s = d["_src"]
        L += [f"## 信号{k} · {d.get('信号','')}",
              f"- **透镜**: {d.get('透镜','')} | **档位**: 【{d.get('档位','')}】 | 置信: {d.get('置信','')}",
              f"- **为何是空地**: {d.get('为何是空地','')}",
              f"- **跨界移植点**: {d.get('跨界移植点','')}",
              f"- **建议动作**: {d.get('建议动作','')}",
              f"- 来源: [{s['lane']}] {s['title'][:80]} · {s.get('url','')}", ""]
    if noise:
        L += ["## 🙉 本周该忽略的热点(防FOMO)"]
        for n in noise:
            L += [f"- {n['title'][:80]} —— 热度高({n.get('points','')}分)但移植度低,别人主场,不追。"]
        L += [""]
    L += ["---", "*内部文档·采集方法进后厨不外露 · 免费池网关 · weekly_hunter_v1*"]
    md = "\n".join(L)

    with open(f"weekly_report_{TODAY}.md", "w", encoding="utf-8") as f:
        f.write(md)
    print("[out] md 已落盘", flush=True)

    # gh issue 推手机
    title = f"🦅鹰眼周报 {TODAY} | {len(signals)}个领先空地+跨界移植"
    try:
        r = subprocess.run(["gh", "issue", "create", "-R", "gufangAI/sync-med",
                            "--title", title, "--body", md],
                           capture_output=True, text=True, timeout=60)
        print(f"[issue] {'✓ ' + r.stdout.strip() if r.returncode == 0 else '✗ ' + r.stderr[:200]}", flush=True)
    except Exception as ex:
        print(f"[issue] 失败: {str(ex)[:120]}", flush=True)

if __name__ == "__main__":
    main()
