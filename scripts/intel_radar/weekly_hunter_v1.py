# -*- coding: utf-8 -*-
'\n\u60c5\u62a5\u9e70\u773c \xb7 \u5468\u62a5\u730e\u624b v1 (MVP)\n\u5b9a\u4f4d: \u9886\u5148\u7a7a\u5730\u730e\u624b + \u8de8\u754c\u79fb\u690d\u5f15\u64ce(\u7167\u300a\u60c5\u62a5\u9e70\u773c_\u91cd\u6784\u8bf4\u660e_v1\u300b\xa79 MVP \u843d\u5730)\n  \u4e09\u8d5b\u9053: AI\u6280\u672f(arXiv) + \u5176\u4ed6\u5782\u76f4\u884c\u4e1aAI(HN Algolia) + \u653f\u7b56/\u4e2d\u6587\u884c\u4e1a(Bing News RSS)\n  \u6253\u5206:   \u9886\u5148\u5ea6 \xd7 \u53ef\u79fb\u690d\u5ea6(LLM, \u514d\u8d39\u6c60\u7f51\u5173)\n  \u4ea7\u51fa:   \u6bcf\u5468 3 \u4e2a\u9886\u5148\u7a7a\u5730 + 1 \u4e2a\u52a8\u4f5c\u5efa\u8bae + \u8be5\u5ffd\u7565\u70ed\u70b9 \u2192 gh issue \u63a8\u624b\u673a + md \u5f52\u6863\n\u94c1\u5f8b: \u9886\u5148\u2260\u592a\u65e9(\u6bcf\u6761\u6807 \u73b0\u5728\u505a/\u57cb\u7740\u7b49/\u53ea\u662f\u8bb0\u5f55);\u8bda\u5b9e\u6807\u6ce8\u6765\u6e90/\u65f6\u95f4;\u62ff\u4e0d\u51c6\u6807"\u5f85\u9a8c\u8bc1"\u3002\n\u5168\u7a0b\u514d\u8d39\u6c60\u7f51\u5173 ai.gufangai.com,\u96f6\u4ed8\u8d39 API\u3002\u65b0\u5efa\u4e0d\u8986\u76d6 daily_report_v3(\u5b83\u7167\u8dd1,\u9a8c\u8bc1\u5468\u62a5\u4ef7\u503c\u540e\u518d\u8c08\u88c1\u64a4)\u3002\n'
import json, re, sys, time, datetime, subprocess, urllib.request, urllib.parse, xml.etree.ElementTree as ET

GW = "https://ai.gufangai.com/v1/chat/completions"
GW_KEY = "gufang"
TODAY = datetime.date.today().isoformat()

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "EagleEyeWeekly/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def llm(system, user, max_tokens=1600, timeout=120):
    
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
            if t: out.append({"lane": 'AI\u6280\u672f', "title": t, "brief": s, "url": u})
    except Exception as ex:
        print(f"[arxiv] \u5931\u8d25(\u5982\u5b9e): {str(ex)[:100]}", flush=True)
    print(f"[arxiv] {len(out)} \u6761", flush=True)
    return out




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
            for it in list(root.iter("item"))[:8]:   
                t = (it.findtext("title") or "").strip()
                if not t or t in seen: continue
                seen.add(t)
                desc = re.sub(r"<[^>]+>", " ", (it.findtext("description") or "")).strip()[:220]
                out.append({"lane": '\u522b\u884c\u4e1a\u6253\u6cd5', "title": t,
                            "brief": f"[{name}] {desc}", "url": it.findtext("link") or ""})
        except Exception as ex:
            print(f"[playbook:{name}] \u5931\u8d25: {str(ex)[:80]}", flush=True)
        time.sleep(0.4)
    print(f"[playbook] {len(out)} \u6761", flush=True)
    return out


def lane_policy():
    out, seen = [], set()
    for q in ['\u4e2d\u533b\u836f \u653f\u7b56', '\u533b\u7597\u6570\u636e \u8981\u7d20 \u653f\u7b56', '\u4eba\u7528\u7ecf\u9a8c \u4e2d\u836f', '\u4e2d\u533b \u4eba\u5de5\u667a\u80fd']:
        try:
            url = "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=rss"
            root = ET.fromstring(fetch(url))
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                if not t or t in seen: continue
                seen.add(t)
                out.append({"lane": '\u653f\u7b56/\u4e2d\u6587', "title": t,
                            "brief": (it.findtext("description") or "")[:200],
                            "url": it.findtext("link") or ""})
        except Exception as ex:
            print(f"[policy:{q}] \u5931\u8d25: {str(ex)[:80]}", flush=True)
        time.sleep(0.6)
    print(f"[policy] {len(out)} \u6761", flush=True)
    return out


SCORE_SYS = '\u4f60\u662f\u300c\u53e4\u65b9AI\u661f\u56fe\u300d\u7684\u60c5\u62a5\u6253\u5206\u5668\u3002\u80cc\u666f:\u6211\u4eec\u505a\u4e2d\u533b\u53e4\u7c4dAI(\u5b66\u6d3e\u601d\u7ef4\u5206\u8eab/\u5224\u65ad\u5f15\u64ce/\u53e4\u7c4d\u51fa\u5904\u6eaf\u6e90/\u80b2\u4eba/\u8ba2\u9605\u53d8\u73b0)\u3002\n\u5bf9\u6bcf\u6761\u60c5\u62a5\u6253\u4e24\u4e2a\u5206(0-5):\n- leading \u9886\u5148\u5ea6: \u8fd9\u4e8b\u662f\u5426"\u522b\u4eba\u8fd8\u6ca1\u770b\u89c1/\u8fd8\u6ca1\u5927\u89c4\u6a21\u505a"(\u8d8a\u65e9\u671f\u8d8a\u51b7\u95e8\u8d8a\u9ad8;\u5237\u5c4f\u70ed\u70b9=\u4f4e)\n- transplant \u53ef\u79fb\u690d\u5ea6: \u2605\u6700\u9ad8\u5206\u7ed9"\u522b\u7684\u5782\u76f4\u884c\u4e1a/\u522b\u7684\u884c\u4e1a\u7684\u3010\u4ea7\u54c1\u6253\u6cd5\xb7\u5546\u4e1a\u6a21\u5f0f\xb7\u4fe1\u4efb\u673a\u5236\u3011"\u2014\u2014\u6cd5\u5f8bAI\u600e\u4e48\u505a\u51fa\u5904\u6eaf\u6e90\u3001\u589e\u957f\u56e2\u961f\u600e\u4e48\u505a\u7528\u6237\u7559\u5b58\u3001\u8d44\u672c\u600e\u4e48\u7ed9\u6309\u7ed3\u679c\u4ed8\u8d39\u5b9a\u4ef7\u3001\u522b\u7684\u4e13\u5bb6\u578bAI\u600e\u4e48\u590d\u73b0\u5224\u65ad,\u8fd9\u4e9b\u4e2d\u533b\u73a9\u5bb6\u7ed3\u6784\u4e0a\u4e0d\u770b\u3001\u5374\u80fd\u76f4\u63a5\u642c\u8fdb\u53e4\u65b9\u3002\'\u53c8\u4e00\u7bc7AI\u80fd\u529b\u8bba\u6587/\u65b0\u6a21\u578b/\u65b0\u7b97\u6cd5\'\u9664\u975e\u76f4\u63a5\u89e3\u9501\u4e00\u4e2a\u65b0\u4ea7\u54c1\u80fd\u529b,\u5426\u5219 transplant \u538b\u5230\u22642(\u90a3\u662f\u4eba\u4eba\u90fd\u5728\u770b\u7684\u7ea2\u6d77)\u3002\u7eaf\u786c\u4ef6/\u65e0\u5173=0\u3002\n\u53ea\u8f93\u51faJSON\u6570\u7ec4:[{"i":\u5e8f\u53f7,"leading":n,"transplant":n,"move":"\u4e00\u53e5\u79fb\u690d\u70b9(\u6ca1\u6709\u5219\u7a7a)"}]'

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
            print(f"[score] \u6279{s}\u5931\u8d25: {str(ex)[:80]}", flush=True)
        print(f"[score] {min(s+B,len(items))}/{len(items)}", flush=True)
    return scored


DEEP_SYS = '\u4f60\u662f\u300c\u53e4\u65b9AI\u661f\u56fe\u300d\u7684\u8de8\u754c\u79fb\u690d\u5206\u6790\u5b98\u3002\u6211\u4eec:\u4e2d\u533b\u53e4\u7c4dAI,\u8d44\u4ea7=\u5b66\u6d3e\u601d\u7ef4\u5206\u8eab+\u5224\u65ad\u5f15\u64ce+\u53e4\u7c4d\u51fa\u5904+\u6d77\u5916\u5b64\u672c\u8bed\u6599,\u53d8\u73b0=\u8ba2\u9605/\u80b2\u4eba\u3002\n\u5bf9\u7ed9\u4f60\u7684\u60c5\u62a5,\u56de\u7b54\u91cd\u6784\u8bf4\u660e\u7684\u6838\u5fc3\u53e5:"X\u884c\u4e1a\u7528Z\u65b9\u6cd5\u89e3\u51b3\u4e86Y\u95ee\u9898,\u4e2d\u533b\u884c\u4e1a\u73a9\u5bb6\u4e0d\u4f1a\u770b\u5230\u2014\u2014\u80fd\u4e0d\u80fd\u642c\u8fdb\u53e4\u65b9?"\n\u8f93\u51faJSON:{"\u4fe1\u53f7":"\u4e00\u53e5\u8bdd\u8bf4\u6e05\u662f\u4ec0\u4e48","\u900f\u955c":"\u6280\u672f\u62d0\u70b9/\u653f\u7b56\u62d0\u70b9/\u7ade\u4e89\u76f2\u533a","\u4e3a\u4f55\u662f\u7a7a\u5730":"\u522b\u4eba\u4e3a\u4ec0\u4e48\u8fd8\u6ca1\u770b\u89c1/\u6ca1\u505a",\n"\u8de8\u754c\u79fb\u690d\u70b9":"\u5728\u54ea\u4e2a\u884c\u4e1a\u5df2\u8dd1\u901a\u2192\u600e\u4e48\u642c\u8fdb\u53e4\u65b9(\u5177\u4f53)","\u5efa\u8bae\u52a8\u4f5c":"\u4e0b\u4e00\u6b65\u5177\u4f53\u505a\u4ec0\u4e48(\u4e00\u53e5)",\n"\u6863\u4f4d":"\u73b0\u5728\u505a/\u57cb\u7740\u7b49/\u53ea\u662f\u8bb0\u5f55","\u53ef\u5ffd\u7565":"\u82e5\u8be5\u5ffd\u7565\u5199\u7406\u7531,\u5426\u5219\u7a7a","\u7f6e\u4fe1":"\u9ad8/\u4e2d/\u5f85\u9a8c\u8bc1"}\n\u94c1\u5f8b:\u9886\u5148\u2260\u592a\u65e9,\u6863\u4f4d\u8981\u8bda\u5b9e;\u62ff\u4e0d\u51c6\u6807\u5f85\u9a8c\u8bc1;\u4e0d\u7f16\u9020\u3002\u53ea\u8f93\u51faJSON\u3002'

def deep_analyze(it):
    u = f"[{it['lane']}] {it['title']}\n{it['brief']}\n\u6765\u6e90:{it.get('url','')}"
    j = pick_json(llm(DEEP_SYS, u, max_tokens=900))
    return j if isinstance(j, dict) else None

def main():
    print(f"=== \u9e70\u773c\u5468\u62a5\u730e\u624b v1 · {TODAY} ===", flush=True)
    items = lane_arxiv() + lane_playbook() + lane_policy()
    if len(items) < 10:
        print(f"[warn] \u6e90\u592a\u5c11({len(items)}),\u5982\u5b9e\u7ee7\u7eed", flush=True)
    scored = score_items(items)
    scored.sort(key=lambda x: (x.get("leading", 0) * x.get("transplant", 0), x.get("transplant", 0)), reverse=True)
    
    
    by_lane = {}
    for x in scored:
        if x.get("transplant", 0) >= 2:
            by_lane.setdefault(x["lane"], []).append(x)
    cands = (by_lane.get('\u522b\u884c\u4e1a\u6253\u6cd5', [])[:6] + by_lane.get('\u653f\u7b56/\u4e2d\u6587', [])[:4]
             + by_lane.get('AI\u6280\u672f', [])[:3])
    print(f"[cand] \u522b\u884c\u4e1a\u6253\u6cd5={len(by_lane.get('别行业打法',[]))} \u653f\u7b56={len(by_lane.get('政策/中文',[]))} "
          f"AI\u6280\u672f={len(by_lane.get('AI技术',[]))}", flush=True)

    def toksig(t):   
        return set(re.findall('[\u4e00-\u9fff]{2}|[a-z]{4,}', t.lower()))

    signals, ai_used, picked = [], 0, []
    for it in cands:
        if len(signals) >= 3: break
        if it["lane"] == 'AI\u6280\u672f' and ai_used >= 1:   
            continue
        sg = toksig(it["title"])
        if any(len(sg & p) / max(len(sg | p), 1) > 0.45 for p in picked):   
            print(f"[dedup] \u8df3\u8fc7\u8fd1\u91cd\u590d: {it['title'][:40]}", flush=True); continue
        try:
            d = deep_analyze(it)
            if d and d.get('\u4fe1\u53f7') and not d.get('\u53ef\u5ffd\u7565'):
                d["_src"] = it
                signals.append(d); picked.append(sg)
                if it["lane"] == 'AI\u6280\u672f': ai_used += 1
                print(f"[signal] ✓ [{it['lane']}] {d['信号'][:46]}", flush=True)
        except Exception as ex:
            print(f"[deep] \u5931\u8d25: {str(ex)[:80]}", flush=True)

    
    noise = [x for x in scored if x.get("points", 0) >= 80 and x.get("transplant", 0) <= 1][:2]

    
    L = [f"# 🦅 \u9e70\u773c\u5468\u62a5 · {TODAY} | \u9886\u5148\u7a7a\u5730 {len(signals)} \u4e2a", "",
         f"> \u5b9a\u4f4d: \u9886\u5148\u7a7a\u5730\u730e\u624b+\u8de8\u754c\u79fb\u690d\u5f15\u64ce(\u91cd\u6784v1·MVP\u4e09\u8d5b\u9053) · \u626b\u63cf {len(items)} \u6761(arXiv/HN/\u653f\u7b56RSS) · \u514d\u8d39\u6c60\u6253\u5206",
         '> \u94c1\u5f8b: \u9886\u5148\u2260\u592a\u65e9,\u6bcf\u6761\u6807\u6863\u4f4d;\u62ff\u4e0d\u51c6\u6807\u5f85\u9a8c\u8bc1\u3002', ""]
    for k, d in enumerate(signals, 1):
        s = d["_src"]
        L += [f"## \u4fe1\u53f7{k} · {d.get('信号','')}",
              f"- **\u900f\u955c**: {d.get('透镜','')} | **\u6863\u4f4d**: 【{d.get('档位','')}】 | \u7f6e\u4fe1: {d.get('置信','')}",
              f"- **\u4e3a\u4f55\u662f\u7a7a\u5730**: {d.get('为何是空地','')}",
              f"- **\u8de8\u754c\u79fb\u690d\u70b9**: {d.get('跨界移植点','')}",
              f"- **\u5efa\u8bae\u52a8\u4f5c**: {d.get('建议动作','')}",
              f"- \u6765\u6e90: [{s['lane']}] {s['title'][:80]} · {s.get('url','')}", ""]
    if noise:
        L += ['## \U0001f649 \u672c\u5468\u8be5\u5ffd\u7565\u7684\u70ed\u70b9(\u9632FOMO)']
        for n in noise:
            L += [f"- {n['title'][:80]} —— \u70ed\u5ea6\u9ad8({n.get('points','')}\u5206)\u4f46\u79fb\u690d\u5ea6\u4f4e,\u522b\u4eba\u4e3b\u573a,\u4e0d\u8ffd。"]
        L += [""]
    L += ["---", '*\u5185\u90e8\u6587\u6863\xb7\u91c7\u96c6\u65b9\u6cd5\u8fdb\u540e\u53a8\u4e0d\u5916\u9732 \xb7 \u514d\u8d39\u6c60\u7f51\u5173 \xb7 weekly_hunter_v1*']
    md = "\n".join(L)

    with open(f"weekly_report_{TODAY}.md", "w", encoding="utf-8") as f:
        f.write(md)
    print('[out] md \u5df2\u843d\u76d8', flush=True)

    
    title = f"🦅\u9e70\u773c\u5468\u62a5 {TODAY} | {len(signals)}\u4e2a\u9886\u5148\u7a7a\u5730+\u8de8\u754c\u79fb\u690d"
    try:
        r = subprocess.run(["gh", "issue", "create", "-R", "gufangAI/sync-med",
                            "--title", title, "--body", md],
                           capture_output=True, text=True, timeout=60)
        print(f"[issue] {'✓ ' + r.stdout.strip() if r.returncode == 0 else '✗ ' + r.stderr[:200]}", flush=True)
    except Exception as ex:
        print(f"[issue] \u5931\u8d25: {str(ex)[:120]}", flush=True)

if __name__ == "__main__":
    main()
