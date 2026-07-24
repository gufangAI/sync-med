'\n\u60c5\u62a5\u96f7\u8fbe \xb7 \u6d77\u91cf\u6293\u53d6 + \u591a\u6a21\u578b\u5e76\u884c\u7b5b\u7cbe\u534e v3\n==========================================\n\u5347\u7ea7\u4eae\u70b9:\n  1. \u6d77\u91cf\u6293\u53d6 (vs \u65e7\u7248 35 \u6761)\n     - arXiv: cs.AI + cs.CL + cs.IR \u5404\u6700\u591a 300 \u6761 (9 \u5929\u7a97\u53e3) \u2192 ~900 \u6761\n     - GitHub Search API: AI/ML/LLM \u4ed3\u5e93\u6309 stars + created \u65f6\u95f4\u7a97 \u2192 200-400 \u4e2a\n     - HuggingFace: daily papers + trending models\n     - PubMed: \u4e2d\u533b AI \u76f8\u5173 (30 \u5929) \u2192 50+ \u6761\n     - \u603b\u91cf: 1000-1500 \u6761/\u5929\n  2. \u591a\u6a21\u578b\u5e76\u884c\u5206\u6790\n     - \u4f18\u5148\u8c03\u672c\u5730\u6838\u52a8\u529b\u6c60 localhost:4000 (glm-4-flash / siliconflow-qwen / modelscope-qwen)\n     - \u628a\u5927\u91cf\u5206\u6279 (\u6bcf\u6279 30 \u6761)\uff0c\u7528 asyncio \u5e76\u53d1\u591a worker \u540c\u65f6\u6253\u4e0d\u540c\u6a21\u578b\n     - \u63a7\u901f: \u6bcf\u6a21\u578b\u9650\u5e76\u53d1 3\uff0c\u6279\u95f4 sleep \u907f\u514d\u8585\u7206\n  3. \u7b5b\u7cbe\u534e + \u5206\u7c7b\n     - \u6bcf\u6761\u6253\u5206 1-5 + \u5206\u7c7b\u6807\u7b7e (RAG/\u5224\u65ad\u5f15\u64ce/OCR/\u4e2d\u533bNLP/\u7ade\u54c1/\u65b9\u6cd5\u524d\u6cbf)\n     - \u6309\u5206\u6570\u6392\u5e8f\uff0c\u7b5b\u51fa TOP 50\n  4. KPI \u62a5\u544a\n\n\u672c\u5730\u8fd0\u884c (\u9700\u6838\u52a8\u529b\u6c60\u5df2\u542f\u52a8 localhost:4000):\n    python daily_report_v3.py\n\n\u4e91\u7aef GitHub Actions \u8fd0\u884c (\u65e0\u672c\u5730\u7f51\u5173):\n    \u8bbe\u7f6e ZHIPU_API_KEY \u6216 NVIDIA_API_KEY\n    python daily_report_v3.py --cloud\n\n\u63a7\u989d\u5ea6:\n    - \u9b54\u642d modelscope: 2000 \u6b21/\u5929 \u2192 \u5206 33 \u6279 \xd7 30 \u6761,\u6bcf\u6279 1 \u6b21\u8c03\u7528\n    - \u7845\u57fa siliconflow: 1000 RPM \u2192 \u6279\u95f4 2s sleep \u591f\u7528\n    - \u667a\u8c31 glm-4-flash: \u6c38\u4e45\u514d\u8d39,\u65e0\u660e\u786e\u4e0a\u9650 \u2192 \u4e3b\u529b\n    - NVIDIA: credits \u6709\u9650 \u2192 \u4ec5 fallback,\u4e0d\u4e3b\u52a8\u8c03\n'

import sys
import os
import re
import time
import json
import asyncio
import datetime
import urllib.request
import urllib.error
import urllib.parse
import http.client
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import argparse


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass



SCRIPT_DIR  = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)



GATEWAY_BASE   = "http://localhost:4000"
GATEWAY_KEY    = "sk-litellm-local-dev"


GATEWAY_MODELS = [
    "glm-4-flash",        
    "modelscope-qwen",    
    "siliconflow-qwen",   
]


NVIDIA_BASE    = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL   = "deepseek-ai/deepseek-v4-flash"
ZHIPU_BASE     = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL    = "glm-4-flash"
NVIDIA_KEY     = os.environ.get("NVIDIA_API_KEY", "")
ZHIPU_KEY      = os.environ.get("ZHIPU_API_KEY", "")


ARXIV_DAYS       = 3      
ARXIV_MAX        = 300    
ARXIV_CATS       = ["cs.AI", "cs.CL", "cs.IR"]

ARXIV_QUERIES    = [
    "traditional Chinese medicine language model", "classical Chinese NLP",
    "ancient Chinese text understanding", "syndrome differentiation TCM",
    "traditional medicine knowledge graph", "medical LLM citation grounding",
    "retrieval augmented generation attribution", "classical Chinese translation",
    "GraphRAG knowledge graph retrieval", "LLM as a judge evaluation",
    "mixture of agents ensemble",
]
ARXIV_QUERY_DAYS = 90     
ARXIV_QUERY_MAX  = 25
GITHUB_MAX       = 300    
PUBMED_DAYS      = 30     
PUBMED_MAX       = 50
HF_TRENDING_MAX  = 100


BATCH_SIZE       = 30     
MAX_WORKERS      = 3      
BATCH_SLEEP      = 2.0    


SUEAI_CONTEXT = '\nSueAI \u662f\u4e2d\u533b\u53e4\u7c4d\u667a\u80fd\u5206\u6790\u7cfb\u7edf:\n1. \u60c5\u62a5\u96f7\u8fbe \u2014 \u626b AI \u524d\u6cbf,\u7b5b\u5bf9\u4e2d\u533b AI \u6709\u4ef7\u503c\u7684\n2. RAG \u68c0\u7d22 \u2014 2100+ \u53e4\u5178\u533b\u6848 + 7700+ \u53e4\u7c4d\u5411\u91cf\u68c0\u7d22\n3. AI \u5bfb\u8109 \u2014 \u75c7\u72b6 \u2192 \u53e4\u7c4d\u8fa8\u8bc1\u53c2\u9605\u62a5\u544a (\u6587\u732e\u4e3b\u8bed,\u975e\u8bca\u7597)\n4. \u5224\u65ad\u6eaf\u6e90 \u2014 AI \u8f93\u51fa\u6807\u6ce8\u6587\u732e\u51fa\u5904 + \u53ef\u4fe1\u5ea6\u5206\u7ea7\n5. \u4e13\u5bb6\u5206\u8eab \u2014 \u4e2d\u533b\u540d\u5bb6\u5b66\u6d3e\u89c6\u89d2\u95ee\u7b54\n6. \u53e4\u7c4d OCR \u2014 \u626b\u63cf\u7248\u533b\u4e66/\u53e4\u7c4d\u6587\u5b57\u5316\n\n\u5173\u952e\u6280\u672f\u65b9\u5411: \u4e2d\u533b NLP\u3001\u53e4\u7c4d OCR\u3001RAG/GraphRAG\u3001\u6587\u672c\u5206\u7c7b\u3001embedding\u3001\n\u77e5\u8bc6\u56fe\u8c31\u3001\u4e2d\u533b\u672f\u8bed\u6807\u51c6\u5316\u3001\u4f20\u7edf\u533b\u5b66\u6587\u732e\u6316\u6398\u3001\u591a\u6587\u6863\u63a8\u7406\u3001\u53ef\u89e3\u91ca AI\n\n\u5206\u7c7b\u6807\u7b7e\u5b9a\u4e49:\n- RAG: \u68c0\u7d22\u589e\u5f3a\u751f\u6210\u3001\u5411\u91cf\u68c0\u7d22\u3001embedding\u3001\u5411\u91cf\u6570\u636e\u5e93\n- \u5224\u65ad\u5f15\u64ce: \u53ef\u89e3\u91ca AI\u3001\u6eaf\u6e90\u3001\u8bc1\u636e\u94fe\u3001\u77e5\u8bc6\u56fe\u8c31\u63a8\u7406\n- OCR/\u6587\u5b57\u5316: \u6587\u6863 OCR\u3001\u7248\u9762\u5206\u6790\u3001\u53e4\u6587\u8bc6\u522b\n- \u4e2d\u533bNLP: \u4e2d\u533b\u672f\u8bed\u3001\u4f20\u7edf\u533b\u5b66\u3001\u53e4\u7c4d\u5206\u6790\n- \u65b9\u6cd5\u524d\u6cbf: \u65b0\u578b\u67b6\u6784/\u8bad\u7ec3\u65b9\u6cd5,\u901a\u7528\u4f46\u5bf9\u6211\u4eec\u6709\u501f\u9274\u4ef7\u503c\n- \u7ade\u54c1\u60c5\u62a5: \u540c\u7c7b\u533b\u7597 AI \u4ea7\u54c1\u3001\u4e2d\u533b AI \u5e73\u53f0\n- \u514d\u8d39\u8d44\u6e90: \u53ef\u76f4\u63a5\u590d\u7528\u7684\u5f00\u6e90\u6a21\u578b/\u6570\u636e\u96c6/\u5de5\u5177\n'



def fetch_url(url: str, timeout: int = 30, data: bytes = None,
              headers: dict = None) -> str:
    '\u540c\u6b65 HTTP \u8bf7\u6c42'
    
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        from urllib.parse import quote as _q
        url = _q(url, safe=":/?&=#%+@[]~*'();,!$")
    req_headers = {"User-Agent": "IntelRadar/3.0 (SueAI)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers,
                                  method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25: {e}")
    except (http.client.HTTPException, ConnectionError, TimeoutError, OSError) as e:
        raise RuntimeError(f"connection glitch (transient, not fatal): {e}")




def fetch_arxiv_cat(cat: str, max_results: int = ARXIV_MAX) -> list:
    '\u6293\u53d6\u5355\u4e2a arXiv \u5206\u7c7b\uff0c\u8fd4\u56de\u8bba\u6587\u5217\u8868'
    url = (
        f"http://export.arxiv.org/api/query"
        f"?search_query=cat:{cat}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
        f"&start=0"
    )
    try:
        raw = fetch_url(url, timeout=60)
    except RuntimeError as e:
        print(f"    [{cat}] \u5931\u8d25: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"    [{cat}] XML \u89e3\u6790\u5931\u8d25: {e}")
        return []

    cutoff = datetime.date.today() - datetime.timedelta(days=ARXIV_DAYS)
    papers = []
    for entry in root.findall("atom:entry", ns):
        title    = (entry.findtext("atom:title", "", ns) or "").replace("\n", " ").strip()
        abstract = (entry.findtext("atom:summary", "", ns) or "").replace("\n", " ").strip()
        arxiv_id = (entry.findtext("atom:id", "", ns) or "").strip()
        published = (entry.findtext("atom:published", "", ns) or "").strip()

        
        if published:
            try:
                pub_date = datetime.date.fromisoformat(published[:10])
                if pub_date < cutoff:
                    continue
            except ValueError:
                pass

        if title:
            papers.append({
                "id": arxiv_id, "title": title,
                "abstract": abstract[:600], "url": arxiv_id,
                "source": f"arXiv {cat}", "published": published[:10],
            })
    return papers


def fetch_arxiv_query(q: str) -> list:
    '\u9776\u5411\u5173\u952e\u8bcd\u67e5\u8be2 arXiv(all \u5b57\u6bb5),\u5bbd\u65f6\u95f4\u7a97\u635e\u6211\u4eec niche \u5c0f\u4f17\u8bba\u6587'
    import urllib.parse as _up
    url = (
        f"http://export.arxiv.org/api/query"
        f"?search_query=all:{_up.quote(q)}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={ARXIV_QUERY_MAX}&start=0"
    )
    try:
        raw = fetch_url(url, timeout=60)
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"    [query:{q[:20]}] \u5931\u8d25: {e}")
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cutoff = datetime.date.today() - datetime.timedelta(days=ARXIV_QUERY_DAYS)
    papers = []
    for entry in root.findall("atom:entry", ns):
        title    = (entry.findtext("atom:title", "", ns) or "").replace("\n", " ").strip()
        abstract = (entry.findtext("atom:summary", "", ns) or "").replace("\n", " ").strip()
        arxiv_id = (entry.findtext("atom:id", "", ns) or "").strip()
        published = (entry.findtext("atom:published", "", ns) or "").strip()
        if published:
            try:
                if datetime.date.fromisoformat(published[:10]) < cutoff:
                    continue
            except ValueError:
                pass
        if title:
            papers.append({"id": arxiv_id, "title": title, "abstract": abstract[:600],
                           "url": arxiv_id, "source": "arXiv niche", "published": published[:10]})
    return papers


def fetch_arxiv_all() -> list:
    '\u5e76\u884c\u6293\u53d6 cs.AI + cs.CL + cs.IR'
    print(f"[\u6293\u53d6] arXiv ({', '.join(ARXIV_CATS)}) \u6700\u8fd1 {ARXIV_DAYS} \u5929 ...", flush=True)
    all_papers = []
    seen_ids = set()
    for cat in ARXIV_CATS:
        papers = fetch_arxiv_cat(cat)
        new = 0
        for p in papers:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_papers.append(p)
                new += 1
        print(f"    [{cat}] +{new} \u6761 (\u53bb\u91cd\u540e)")
        time.sleep(1)  
    
    nq = 0
    for q in ARXIV_QUERIES:
        for p in fetch_arxiv_query(q):
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"]); all_papers.append(p); nq += 1
        time.sleep(1)
    print(f"    [\u9776\u5411niche] +{nq} \u6761")
    print(f"  arXiv \u5408\u8ba1: {len(all_papers)} \u6761")
    return all_papers


def fetch_github_trending() -> list:
    '\n    \u7528 GitHub Search API \u6293\u6700\u8fd1 7 \u5929\u65b0\u5efa\u7684 AI/ML \u76f8\u5173\u70ed\u95e8\u4ed3\u5e93\u3002\n    \u65e0\u9700 token (\u533f\u540d 60\u6b21/h,\u591f\u7528)\u3002\n    '
    print(f"[\u6293\u53d6] GitHub Trending AI/ML (7\u5929\u5185,≤{GITHUB_MAX}) ...", flush=True)
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    queries = [
        f"topic:large-language-model created:>{cutoff_date}",
        f"topic:llm stars:>10 created:>{cutoff_date}",
        f"topic:rag created:>{cutoff_date}",
        f"topic:machine-learning stars:>50 created:>{cutoff_date}",
        f"topic:nlp stars:>20 created:>{cutoff_date}",
    ]

    seen_ids = set()
    repos = []
    for q in queries:
        if len(repos) >= GITHUB_MAX:
            break
        encoded = urllib.parse.quote(q)
        url = (
            f"https://api.github.com/search/repositories"
            f"?q={encoded}&sort=stars&order=desc&per_page=50"
        )
        try:
            raw = fetch_url(url, timeout=30, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            data = json.loads(raw)
        except (RuntimeError, json.JSONDecodeError) as e:
            print(f"    [GitHub] \u67e5\u8be2\u5931\u8d25 ({q[:50]}...): {e}")
            time.sleep(2)
            continue

        items = data.get("items", [])
        new = 0
        for item in items:
            rid = item.get("id")
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            desc = (item.get("description") or "").replace("\n", " ").strip()
            topics = ", ".join(item.get("topics", []))
            repos.append({
                "id": str(rid),
                "title": item.get("full_name", ""),
                "abstract": f"{desc} | \u8bdd\u9898: {topics} | ⭐{item.get('stargazers_count', 0)}",
                "url": item.get("html_url", ""),
                "source": "GitHub Trending",
                "stars": item.get("stargazers_count", 0),
                "lang": item.get("language", ""),
            })
            new += 1
        print(f"    [GitHub] q='{q[:40]}...' +{new} \u6761")
        time.sleep(1.5)  

    print(f"  GitHub \u5408\u8ba1: {len(repos)} \u4e2a\u4ed3\u5e93")
    return repos


def _strip_html(s: str) -> str:
    '\u53bb HTML \u6807\u7b7e + \u6298\u53e0\u7a7a\u767d,\u7ed9 RSS \u6458\u8981\u7528\u3002'
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&[a-z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_rss(url: str, source: str, max_items: int = 40) -> list:
    '\u901a\u7528 RSS/Atom \u6293\u53d6\u5668 \u2014\u2014 \u4e2d\u6587 AI \u8d44\u8baf\u7ad9\u591a\u6570\u63d0\u4f9b RSS,\u514d\u767b\u5f55\u514d key\u3002\n    \u8fd4\u56de\u7edf\u4e00 dict \u5217\u8868\u3002\u6293\u53d6\u5931\u8d25\u9759\u9ed8\u8fd4\u56de\u7a7a,\u4e0d\u963b\u65ad\u6574\u8f6e\u3002\n    '
    try:
        raw = fetch_url(url, timeout=25)
    except RuntimeError as e:
        print(f"    [RSS] {source} \u5931\u8d25: {e}")
        return []
    items = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    
    nodes = root.iter("item")
    entries = list(nodes)
    is_atom = False
    if not entries:
        entries = [e for e in root.iter() if e.tag.endswith("}entry")]
        is_atom = True
    for e in entries[:max_items]:
        if is_atom:
            title = (e.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            summ  = (e.findtext("{http://www.w3.org/2005/Atom}summary")
                     or e.findtext("{http://www.w3.org/2005/Atom}content") or "")
            link_el = e.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href") if link_el is not None else ""
        else:
            title = (e.findtext("title") or "").strip()
            summ  = e.findtext("description") or e.findtext("summary") or ""
            link  = (e.findtext("link") or "").strip()
        title = _strip_html(title)
        summ = _strip_html(summ)[:500]
        if not title:
            continue
        items.append({
            "id": link or title,
            "title": title,
            "abstract": summ,
            "url": link,
            "source": source,
        })
    print(f"    [RSS] {source} +{len(items)} \u6761")
    return items







CN_RSS_SOURCES = [
    ('\u91cf\u5b50\u4f4d',     "https://www.qbitai.com/feed"),          
    ('IT\u4e4b\u5bb6',     "https://www.ithome.com/rss/"),           
    ('\u673a\u5668\u4e4b\u5fc3',   "https://rsshub.rssforever.com/jiqizhixin"),  
    ('\u91cf\u5b50\u4f4d\u955c\u50cf', 'https://rsshub.rssforever.com/qbitai/category/\u8d44\u8baf'),
    ('36\u6c2a\u5feb\u8baf',   "https://rsshub.rssforever.com/36kr/newsflashes"),
    ("AIbase",     "https://rsshub.rssforever.com/aibase/news"),
]


def fetch_cn_intel() -> list:
    '\u6293\u4e2d\u6587 AI \u8d44\u8baf\u7ad9 RSS\u3002\u4efb\u4e00\u6e90\u6302\u4e86\u4e0d\u5f71\u54cd\u5176\u5b83\u3002'
    print('[\u6293\u53d6] \u4e2d\u6587 AI \u5b9e\u6218\u60c5\u62a5\u6e90 (RSS) ...', flush=True)
    out = []
    for name, url in CN_RSS_SOURCES:
        out.extend(fetch_rss(url, f"CN:{name}", max_items=40))
        time.sleep(1.0)
    print(f"  \u4e2d\u6587\u6e90\u5408\u8ba1: {len(out)} \u6761")
    return out


HN_RSS_SOURCES = [
    ("HN:Frontpage", "https://hnrss.org/frontpage"),
    ("HN:Show", "https://hnrss.org/show"),
    ("HN:Newest20pt", "https://hnrss.org/newest?points=20"),
]


def fetch_hn_intel() -> list:
    'Hacker News (hnrss.org bridge, no key needed) - catches niche/indie tools \
(e.g. Show HN posts) before they hit GitHub Trending or arXiv; this is the \
category of source most likely to carry a brand-new pattern/tool early.'
    print("[Fetch] Hacker News (frontpage+show+newest) ...", flush=True)
    out = []
    for name, url in HN_RSS_SOURCES:
        out.extend(fetch_rss(url, name, max_items=40))
        time.sleep(1.0)
    print(f"  Hacker News total: {len(out)}")
    return out


def fetch_github_freebies() -> list:
    '\u4e13\u6252 GitHub \u4e0a\u300e\u514d\u8d39\u7b97\u529b/\u7f51\u5173/agent\u300f\u8fd9\u7c7b\u53ef\u76f4\u63a5\u590d\u7528\u7684\u5de5\u5177,\u6309 stars \u8fd1 30 \u5929\u70ed\u5ea6\u7b5b\u3002\n    \u5bf9\u5e94\u300e\u628a\u53ef\u7528\u7684 GitHub / AI \u514d\u8d39\u7b97\u529b\u63a5\u5165\u81ea\u6709\u8c03\u5ea6\u6c60\u300f\u8fd9\u4e00\u65b9\u5411\u3002\n    '
    print('[\u6293\u53d6] GitHub \u514d\u8d39\u7b97\u529b/gateway/agent \u519b\u706b ...', flush=True)
    cutoff = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    queries = [
        f"ai gateway free openai-compatible pushed:>{cutoff}",
        f"llm proxy multi-provider free pushed:>{cutoff}",
        f"free api aggregator llm stars:>50 pushed:>{cutoff}",
        f"claude-code agent orchestration stars:>100 pushed:>{cutoff}",
    ]
    seen, repos = set(), []
    for q in queries:
        encoded = urllib.parse.quote(q)
        url = (f"https://api.github.com/search/repositories"
               f"?q={encoded}&sort=stars&order=desc&per_page=30")
        try:
            data = json.loads(fetch_url(url, timeout=30, headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }))
        except (RuntimeError, json.JSONDecodeError) as e:
            print(f"    [Freebies] \u67e5\u8be2\u5931\u8d25: {e}")
            time.sleep(2)
            continue
        for item in data.get("items", []):
            rid = item.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            desc = (item.get("description") or "").replace("\n", " ").strip()
            repos.append({
                "id": str(rid),
                "title": item.get("full_name", ""),
                "abstract": f"{desc} | ⭐{item.get('stargazers_count', 0)} | \u514d\u8d39\u7b97\u529b/\u5de5\u5177\u5019\u9009",
                "url": item.get("html_url", ""),
                "source": 'GitHub \u514d\u8d39\u519b\u706b',
                "stars": item.get("stargazers_count", 0),
            })
        time.sleep(1.5)
    print(f"  \u514d\u8d39\u519b\u706b\u5408\u8ba1: {len(repos)} \u4e2a")
    return repos


# ============================================================
# \u8865\u76f2\u533a\u96f7\u8fbe (github_radar \u5e76\u5165): \u8986\u76d6\u88ab\u4e3b\u626b\u63cf topic \u5199\u6b7b\u6f0f\u6389\u7684\u6574\u7c7b
#   \u2014\u2014 video / tts / ocr / knowledge-graph / tcm \u7b49
# \u4e3b\u626b\u63cf fetch_github_trending \u53ea\u6293 created:>7\u5929 \u7684"\u65b0\u5efa"\u4ed3\u5e93,
# \u6293\u4e0d\u5230 OpenCut / GPT-SoVITS / lossless-cut \u8fd9\u7c7b"\u5df2\u6210\u540d+\u4ecd\u6d3b\u8dc3"\u7684\u8001\u724c\u9ad8\u661f\u9879\u76ee\u3002
# \u672c\u96f7\u8fbe\u7528 stars:>N + pushed:>date \u4e13\u635e\u8fd9\u7c7b, \u8865\u4e0a\u4eca\u665a\u66b4\u9732\u7684 OpenCut \u76f2\u533a\u3002
# \u96f6\u672c\u5730\u7b97\u529b\u3001\u514d\u8d39 (GITHUB_TOKEN 5000/hr + \u514d\u8d39 glm-4-flash \u6253\u5206)\u3002
# ============================================================

BLINDSPOT_TOPIC_CLUSTERS = {
    "OCR\u71c3\u6599":  ["ocr", "document-understanding", "table-recognition", "document-ai"],
    "\u89c6\u9891\u5185\u5bb9": ["video-editor", "video-editing", "video-generation", "text-to-video"],
    "\u8bed\u97f3\u914d\u97f3": ["text-to-speech", "tts", "speech-synthesis", "voice-cloning"],
    "\u77e5\u8bc6\u56fe\u8c31": ["knowledge-graph", "graphrag", "graph-visualization"],
    "\u4e2d\u533b\u5782\u76f4": ["tcm", "chinese-medicine"],
}
BLINDSPOT_MIN_STARS   = 2000     # \u8001\u724c\u9ad8\u661f\u95e8\u69db
BLINDSPOT_ACTIVE_DAYS = 120      # pushed \u5728\u8fd1 N \u5929\u5185 = \u4ecd\u6d3b\u8dc3
BLINDSPOT_PER_TOPIC   = 8


def _load_arsenal_names() -> set:
    """\u53ef\u9009: \u8bfb committed \u7684 arsenal_repos.txt (ASCII repo \u77ed\u540d, \u4e00\u884c\u4e00\u4e2a) \u505a\u53bb\u91cd\u3002
    \u519b\u706b\u5e93\u603b\u53f0\u8d26.md \u5728\u672c\u5730 F \u76d8\u3001\u4e14\u662f CJK \u6b63\u6587, \u4e0d\u5b9c\u8fdb public \u4ed3, \u6240\u4ee5\u4e91\u7aef\u9ed8\u8ba4\u6ca1\u6709\u5b83\u3002
    \u8fd9\u4e2a hook \u8ba9\u5c06\u6765\u80fd\u585e\u4e00\u4efd curated \u5df2\u77e5\u540d\u5355\u8fdb\u6765\u800c\u4e0d\u5fc5\u6539\u4ee3\u7801;
    \u6ca1\u6709\u8be5\u6587\u4ef6\u65f6\u8df3\u8fc7\u53f0\u8d26\u53bb\u91cd, \u53ea\u505a run \u5185\u53bb\u91cd, \u5e76\u5728\u677f\u5757\u91cc\u5982\u5b9e\u6807\u6ce8\u8fd9\u5c42\u8fb9\u754c\u3002"""
    p = SCRIPT_DIR / "arsenal_repos.txt"
    names = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip().lower()
            if s and not s.startswith("#"):
                names.add(s.split("/")[-1])   # \u53ea\u7559 repo \u77ed\u540d
    return names


def fetch_blindspot_radar() -> list:
    """\u8865\u76f2\u533a\u96f7\u8fbe: \u6309 topic \u7c07\u626b GitHub \u9ad8\u661f(stars:>N)+\u6d3b\u8dc3(pushed:>date)\u7684\u8001\u724c\u9879\u76ee,
    \u8865\u4e3b\u626b\u63cf topic \u6f0f\u6389\u7684 video/tts/ocr/kg/tcm \u6574\u7c7b\u3002\u8fd4\u56de\u7edf\u4e00 dict \u5217\u8868(\u540c\u5176\u5b83 fetcher)\u3002"""
    print(f"[\u6293\u53d6] \u8865\u76f2\u533a\u96f7\u8fbe (video/tts/ocr/kg/tcm, stars>{BLINDSPOT_MIN_STARS}) ...", flush=True)
    cutoff = (datetime.date.today() - datetime.timedelta(days=BLINDSPOT_ACTIVE_DAYS)).isoformat()
    arsenal = _load_arsenal_names()
    gh_token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"   # 5000/hr, \u907f\u514d\u533f\u540d\u9650\u6d41

    seen_ids, cands = set(), []
    for cluster, topics in BLINDSPOT_TOPIC_CLUSTERS.items():
        for topic in topics:
            q = f"topic:{topic} stars:>{BLINDSPOT_MIN_STARS} pushed:>{cutoff}"
            url = (f"https://api.github.com/search/repositories"
                   f"?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={BLINDSPOT_PER_TOPIC}")
            try:
                data = json.loads(fetch_url(url, timeout=30, headers=headers))
            except (RuntimeError, json.JSONDecodeError) as e:
                print(f"    [\u8865\u76f2\u533a] topic={topic} \u67e5\u8be2\u5931\u8d25: {e}", flush=True)
                time.sleep(2)
                continue
            new = 0
            for item in data.get("items", []):
                rid = item.get("id")
                full = item.get("full_name", "")
                if not full or rid in seen_ids:
                    continue
                seen_ids.add(rid)
                short = full.split("/")[-1].lower()
                if short in arsenal:          # \u519b\u706b\u5e93\u5df2\u6536\u5f55(\u82e5\u6709\u540d\u5355) \u2192 \u8df3\u8fc7, \u53ea\u7559\u6f0f\u7f51
                    continue
                desc = (item.get("description") or "").replace("\n", " ").strip()
                gh_topics = ", ".join(item.get("topics", [])[:6])
                cands.append({
                    "id": str(rid),
                    "title": full,
                    "abstract": f"{desc} | \u8bdd\u9898: {gh_topics} | \u2b50{item.get('stargazers_count', 0)}",
                    "url": item.get("html_url", ""),
                    "source": "\U0001f195\u8865\u76f2\u533a\u96f7\u8fbe",
                    "stars": item.get("stargazers_count", 0),
                    "lang": item.get("language", ""),
                    "_cluster": cluster,
                })
                new += 1
            print(f"    [\u8865\u76f2\u533a] topic={topic:<22} +{new}", flush=True)
            time.sleep(1.2)
    cands.sort(key=lambda x: -x.get("stars", 0))
    print(f"  \u8865\u76f2\u533a\u96f7\u8fbe\u5408\u8ba1: {len(cands)} \u4e2a\u9ad8\u661f\u6d3b\u8dc3\u5019\u9009 (arsenal\u540d\u5355={len(arsenal)}\u6761)", flush=True)
    return cands


def fetch_hf_papers() -> list:
    '\u6293\u53d6 HuggingFace Daily Papers'
    print('[\u6293\u53d6] HuggingFace Daily Papers ...', end=" ", flush=True)
    try:
        raw = fetch_url("https://huggingface.co/api/daily_papers", timeout=30)
        data = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"\u5931\u8d25: {e}")
        return []

    papers = []
    for item in data:
        paper = item.get("paper", item)
        title    = paper.get("title", "").strip()
        abstract = paper.get("summary", paper.get("abstract", "")).replace("\n", " ").strip()
        pid      = paper.get("id", "")
        url      = f"https://huggingface.co/papers/{pid}" if pid else ""
        if title:
            papers.append({
                "id": pid, "title": title,
                "abstract": abstract[:600], "url": url,
                "source": "HF Daily",
            })
    print(f"OK, {len(papers)} \u6761")
    return papers


def fetch_hf_trending_models() -> list:
    '\u6293\u53d6 HuggingFace trending models (API)'
    print(f"[\u6293\u53d6] HuggingFace Trending Models (≤{HF_TRENDING_MAX}) ...", end=" ", flush=True)
    
    url = f"https://huggingface.co/api/models?sort=likes&limit={HF_TRENDING_MAX}&direction=-1"
    try:
        raw = fetch_url(url, timeout=30)
        data = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"\u5931\u8d25: {e}")
        return []

    models = []
    for item in data:
        mid   = item.get("modelId", item.get("id", ""))
        tags  = ", ".join(item.get("tags", []))
        likes = item.get("likes", 0)
        dl    = item.get("downloads", 0)
        models.append({
            "id": mid,
            "title": mid,
            "abstract": f"\u6807\u7b7e: {tags} | 👍{likes} | ⬇{dl}",
            "url": f"https://huggingface.co/{mid}",
            "source": "HF Trending Models",
        })
    print(f"OK, {len(models)} \u6761")
    return models


def fetch_pubmed() -> list:
    'PubMed \u4e2d\u533b AI \u76f8\u5173\u8bba\u6587'
    print(f"[\u6293\u53d6] PubMed TCM+AI (\u6700\u8fd1 {PUBMED_DAYS} \u5929) ...", end=" ", flush=True)
    search_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        f"?db=pubmed&retmode=json&retmax={PUBMED_MAX}"
        f"&term=traditional+Chinese+medicine+AND+artificial+intelligence"
        f"&sort=pub+date&datetype=pdat&reldate={PUBMED_DAYS}"
    )
    try:
        raw = fetch_url(search_url, timeout=30)
        search = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"\u641c\u7d22\u5931\u8d25: {e}")
        return []

    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        print('0 \u6761')
        return []

    fetch_url_pm = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&retmode=xml&rettype=abstract&id={','.join(pmids)}"
    )
    try:
        xml_raw = fetch_url(fetch_url_pm, timeout=60)
        root = ET.fromstring(xml_raw)
    except (RuntimeError, ET.ParseError) as e:
        print(f"efetch \u5931\u8d25: {e}")
        return []

    papers = []
    for article in root.findall(".//PubmedArticle"):
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        abstract_texts = article.findall(".//AbstractText")
        abstract = " ".join("".join(el.itertext()) for el in abstract_texts).strip()
        pmid_el = article.find(".//PMID")
        pmid = pmid_el.text.strip() if pmid_el is not None else ""
        if title:
            papers.append({
                "id": pmid, "title": title,
                "abstract": abstract[:600],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source": "PubMed",
            })

    print(f"OK, {len(papers)} \u6761")
    return papers




def _call_llm_sync(model: str, messages: list, max_tokens: int = 1500,
                   use_gateway: bool = True) -> str:
    '\n    \u540c\u6b65\u8c03\u7528 LLM\u3002\n    use_gateway=True \u2192 \u672c\u5730\u6838\u52a8\u529b\u6c60 localhost:4000\n    use_gateway=False \u2192 \u4e91\u7aef API (ZHIPU / NVIDIA)\n    '
    if use_gateway:
        base, key = GATEWAY_BASE, GATEWAY_KEY
    elif ZHIPU_KEY:
        base, key, model = ZHIPU_BASE, ZHIPU_KEY, ZHIPU_MODEL
    elif NVIDIA_KEY:
        base, key, model = NVIDIA_BASE, NVIDIA_KEY, NVIDIA_MODEL
    else:
        raise RuntimeError('\u65e0\u53ef\u7528 API key (\u7f51\u5173/ZHIPU/NVIDIA \u5747\u4e0d\u53ef\u7528)')

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")

    raw = fetch_url(
        f"{base}/chat/completions",
        timeout=150,  
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    result = json.loads(raw)
    return result["choices"][0]["message"]["content"].strip()


def check_gateway_alive() -> bool:
    '\u68c0\u67e5\u6838\u52a8\u529b\u6c60 localhost:4000 \u662f\u5426\u53ef\u7528'
    try:
        raw = fetch_url(f"{GATEWAY_BASE}/models", timeout=5,
                        headers={"Authorization": f"Bearer {GATEWAY_KEY}"})
        return True
    except Exception:
        return False




ANALYZE_PROMPT_TPL = '\u4f60\u662f SueAI \u60c5\u62a5\u96f7\u8fbe\u5206\u6790\u5927\u8111\u3002SueAI \u662f\u4e00\u4e2a\u300c\u4e2d\u533b\u53e4\u7c4d AI \u5e73\u53f0\u300d(AI\u5bfb\u8109/\u53e4\u7c4d\u5bfc\u8bfb/\u53e4\u7c4dOCR/\u77e5\u8bc6\u56fe\u8c31/RAG\u68c0\u7d22/\u53e4\u7c4d\u6570\u5b57\u5316)\u3002\n\n{context}\n\n\u4ee5\u4e0b\u662f\u4e00\u6279\u60c5\u62a5\u6761\u76ee (\u8bba\u6587/\u4ed3\u5e93/\u6a21\u578b)\u3002\u8bf7\u4e3a\u6bcf\u6761\u6253\u5206\u5e76\u5206\u7c7b\u3002\u6253\u5206\u7b2c\u4e00\u5224\u636e\u4e0d\u662f\u300c\u6280\u672f\u70ed\u4e0d\u70ed\u300d\uff0c\u800c\u662f\u300c\u5bf9\u672c\u4e2d\u533b\u53e4\u7c4d AI \u5e73\u53f0\u7684\u76f4\u63a5\u76f8\u5173\u6027\u300d\u3002\n\n\u6761\u76ee\u5217\u8868:\n{items}\n\n\u6253\u5206\u5224\u636e (\u5148\u5224\u76f8\u5173\u6027\u518d\u7ed9\u5206):\n- 5 = \u76f4\u63a5\u547d\u4e2d\u4e2d\u533b/\u53e4\u7c4d/\u6587\u8a00\u6587/\u4e2d\u533bNLP/\u53e4\u7c4dOCR/\u4e2d\u533b\u77e5\u8bc6\u56fe\u8c31/\u4e2d\u533b\u6216\u53e4\u7c4dRAG/\u53e4\u7c4d\u6570\u5b57\u5316\n- 4 = \u901a\u7528\u6280\u672f\u4f46\u80fd\u76f4\u63a5\u63a5\u5165\u6211\u4eec\u7684\u7ba1\u7ebf(RAG/\u68c0\u7d22/OCR/\u7248\u9762\u5206\u6790/Agent\u7f16\u6392/\u77e5\u8bc6\u56fe\u8c31/embedding/\u53ef\u89e3\u91caAI/\u6587\u732e\u6eaf\u6e90)\n- 3 = \u6709\u501f\u9274\u4ef7\u503c\u7684\u901a\u7528\u65b9\u6cd5\uff0c\u4f46\u9700\u6539\u9020\u624d\u80fd\u7528\u4e0a\n- 1-2 = \u7eaf\u901a\u7528AI\u786c\u4ef6(GPU\u670d\u52a1\u5668/\u8d85\u7b97/\u82af\u7247/\u7b97\u529b\u96c6\u7fa4)\u3001\u6216\u4e0e\u4e2d\u533b\u53e4\u7c4dAI\u65e0\u76f4\u63a5\u5173\u8054\u7684\u884c\u4e1a(\u94bb\u4e95/\u519b\u5de5/\u81ea\u52a8\u9a7e\u9a76/\u91d1\u878d\u91cf\u5316/\u6e38\u620f/\u5e7f\u544a\u7b49);\u5373\u4fbf\u6280\u672f\u70ed\u5ea6\u518d\u9ad8\uff0c\u5bf9\u672c\u5e73\u53f0\u4ef7\u503c\u4e5f\u4f4e\uff0c\u4e00\u5f8b 1-2\uff0c\u4e14\u4e0d\u5f97\u5f52\u5165\u300c\u65b9\u6cd5\u524d\u6cbf\u300d\u9ad8\u5206\n- \u4e0d\u76f8\u5173: \u8df3\u8fc7 (\u4e0d\u8f93\u51fa)\n\n\u5206\u7c7b\u6807\u7b7e\u53ea\u80fd\u53d6: RAG|\u5224\u65ad\u5f15\u64ce|OCR\u6587\u5b57\u5316|\u4e2d\u533bNLP|\u65b9\u6cd5\u524d\u6cbf|\u7ade\u54c1\u60c5\u62a5|\u514d\u8d39\u8d44\u6e90\n\u300c\u65b9\u6cd5\u524d\u6cbf\u300d\u4ec5\u9650\u300c\u65b0\u578b\u67b6\u6784/\u8bad\u7ec3/\u63a8\u7406\u65b9\u6cd5\u4e14\u6211\u4eec\u7ba1\u7ebf\u80fd\u76f4\u63a5\u501f\u9274\u300d\uff0c\u7eaf\u786c\u4ef6\u57fa\u5efa\u4e0e\u65e0\u5173\u884c\u4e1a\u4e0d\u5f97\u8fdb\u6b64\u7c7b\u3002\n\n\u53ea\u8f93\u51fa JSON \u6570\u7ec4 (\u65e0\u591a\u4f59\u6587\u5b57):\n[\n  {{\n    "index": <\u6761\u76ee\u7f16\u53f7>,\n    "score": <1-5>,\n    "category": "<RAG|\u5224\u65ad\u5f15\u64ce|OCR\u6587\u5b57\u5316|\u4e2d\u533bNLP|\u65b9\u6cd5\u524d\u6cbf|\u7ade\u54c1\u60c5\u62a5|\u514d\u8d39\u8d44\u6e90>",\n    "reason": "<\u4e00\u53e5\u8bdd: \u5bf9\u672c\u4e2d\u533b\u53e4\u7c4d\u5e73\u53f0\u54ea\u4e2a\u6a21\u5757\u6709\u4ef7\u503c;\u82e5\u901a\u7528/\u65e0\u5173\u987b\u70b9\u660e>"\n  }},\n  ...\n]\n\u65e0\u76f8\u5173\u6761\u76ee\u65f6\u8f93\u51fa []\u3002'









SYNTHESIS_DIMENSIONS = ['\u6280\u672f', '\u4e2d\u533b\u77e5\u8bc6', '\u7ade\u54c1', '\u53d8\u73b0', '\u98ce\u9669']

SYNTHESIS_PROMPT = '\u4f60\u662f SueAI \u5e73\u53f0\u7684\u603b\u60c5\u62a5\u5b98\uff0c\u8981\u628a\u4eca\u5929\u626b\u5230\u7684\u4fe1\u53f7\u505a"\u591a\u5b66\u79d1\u7efc\u5408\u7814\u5224"\uff0c\n\u4e0d\u662f\u5199\u8bba\u6587\u6458\u8981\uff0c\u662f\u7ed9\u5e73\u53f0 CTO \u4e00\u4efd\u80fd\u76f4\u63a5\u62cd\u677f\u7684\u51b3\u7b56\u7b80\u62a5\u3002\n\n{context}\n\n\u4eca\u5929\u7684\u7cbe\u534e\u4fe1\u53f7 (\u5df2\u6309\u76f8\u5173\u6027\u7b5b\u8fc7\uff0c\u542b \u6807\u9898/\u6765\u6e90/\u5206\u7c7b/\u6253\u5206\u7406\u7531):\n{items}\n\n\u8bf7\u628a\u8fd9\u4e9b\u4fe1\u53f7(\u4ee5\u53ca\u4f60\u80fd\u4ece\u4e2d\u5408\u7406\u63a8\u65ad\u7684\u5173\u8054)\u5f52\u5230\u4ee5\u4e0b 5 \u4e2a\u7ef4\u5ea6\uff0c\u5224\u65ad\u5bf9\u6211\u4eec"\u6709\u6ca1\u6709\u7528\u3001\u8981\u4e0d\u8981\u52a8":\n1. \u6280\u672f \u2014 \u5bf9\u6211\u4eec\u5224\u65ad\u5f15\u64ce/\u7f51\u7edc\u67b6\u6784\u6709\u7528\u7684 (RAG/OCR/Agent \u7f16\u6392/\u6a21\u578b/\u57fa\u7840\u8bbe\u65bd)\n2. \u4e2d\u533b\u77e5\u8bc6 \u2014 \u53e4\u7c4d\u6570\u5b57\u5316/\u533b\u6848/\u4e2d\u533b AI \u76f8\u5173\n3. \u7ade\u54c1 \u2014 \u4e2d\u533b AI \u8d5b\u9053\u52a8\u6001 (\u82e5\u4fe1\u53f7\u91cc\u6ca1\u6709\uff0c\u5982\u5b9e\u8bf4\u6ca1\u6709)\n4. \u53d8\u73b0 \u2014 \u793e\u5a92/AI \u5185\u5bb9\u53d8\u73b0\u76f8\u5173 (\u82e5\u4fe1\u53f7\u91cc\u6ca1\u6709\uff0c\u5982\u5b9e\u8bf4\u6ca1\u6709)\n5. \u98ce\u9669 \u2014 \u5408\u89c4/\u76d1\u7ba1\u52a8\u6001 (\u82e5\u4fe1\u53f7\u91cc\u6ca1\u6709\uff0c\u5982\u5b9e\u8bf4\u6ca1\u6709)\n\n\u94c1\u5f8b: \u4e25\u7981\u4e3a\u4e86\u51d1\u6570\u786c\u7f16\u3002\u67d0\u7ef4\u5ea6\u4eca\u5929\u4fe1\u53f7\u91cc\u786e\u5b9e\u6ca1\u6709\u76f8\u5173\u5185\u5bb9\uff0c\u5c31\u5728\u8be5\u7ef4\u5ea6\u8f93\u51fa\nno_signal=true + note="\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7"\uff0c\u7edd\u4e0d\u7f16\u9020\u65e0\u4e2d\u751f\u6709\u7684"\u53d1\u73b0"\u3002\n\n\u6bcf\u6761\u53d1\u73b0\u5fc5\u987b\u542b: finding(\u53d1\u73b0\u662f\u4ec0\u4e48) / meaning(\u5bf9\u6211\u4eec\u610f\u5473\u7740\u4ec0\u4e48) / action(\u8981\u4e0d\u8981\u884c\u52a8\uff0c\n\u7ed9\u5177\u4f53\u5efa\u8bae\uff0c\u6216\u660e\u8bf4"\u5148\u89c2\u5bdf\u4e0d\u52a8")\u3002\u6bcf\u4e2a\u7ef4\u5ea6\u6700\u591a 3 \u6761\uff0c\u6ca1\u6709\u5c31\u6807 no_signal\u3002\n\n\u53ea\u8f93\u51fa JSON\uff0c\u65e0\u591a\u4f59\u6587\u5b57\uff0c\u4e25\u683c\u6309\u6b64\u7ed3\u6784:\n{{\n  "\u6280\u672f":    {{"no_signal": false, "items": [{{"finding":"...","meaning":"...","action":"..."}}]}},\n  "\u4e2d\u533b\u77e5\u8bc6": {{"no_signal": false, "items": [...]}},\n  "\u7ade\u54c1":    {{"no_signal": true,  "note": "\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7", "items": []}},\n  "\u53d8\u73b0":    {{"no_signal": true,  "note": "\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7", "items": []}},\n  "\u98ce\u9669":    {{"no_signal": true,  "note": "\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7", "items": []}}\n}}'


def build_synthesis_input(top_items: list, opensource_results: Optional[list] = None,
                          skill_results_data: Optional[dict] = None, max_items: int = 40) -> str:
    '\u628a\u5f53\u5929\u7cbe\u534e\u4fe1\u53f7\u62fc\u6210\u591a\u5b66\u79d1\u7814\u5224 prompt \u7684\u8f93\u5165\u6587\u672c (opensource/skill \u6682\u4e3a\u9884\u7559\u53c2\u6570\uff0c\u672c\u7248\u672a\u63a5)'
    lines = []
    idx = 1
    for item in top_items[:max_items]:
        lines.append(
            f"[{idx}] [{item.get('category','')}] {item.get('title','')}\n"
            f"    \u6765\u6e90:{item.get('source','')} | \u6253\u5206:{item.get('score','')}/5 | "
            f"{item.get('reason','')[:120]}"
        )
        idx += 1
    for r in (opensource_results or [])[:15]:
        lines.append(
            f"[{idx}] [\u5f00\u6e90\u7cbe\u534e] {r.get('title','')}\n"
            f"    \u521b\u65b0:{r.get('innovation','')[:100]} | \u5438\u6536:{r.get('absorb','')[:100]}"
        )
        idx += 1
    if skill_results_data:
        for entry in skill_results_data.values():
            dname = entry.get("domain", {}).get("name", "")
            for kh in entry.get("knowhows", [])[:2]:
                lines.append(
                    f"[{idx}] [\u6280\u80fd\u60c5\u62a5/{dname}] {kh.get('knowhow','')[:120]}\n"
                    f"    \u7528\u4e8e:{kh.get('apply_to','')[:80]}"
                )
                idx += 1
    return "\n\n".join(lines) if lines else '(\u4eca\u65e5\u65e0\u7cbe\u534e\u4fe1\u53f7)'


def generate_synthesis(top_items: list, use_gateway: bool, models_used: list) -> Optional[dict]:
    '\n    \u591a\u5b66\u79d1\u7efc\u5408\u7814\u5224: \u4e00\u6b21 LLM \u8c03\u7528\uff0c\u628a\u4eca\u5929\u7684\u7cbe\u534e\u4fe1\u53f7\u6309 5 \u7ef4\u5ea6\n    (\u6280\u672f/\u4e2d\u533b\u77e5\u8bc6/\u7ade\u54c1/\u53d8\u73b0/\u98ce\u9669) \u8f93\u51fa"\u53d1\u73b0+\u5bf9\u6211\u4eec\u610f\u5473\u7740\u4ec0\u4e48+\u8981\u4e0d\u8981\u884c\u52a8"\u3002\n    \u67d0\u7ef4\u5ea6\u65e0\u4fe1\u53f7\u5fc5\u987b\u5982\u5b9e\u6807\u6ce8\uff0c\u4e0d\u786c\u7f16\u3002\n    \u8fd4\u56de: {dimension: {"no_signal": bool, "note": str, "items": [...]}, ...} \u6216 None(\u5931\u8d25)\n    '
    items_text = build_synthesis_input(top_items)
    prompt = SYNTHESIS_PROMPT.format(context=SUEAI_CONTEXT, items=items_text)
    messages = [{"role": "user", "content": prompt}]
    model_to_use = models_used[0] if models_used else "glm-4-flash"

    print(f"\n[\u591a\u5b66\u79d1\u7814\u5224] \u8c03 {model_to_use} \u505a\u4e94\u7ef4\u5ea6\u7efc\u5408 ...", flush=True)
    try:
        response = _call_llm_sync(model_to_use, messages, max_tokens=2000, use_gateway=use_gateway)
    except Exception as e:
        print(f"  [\u591a\u5b66\u79d1\u7814\u5224] LLM \u8c03\u7528\u5931\u8d25: {e}")
        return None

    text = response.strip()
    if text.startswith("```"):
        inner = []
        for line in text.split("\n")[1:]:
            if line.strip() == "```":
                break
            inner.append(line)
        text = "\n".join(inner)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                print(f"  [\u591a\u5b66\u79d1\u7814\u5224] JSON \u89e3\u6790\u5931\u8d25: {text[:200]}")
                return None
        else:
            print(f"  [\u591a\u5b66\u79d1\u7814\u5224] \u65e0\u6cd5\u89e3\u6790: {text[:200]}")
            return None

    if not isinstance(data, dict):
        print(f"  [\u591a\u5b66\u79d1\u7814\u5224] \u8fd4\u56de\u975e dict \u7ed3\u6784，\u4e22\u5f03: {str(data)[:200]}")
        return None

    
    for dim in SYNTHESIS_DIMENSIONS:
        if dim not in data or not isinstance(data.get(dim), dict):
            data[dim] = {"no_signal": True, "note": '\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7', "items": []}

    summary_bits = []
    for dim in SYNTHESIS_DIMENSIONS:
        if data[dim].get("no_signal"):
            summary_bits.append(f"{dim}(\u65e0\u4fe1\u53f7)")
        else:
            summary_bits.append(f"{dim}({len(data[dim].get('items', []))}\u6761)")
    print('  [\u591a\u5b66\u79d1\u7814\u5224] \u5b8c\u6210: ' + ", ".join(summary_bits))
    return data


def generate_synthesis_section(synthesis: Optional[dict]) -> str:
    "\u751f\u6210'\u4eca\u65e5\u591a\u5b66\u79d1\u7814\u5224'\u677f\u5757 Markdown (\u65e5\u62a5\u5934\u90e8\uff0c\u4e94\u7ef4\u5ea6: \u6280\u672f/\u4e2d\u533b\u77e5\u8bc6/\u7ade\u54c1/\u53d8\u73b0/\u98ce\u9669)"
    if not synthesis:
        return (
            '## \U0001f9ed \u4eca\u65e5\u591a\u5b66\u79d1\u7814\u5224\n\n'
            '> \u672c\u6b21\u7efc\u5408\u7814\u5224\u672a\u751f\u6210(LLM \u8c03\u7528\u5931\u8d25\u6216\u8df3\u8fc7)\uff0c\u4e94\u7ef4\u5ea6\u5224\u65ad\u6682\u7f3a\uff0c'
            '\u8be6\u89c1\u4e0b\u65b9\u9010\u6761\u7cbe\u534e\u60c5\u62a5\u3002\n\n'
            "---\n"
        )

    DIM_EMOJI = {'\u6280\u672f': "🔧", '\u4e2d\u533b\u77e5\u8bc6': "📖", '\u7ade\u54c1': "🎯", '\u53d8\u73b0': "💰", '\u98ce\u9669': "🛡️"}
    lines = [
        '## \U0001f9ed \u4eca\u65e5\u591a\u5b66\u79d1\u7814\u5224',
        "",
        '> \u603b\u60c5\u62a5\u5b98\u4e94\u7ef4\u5ea6\u7efc\u5408 (\u6280\u672f/\u4e2d\u533b\u77e5\u8bc6/\u7ade\u54c1/\u53d8\u73b0/\u98ce\u9669) | \u65e0\u4fe1\u53f7\u7ef4\u5ea6\u5982\u5b9e\u6807\u6ce8\uff0c\u4e0d\u786c\u7f16',
        "",
    ]
    for dim in SYNTHESIS_DIMENSIONS:
        entry = synthesis.get(dim) or {"no_signal": True, "note": '\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7', "items": []}
        emoji = DIM_EMOJI.get(dim, "📌")
        lines.append(f"### {emoji} {dim}")
        lines.append("")
        items = entry.get("items") or []
        if entry.get("no_signal") or not items:
            note = entry.get("note") or '\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7'
            lines.append(f"_{note}_")
            lines.append("")
            continue
        for it in items:
            lines.append(f"- **\u53d1\u73b0**: {it.get('finding','')}")
            lines.append(f"  - \u5bf9\u6211\u4eec\u610f\u5473\u7740\u4ec0\u4e48: {it.get('meaning','')}")
            lines.append(f"  - \u8981\u4e0d\u8981\u884c\u52a8: {it.get('action','')}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_d1_title_summary(today: str, synthesis: Optional[dict], total_top: int) -> tuple:
    '\u7ed9 D1 intel_reports \u7684 title/summary \u5b57\u6bb5\u6784\u9020\u77ed\u6458\u8981 (\u4eba\u8bdd\uff0c\u4e0d\u662f\u5b8c\u6574\u62a5\u544a)'
    title = f"\u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 · {today}"
    if not synthesis:
        return title, f"\u4eca\u65e5\u7cbe\u534e {total_top} \u6761 (\u591a\u5b66\u79d1\u7814\u5224\u672c\u6b21\u672a\u751f\u6210，\u8be6\u89c1\u6b63\u6587\u9010\u6761\u60c5\u62a5)。"
    bits = []
    for dim in SYNTHESIS_DIMENSIONS:
        entry = synthesis.get(dim) or {}
        items = entry.get("items") or []
        if entry.get("no_signal") or not items:
            bits.append(f"{dim}:\u4eca\u65e5\u65e0\u65b0\u4fe1\u53f7")
        else:
            bits.append(f"{dim}:{items[0].get('finding','')[:40]}")
    return title, " | ".join(bits)


def build_items_text(batch: list, offset: int = 0) -> str:
    lines = []
    for i, p in enumerate(batch):
        title    = p.get("title", "")
        abstract = p.get("abstract", "")[:300]
        source   = p.get("source", "")
        lines.append(f"[{offset + i + 1}] [{source}] {title}\n    {abstract}")
    return "\n\n".join(lines)




async def analyze_batch_async(
    batch: list,
    batch_idx: int,
    offset: int,
    model: str,
    use_gateway: bool,
    loop: asyncio.AbstractEventLoop,
) -> list:
    '\n    \u5f02\u6b65\u5206\u6790\u5355\u6279\uff0c\u5728 executor \u4e2d\u8c03\u540c\u6b65 LLM \u51fd\u6570\u3002\n    \u8fd4\u56de [{index, score, category, reason}, ...]\n    '
    items_text = build_items_text(batch, offset)
    prompt = ANALYZE_PROMPT_TPL.format(
        context=SUEAI_CONTEXT,
        items=items_text,
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        response = await loop.run_in_executor(
            None,
            lambda: _call_llm_sync(model, messages, max_tokens=1500, use_gateway=use_gateway)
        )
    except Exception as e:
        print(f"    [\u6279{batch_idx}|{model}] LLM \u5931\u8d25: {e}", flush=True)
        return []

    
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = []
        for line in lines[1:]:
            if line.strip() == "```":
                break
            inner.append(line)
        text = "\n".join(inner)

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            try:
                picks = json.loads(m.group())
            except json.JSONDecodeError:
                print(f"    [\u6279{batch_idx}|{model}] JSON \u89e3\u6790\u5931\u8d25,\u539f\u59cb: {text[:150]}", flush=True)
                return []
        else:
            return []

    
    SCORE_TEXT_MAP = {
        '\u6781\u9ad8\u4ef7\u503c': 5, '\u975e\u5e38\u9ad8': 5, '\u9ad8\u4ef7\u503c': 4, '\u6709\u4ef7\u503c': 3,
        '\u5f31\u76f8\u5173': 1, '\u4f4e\u76f8\u5173': 1, '\u65e0\u5173': 0, '\u4e0d\u76f8\u5173': 0,
        '\u4e2d\u7b49': 3, '\u8f83\u9ad8': 4, '\u4e00\u822c': 2,
    }
    cleaned = []
    for pick in picks:
        score = pick.get("score", 0)
        if isinstance(score, str):
            
            try:
                score = int(float(score.strip()))
            except (ValueError, TypeError):
                
                score = SCORE_TEXT_MAP.get(score.strip(), 0)
        pick["score"] = max(0, min(5, int(score)))
        pick["_model"] = model
        if pick["score"] > 0:  
            cleaned.append(pick)
    return cleaned


async def analyze_all_parallel(
    all_items: list,
    use_gateway: bool,
    models: list,
) -> list:
    '\n    \u628a all_items \u5206\u6279\uff0c\u7528\u591a\u4e2a\u6a21\u578b\u5e76\u53d1\u5206\u6790\u3002\n    \u8fd4\u56de\u5168\u90e8 pick \u5217\u8868 (\u542b index/score/category/reason/_model)\n    '
    batches = [all_items[i:i+BATCH_SIZE] for i in range(0, len(all_items), BATCH_SIZE)]
    n_batches = len(batches)
    print(f"\n[\u5e76\u884c\u5206\u6790] {len(all_items)} \u6761 -> {n_batches} \u6279 x {BATCH_SIZE} | "
          f"\u6a21\u578b: {models} | \u5e76\u53d1 worker: {MAX_WORKERS}", flush=True)

    loop = asyncio.get_event_loop()
    all_picks = []
    sem = asyncio.Semaphore(MAX_WORKERS)

    async def bounded_analyze(batch, batch_idx, offset, model):
        async with sem:
            picks = await analyze_batch_async(
                batch, batch_idx, offset, model, use_gateway, loop
            )
            hit = len(picks)
            print(f"    [OK] \u6279{batch_idx:03d}/{n_batches} [{model}] "
                  f"-> {hit} \u6761\u547d\u4e2d (\u5171 {len(batch)} \u6761)", flush=True)
            if hit:
                all_picks.extend(picks)
            await asyncio.sleep(BATCH_SLEEP)

    
    tasks = []
    for bi, batch in enumerate(batches):
        model = models[bi % len(models)]
        offset = bi * BATCH_SIZE
        tasks.append(bounded_analyze(batch, bi + 1, offset, model))

    await asyncio.gather(*tasks)
    return all_picks




_STOPWORDS = {"the", "a", "an", "for", "with", "and", "of", "to", "in", "on",
              "api", "llm", "ai", "free", "gateway", "proxy", "openai"}


def _tokens(text):
    words = re.findall(r"[a-z0-9一-鿿]+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _STOPWORDS}


def prefilter_dedup(items, sim_threshold=0.6):
    'Layer2-lite pre-filter (cheap, rule-based, runs BEFORE the LLM analysis pass): \
drops near-duplicate items (Jaccard token overlap on title+abstract) so the LLM \
budget is not spent re-scoring 5+ near-identical "free gateway" repos every day. \
Items are assumed already sorted by relevance/stars (first occurrence wins).'
    kept = []
    kept_token_sets = []
    dropped = 0
    for item in items:
        toks = _tokens(item.get("title", "")) | _tokens(item.get("abstract", "")[:200])
        if not toks:
            kept.append(item)
            kept_token_sets.append(toks)
            continue
        is_dup = False
        for prev_toks in kept_token_sets:
            if not prev_toks:
                continue
            overlap = len(toks & prev_toks) / len(toks | prev_toks)
            if overlap >= sim_threshold:
                is_dup = True
                break
        if is_dup:
            dropped += 1
            continue
        kept.append(item)
        kept_token_sets.append(toks)
    if dropped:
        print(f"  [Layer2 prefilter] dropped {dropped} near-duplicate items "
              f"({len(items)} -> {len(kept)})", flush=True)
    return kept


def merge_picks(all_items: list, raw_picks: list, top_n: int = 50,
               min_score: int = 2) -> list:
    '\n    \u628a raw_picks \u6620\u5c04\u56de all_items\uff0c\u53bb\u91cd\uff0c\u6309\u5206\u6570\u6392\u5e8f\uff0c\u53d6 TOP N\u3002\n    min_score: \u6700\u4f4e\u5165\u9009\u5206 (\u9ed8\u8ba4 2\uff0c\u8fc7\u6ee4 modelscope \u8fc7\u5bbd\u677e\u7684\u5168\u91cf\u547d\u4e2d)\n    '
    
    idx_map: dict[int, dict] = {}
    for pick in raw_picks:
        idx = pick.get("index", 0)
        
        try:
            idx = int(str(idx).strip().strip('"').strip("'"))
        except (ValueError, TypeError):
            continue
        if idx <= 0 or idx > len(all_items):
            continue
        score = pick.get("score", 1)
        if score < min_score:
            continue  
        if idx not in idx_map or score > idx_map[idx].get("score", 0):
            idx_map[idx] = pick

    results = []
    for idx, pick in idx_map.items():
        item = all_items[idx - 1]
        results.append({
            "title":    item.get("title", ""),
            "url":      item.get("url", ""),
            "source":   item.get("source", ""),
            "abstract": item.get("abstract", "")[:200],
            "score":    pick.get("score", 1),
            "category": pick.get("category", '\u672a\u5206\u7c7b'),
            "reason":   pick.get("reason", ""),
            "_model":   pick.get("_model", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]




VERIFY_PROMPT = """You are a skeptical fact-checker (查证官) for SueAI's intel radar.
Your job is to challenge claimed relevance, not rubber-stamp it - many flagged items
carry generic boilerplate reasons that would apply equally to hundreds of unrelated repos.

{context}

Flagged signal:
Title: {title}
Category: {category}
Claimed reason: {reason}
Abstract: {abstract}

Does the claimed reason name a SPECIFIC, concrete connection to one of SueAI's actual
components listed above (a real module/capability/gap it would plug into) - not just a
generic "related to RAG/AI" statement that could describe almost any repo in this space?

Reply with strict JSON only, no extra text:
{{"verified": true or false, "note": "<one short reason in Chinese, <=40 chars>"}}
"""


def verify_top_items(top_items, use_gateway, models, verify_n=15):
    'Layer3-lite ("查证官"/verification officer, MDAgents-style adversarial \
check): re-challenges the TOP N highest-scored items claimed relevance with a fresh, \
skeptically-framed LLM call, so generic boilerplate reasons (e.g. the same "SueAI RAG \
xiangguan" sentence copy-pasted across unrelated repos) get flagged instead of silently \
passing through as if the first-pass score alone proved real relevance.'
    model = models[0] if models else "glm-4-flash"
    checked = passed = 0
    for item in top_items[:verify_n]:
        prompt = VERIFY_PROMPT.format(
            context=SUEAI_CONTEXT,
            title=item.get("title", ""),
            category=item.get("category", ""),
            reason=item.get("reason", ""),
            abstract=item.get("abstract", "")[:150],
        )
        try:
            resp = _call_llm_sync(model, [{"role": "user", "content": prompt}],
                                   max_tokens=150, use_gateway=use_gateway)
            text = resp.strip()
            if text.startswith("```"):
                lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            item["verified"] = bool(data.get("verified", False))
            item["verify_note"] = str(data.get("note", ""))[:60]
            checked += 1
            if item["verified"]:
                passed += 1
        except Exception:
            item["verified"] = None
            item["verify_note"] = ""
    print(f"  [Layer3 verify] {passed}/{checked} passed skeptical check "
          f"({checked}/{min(verify_n, len(top_items))} attempted)", flush=True)
    return top_items


def flag_action_worthy_items(top_items, min_score=4):
    """给通过Layer3核验且分值达标的高置信度条目打上"建议立即验证"标记,让日报能直接指出哪几条值得当场派agent真测,不用每次口头一条条派。判据:score>=min_score(默认4星)且verified==True(通过怀疑式核验)。这一步只做客观阈值判断,不做"是否已在军火库台账里"这类跨云端-本地的比对(军火库总台账.md在本地F盘,这个脚本跑在云端GitHub Actions,两边没有同步机制,如实标注这个边界,不假装能做到)。"""
    flagged = []
    for item in top_items:
        is_flagged = (item.get("score", 0) >= min_score and item.get("verified") is True)
        item["action_flag"] = is_flagged
        if is_flagged:
            flagged.append(item)
    print(f"  [自动分诊] {len(flagged)} 条达到\"建议立即验证\"阈值(score>={min_score}+已核验)", flush=True)
    return top_items, flagged


def generate_action_flags_section(flagged_items: list) -> str:
    """生成"今日建议立即验证"板块,把自动分诊挑出的高置信度条目单独列在最前面,不用翻遍全部TOP15才能找到该行动的那几条。这不是自动派agent(还没有这个基础设施),只是把"该测哪条"这个判断做实,缩短从"鹰眼发现"到"决定要不要测"之间的人工来回。"""
    if not flagged_items:
        return (
            '## \U0001f3af 今日建议立即验证\n\n'
            '> 本轮无条目同时满足"score>=4且通过Layer3核验"这个阈值,不代表今天没有价值的发现,'
            '仅代表没有条目达到"高置信度+低风险"的自动分诊标准,详见下方精华情报逐条判断。\n\n'
            "---\n"
        )
    lines = [
        '## \U0001f3af 今日建议立即验证',
        "",
        f'> 以下 {len(flagged_items)} 条同时满足 score>=4 星 且 通过Layer3怀疑式核验,'
        '是本轮自动分诊挑出的高置信度信号,建议优先派agent真实测试/验证。'
        '(注:这一步不比对本地军火库总台账.md是否已收录,本地台账和这个云端脚本目前没有同步机制,'
        '如实标注,不假装做了这层判断)',
        "",
    ]
    for item in flagged_items:
        title = item.get("title", "")
        url = item.get("url", "")
        note = item.get("verify_note", "")
        if url:
            lines.append(f"- **[{title}]({url})** — {item.get('reason','')} (核验备注: {note})")
        else:
            lines.append(f"- **{title}** — {item.get('reason','')} (核验备注: {note})")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def score_blindspot(cands: list, use_gateway: bool, models: list) -> list:
    """复用主打分链 (analyze_all_parallel + glm-4-flash) 给补盲区候选打价值分。
    左连接: 保留全部候选 (哪怕 LLM 判为低相关也照列, 因为「这类高星项目存在」本身
    就是要被看见的盲区信号), 有分的补上 score/category/reason。返回按 (score, stars) 排序。"""
    if not cands:
        return []
    if not models or not (use_gateway or ZHIPU_KEY or NVIDIA_KEY):
        print("  [补盲区打分] 无可用 LLM, 仅列原始候选 (不打分)", flush=True)
        for c in cands:
            c["score"], c["category"], c["reason"] = 0, "未判定", ""
        return sorted(cands, key=lambda x: -x.get("stars", 0))
    print(f"\n[补盲区打分] {len(cands)} 个候选走 glm-4-flash 价值判断 ...", flush=True)
    try:
        picks = asyncio.run(analyze_all_parallel(cands, use_gateway, models))
    except Exception as e:
        print(f"  [补盲区打分] 打分失败 (不阻断): {e}", flush=True)
        picks = []
    score_map = {}
    for p in picks:
        try:
            idx = int(str(p.get("index", 0)).strip().strip('"').strip("'"))
        except (ValueError, TypeError):
            continue
        if 1 <= idx <= len(cands):
            if idx not in score_map or p.get("score", 0) > score_map[idx].get("score", 0):
                score_map[idx] = p
    for i, c in enumerate(cands, 1):
        p = score_map.get(i)
        c["score"]    = p.get("score", 0) if p else 0
        c["category"] = p.get("category", "未判定") if p else "未判定"
        c["reason"]   = p.get("reason", "") if p else ""
    return sorted(cands, key=lambda x: (-x.get("score", 0), -x.get("stars", 0)))


def generate_blindspot_section(scored: list, max_show: int = 15) -> str:
    """生成「🆕补盲区新发现」板块 (video/tts/ocr/kg/tcm 整类)。这是把「人肉发现 OpenCut」
    升级成「cron 自动扫外部 AI 世界」的落地体现。已按 committed 的 arsenal_repos.txt
    (台账 ASCII 快照) 在抓取阶段去重: 短名命中的已收录项被剔除, 不再重复标为新发现;
    但该快照是人工同步、非与本地台账实时联动, 快照外的新增项仍需人工对照。"""
    if not scored:
        return (
            "## 🆕 补盲区新发现\n\n"
            "> 本轮补盲区雷达未捞到候选 (video/tts/ocr/kg/tcm 整类, stars> 门槛 + 近期活跃)。\n\n"
            "---\n"
        )
    by_cluster = {}
    for c in scored:
        by_cluster.setdefault(c.get("_cluster", "其它"), []).append(c)
    lines = [
        "## 🆕 补盲区新发现 (video / tts / ocr / kg / tcm 整类)",
        "",
        "> 主扫描的 topic 写死在 ai-agent/llm/rag 类, 漏掉了 video/剪辑/tts/ocr 整类 "
        "(今晚 OpenCut 盲区的根因)。本雷达按 topic 簇专捞这些方向的高星 (stars> 门槛) + 活跃项目, "
        "并用免费 glm-4-flash 判它对我们哪个子系统 (OCR/RAG/视频/判断/KG) 有价值。",
        "> **边界 (如实标注)**: 已按 committed 的 arsenal_repos.txt (台账 ASCII 快照) "
        "在抓取阶段去重 —— 短名命中的已收录项被剔除, 不再重复展示; 但该快照是人工同步、"
        "非与本地台账实时联动, 所以下面列的都是快照外的高星活跃候选 + 价值判断, 是否已入库仍请人工对照。",
        "",
    ]
    for cluster, items in by_cluster.items():
        lines.append(f"### {cluster} ({len(items)})")
        lines.append("")
        for c in items[:max_show]:
            stars = c.get("stars", 0)
            sc = c.get("score", 0)
            val = f"价值{sc}/5·{c.get('category','')}" if sc else "价值未判定/低相关"
            reason = f" — {c.get('reason','')}" if c.get("reason") else ""
            url = c.get("url", "")
            title = c.get("title", "")
            head = f"[{title}]({url})" if url else title
            lines.append(f"- **{head}** ⭐{stars} · {val}{reason}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


CATEGORY_EMOJI = {
    "RAG":       "🔍",
    '\u5224\u65ad\u5f15\u64ce':  "🧠",
    'OCR\u6587\u5b57\u5316': "📄",
    '\u4e2d\u533bNLP':   "🏥",
    '\u65b9\u6cd5\u524d\u6cbf':  "🚀",
    '\u7ade\u54c1\u60c5\u62a5':  "👀",
    '\u514d\u8d39\u8d44\u6e90':  "🎁",
    '\u672a\u5206\u7c7b':    "📌",
}

def _decision_advice(x: dict) -> str:
    """按类别/核验状态给一句话行动建议(确定性规则, 不调 LLM, 不烧算力)。"""
    cat = x.get("category", "") or ""
    if x.get("verified") is True and int(x.get("score", 0) or 0) >= 4:
        return "高分且已通过核验 —— 今日就派 agent 沙箱实测, 通过即纳入军火库。"
    if "竞品" in cat:
        return "竞品动向 —— 今日扫一眼对方在做什么, 判断要不要跟进防守。"
    if "免费" in cat:
        return "免费资源 —— 今日评估能否接入免费网关/舰队, 省掉付费或本地算力。"
    if x.get("_kind") == "blindspot":
        return "补盲区高星项 —— 今日人工对照军火库是否已收录, 未收录则评估补入。"
    if cat in ("OCR文字化", "RAG"):
        return "命中核心管线(OCR/RAG) —— 今日评估是否值得进一步沙箱验证再决定纳入。"
    return "今日快速评估与 SueAI 的契合度, 决定纳入试用还是放弃。"


def _tcm_relevance_tier(x: dict) -> int:
    """确定性「中医古籍平台相关性」分层(不调 LLM, 不烧算力): 让 TOP3 决策优先中医/古籍/OCR/
    RAG/知识图谱等直接相关项, 把纯通用 AI 硬件(超算/AI服务器/显卡)与无关行业(钻井/军工/自动驾驶/
    金融量化)项降权。+1=直接相关, 0=中性, -1=纯硬件/无关。
    治「TOP3 被 AI 大类高分(阿里云超节点/英伟达AI服务器)霸榜、与中医平台关联弱」。"""
    cat  = (x.get("category") or "")
    blob = (str(x.get("title", "")) + " " + str(x.get("reason", ""))).lower()
    # 命中核心管线分类 = 直接相关
    if cat in ("中医NLP", "OCR文字化", "RAG", "判断引擎"):
        return 1
    TCM_CJK = ("中医", "古籍", "古文", "文言", "方剂", "医案", "本草", "针灸", "辨证", "中药",
               "经方", "知识图谱", "溯源", "导读", "数字化", "古典", "中医药", "向量")
    TCM_ASCII = (r"\b(tcm|ocr|rag|retrieval|knowledge graph|graphrag|embedding|"
                 r"classical chinese|ancient chinese|chinese medicine)\b")
    if any(k in blob for k in TCM_CJK) or re.search(TCM_ASCII, blob):
        return 1
    IRR_CJK = ("超算", "超节点", "芯片", "算力", "英伟达", "钻井", "军工", "自动驾驶", "量化交易",
               "游戏", "广告", "挖矿", "无人机", "数据中心", "显卡", "服务器集群")
    IRR_ASCII = (r"\b(gpu|nvidia|chip|cluster|drone|gaming|data ?center|"
                 r"autonomous driving|mining rig)\b")
    if any(k in blob for k in IRR_CJK) or re.search(IRR_ASCII, blob):
        return -1
    return 0


def generate_top3_decision_section(top_items: list,
                                   scored_blindspot: Optional[list] = None) -> str:
    """置顶「今日 TOP3 决策就绪」板块: 从当日全部候选(主扫 top_items + 补盲区 scored_blindspot)
    里, 按 (分值, 已核验, 采用广度=星数) 综合排序, 挑最该创始人当场拍板的 1-3 条, 每条一句话建议。
    治「产出堆 Issue 没人看」: 一眼看到该决策的那几条, 而不是扫完全部 TOP15+补盲区才找得到。
    纯确定性打包已有字段(score/verified/stars/reason), 不额外调 LLM。"""
    pool = []
    for it in (top_items or []):
        pool.append({
            "title": it.get("title", ""), "url": it.get("url", ""),
            "score": int(it.get("score", 0) or 0),
            "verified": it.get("verified"),
            "stars": int(it.get("stars", 0) or 0),
            "category": it.get("category", "未分类"),
            "reason": it.get("reason", ""),
            "source": it.get("source", "主扫描"),
            "_kind": "main",
        })
    for c in (scored_blindspot or []):
        pool.append({
            "title": c.get("title", ""), "url": c.get("url", ""),
            "score": int(c.get("score", 0) or 0),
            "verified": None,
            "stars": int(c.get("stars", 0) or 0),
            "category": c.get("category", "未判定"),
            "reason": c.get("reason", ""),
            "source": c.get("source", "🆕补盲区雷达"),
            "_kind": "blindspot",
        })

    # 综合"紧迫度": ①中医古籍平台相关性优先(纯硬件/无关行业降到相关项之后) ②分值 ③同分已核验优先
    #             ④再按星数(采用广度大=更成熟可决策)。相关性置顶治「TOP3 被 AI 大类高分霸榜」。
    pool.sort(key=lambda x: (_tcm_relevance_tier(x), x["score"],
                             1 if x["verified"] is True else 0, x["stars"]),
              reverse=True)

    MIN_DECISION_SCORE = 3
    picks = [x for x in pool if x["score"] >= MIN_DECISION_SCORE][:3]
    total = len(pool)

    if not picks:
        return (
            "## 🎯 今日 TOP3 决策就绪\n\n"
            f"> 今日 {total} 条候选中, 无分值 ≥{MIN_DECISION_SCORE} 的高置信决策项。"
            "不代表没有价值发现, 仅表示今日没有达到「该立即拍板」阈值的信号, 可照常浏览下方明细。\n\n"
            "---\n"
        )

    lines = [
        "## 🎯 今日 TOP3 决策就绪",
        "",
        f"> 从今日全部 {total} 条候选里, 按「中医古籍平台相关性 × 分值 × 是否核验 × 采用广度」"
        f"排出最该你拍板的 {len(picks)} 条(纯通用AI硬件/无关行业已降权), 附一句话建议。"
        f"**先看这里、其余按需翻。**",
        "",
    ]
    for i, x in enumerate(picks, 1):
        stars_m = "⭐" * max(1, x["score"])
        head = f"[{x['title']}]({x['url']})" if x["url"] else x["title"]
        vmark = ""
        if x["verified"] is True:
            vmark = " · ✅已核验"
        elif x["verified"] is False:
            vmark = " · ⚠️待核验"
        gh_stars = f" · GitHub⭐{x['stars']}" if x["stars"] else ""
        lines.append(f"### {i}. {head}")
        lines.append(f"- {stars_m} 分值{x['score']}/5 · [{x['category']}]{vmark}{gh_stars} · 来源:{x['source']}")
        if x["reason"]:
            lines.append(f"- 为什么值得决策: {x['reason']}")
        lines.append(f"- 👉 **建议**: {_decision_advice(x)}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ── 内视眼:系统自省探针 ─────────────────────────────────────────────
# 鹰眼从"只向外看AI新闻的死眼"进化为"能感知本平台自身缺口的活体"的关键一环:
# 探测本平台各器官(寻脉/星图/导读/转化)真实健康度 → 生成"系统自身缺口"板块置顶日报。
# 复用 push_d1_intel_report 的 CF_ACCOUNT_ID/D1_API_TOKEN/D1_DATABASE_ID(零新增 secret)。
# 全程软失败:任一子探测异常只记"查询失败",绝不阻断主日报。

def _selfcheck_d1(sql: str):
    account_id  = os.environ.get("CF_ACCOUNT_ID", "").strip()
    api_token   = os.environ.get("D1_API_TOKEN", "").strip()
    database_id = os.environ.get("D1_DATABASE_ID", "").strip()
    if not (account_id and api_token and database_id):
        raise RuntimeError("D1 env 三件套缺失")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/d1/database/{database_id}/query")
    body = json.dumps({"sql": sql}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + api_token, "Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if not d.get("success"):
        raise RuntimeError(str(d.get("errors"))[:100])
    return d["result"][0]["results"]

def _selfcheck_scalar(sql: str):
    r = _selfcheck_d1(sql)
    return list(r[0].values())[0] if r else 0

def generate_selfcheck_section() -> str:
    rows = []   # [(器官, 实测值, is_gap)]
    # 👄 寻脉:从真实调用日志 sue_call_logs 看健康度(D1 内部查询·云端可靠;
    #    不从云端 runner POST 外部 API—后者受超时/无 cookie 限制会假 0)。反映真实用户实际拿到的证据召回。
    try:
        r = _selfcheck_d1(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN public_evidence_count>0 THEN 1 ELSE 0 END) with_ev, "
            "SUM(CASE WHEN output_status='success' THEN 1 ELSE 0 END) ok "
            "FROM sue_call_logs WHERE created_at >= strftime('%s','now','-7 days')")
        row = (r[0] if r else {}) or {}
        total   = row.get("total", 0) or 0
        with_ev = row.get("with_ev", 0) or 0
        ok      = row.get("ok", 0) or 0
        if total == 0:
            rows.append(("👄 寻脉·近7天调用量", "0(近期无人调用)", True))
        else:
            pct = with_ev * 100 // total
            rows.append(("👄 寻脉·近7天证据召回率", f"{with_ev}/{total}({pct}%)", pct < 60))
            rows.append(("👄 寻脉·近7天成功率", f"{ok}/{total}", ok < total * 0.7))
    except Exception as e:
        rows.append(("👄 寻脉·调用日志", f"查询失败({str(e)[:36]})", False))
    # 🧠 星图 / 🖼️ 导读 / 💰 转化(各自软失败,一个错不拖累其他)
    try:
        n = _selfcheck_scalar("SELECT COUNT(*) c FROM sue_graph_nodes")
        # 总数不代表质量(大批量导入含大量贴牌),只显示不判健康;真缺口看下面"已提升真节点"
        rows.append(("🧠 星图·节点总数(含贴牌)", str(n), False))
    except Exception as e:
        rows.append(("🧠 星图·节点总数(含贴牌)", f"查询失败({str(e)[:36]})", False))
    try:
        n = _selfcheck_scalar("SELECT COUNT(*) c FROM sue_graph_candidates WHERE review_status='approved'")
        # 审核通过提升的才是真节点,少=真缺口(贴牌多、原文直证真节点少)
        rows.append(("🧠 星图·已提升真节点(原文直证)", str(n), isinstance(n, int) and n < 300))
    except Exception as e:
        rows.append(("🧠 星图·已提升真节点(原文直证)", f"查询失败({str(e)[:36]})", False))
    try:
        # 真积压 = 未判(stage1) + LLM判过待人工终审;LLM已判拒的不算积压(留库仅作审计,曾虚高365)
        n = _selfcheck_scalar("SELECT COUNT(*) c FROM sue_graph_candidates WHERE review_status='pending' "
                              "AND (llm_verdict IS NULL OR llm_verdict='accept')")
        rows.append(("🧠 星图·待审候选(真积压)", str(n), isinstance(n, int) and n > 50))
    except Exception as e:
        rows.append(("🧠 星图·待审候选(真积压)", f"查询失败({str(e)[:36]})", False))
    try:
        n = _selfcheck_scalar("SELECT COUNT(DISTINCT book_id) c FROM book_daodu_ai WHERE status='visible'")
        rows.append(("🖼️ 导读·已生成本数", str(n), isinstance(n, int) and n < 200))
    except Exception as e:
        rows.append(("🖼️ 导读·已生成本数", f"查询失败({str(e)[:36]})", False))
    try:
        r = _selfcheck_d1("SELECT event_name k, COUNT(*) c FROM events GROUP BY event_name ORDER BY c DESC LIMIT 12")
        funnel = ", ".join(f"{x['k']}={x['c']}" for x in r) or "无埋点数据"
        rows.append(("💰 转化·漏斗埋点", funnel, "register" not in funnel))
    except Exception as e:
        rows.append(("💰 转化·漏斗埋点", f"查询失败({str(e)[:36]})", False))
    # 渲染置顶板块
    gaps = [x for x in rows if x[2]]
    lines = [
        "## 🔍 内视:系统自身缺口(自省 · 活体自进化)",
        "",
        f"> 鹰眼向内看本平台各器官真实健康度 —— 判定缺口 **{len(gaps)}** 个。"
        f"这是该优先补的自身短板,比向外看的新技术更该先动手。",
        "",
        "| 器官 | 实测值 | 判定 |",
        "|------|--------|------|",
    ]
    for label, val, is_gap in rows:
        lines.append(f"| {label} | {val} | {'⚠️ 缺口' if is_gap else '✅ 健康'} |")
    lines += ["", "---", ""]
    return "\n".join(lines)


def generate_report_v3(
    date_str: str,
    raw_counts: dict,
    top_items: list,
    elapsed: float,
    models_used: list,
    gateway_alive: bool,
    total_raw: int,
    total_analyzed: int,
    synthesis_md: Optional[str] = None,
    action_flags_md: Optional[str] = None,
    blindspot_md: Optional[str] = None,
    top3_md: Optional[str] = None,
    selfcheck_md: Optional[str] = None,
) -> str:
    '\u751f\u6210 Markdown \u62a5\u544a (\u5934\u90e8\u591a\u5b66\u79d1\u7814\u5224 + \u7cbe\u534e\u60c5\u62a5)'
    total_top = len(top_items)
    rate_str  = f"{total_top/total_analyzed*100:.1f}%" if total_analyzed else "N/A"

    
    by_cat: dict[str, list] = {}
    for item in top_items:
        cat = item.get("category", '\u672a\u5206\u7c7b')
        by_cat.setdefault(cat, []).append(item)

    lines = [
        f"# \u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 v3 · {date_str}",
        "",
        f"> \u81ea\u52a8\u751f\u6210 | \u6d77\u91cf\u6293\u53d6: **{total_raw} \u6761** | AI \u5206\u6790: {total_analyzed} \u6761 | "
        f"\u7cbe\u534e: **{total_top} \u6761** | \u6838\u52a8\u529b\u6c60: {'✅ 在线' if gateway_alive else '❌ 离线(备用)'}",
        f"> \u5206\u6790\u6a21\u578b: {', '.join(models_used)}",
        "",
        "---",
        "",
    ]

    # 置顶最前:内视 —— 系统自身缺口(活体自省,比向外看的 TOP3 更该先看)
    if selfcheck_md:
        lines.append(selfcheck_md)

    # 置顶: 今日 TOP3 决策就绪 (治「产出堆着没人看」, 一眼看该拍板的)
    if top3_md:
        lines.append(top3_md)

    if synthesis_md:
        lines.append(synthesis_md)

    if action_flags_md:
        lines.append(action_flags_md)

    if blindspot_md:
        lines.append(blindspot_md)

    lines += [
        '## \u7cbe\u534e\u60c5\u62a5 (\u6309\u5206\u503c\u6392\u5e8f)',
        "",
    ]

    for cat, items in sorted(by_cat.items(), key=lambda x: -max(i["score"] for i in x[1])):
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        lines.append(f"### {emoji} {cat} ({len(items)} \u6761)")
        lines.append("")
        for item in sorted(items, key=lambda x: -x["score"]):
            score = item["score"]
            stars = "⭐" * score
            title = item["title"]
            url   = item["url"]
            if url:
                lines.append(f"**[{title}]({url})**")
            else:
                lines.append(f"**{title}**")
            lines.append(f"- \u5206\u503c: {stars} ({score}/5) | \u6765\u6e90: {item['source']}")
            lines.append(f"- \u4ef7\u503c: {item['reason']}")
            lines.append("")
        lines.append("")

    
    lines += [
        "---",
        "",
        "## KPI",
        "",
        '| \u6307\u6807 | \u503c |',
        "|------|-----|",
    ]
    for src, cnt in sorted(raw_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| \u6293\u53d6: {src} | {cnt} \u6761 |")
    lines += [
        f"| \u539f\u59cb\u603b\u91cf | **{total_raw} \u6761** |",
        f"| \u5b9e\u9645\u5206\u6790 | {total_analyzed} \u6761 |",
        f"| \u7b5b\u51fa\u7cbe\u534e | **{total_top} \u6761** |",
        f"| \u7cbe\u534e\u7387 | {rate_str} |",
        f"| \u6838\u52a8\u529b\u6c60 | {'在线' if gateway_alive else '离线(备用)'} |",
        f"| \u5206\u6790\u6a21\u578b | {', '.join(models_used)} |",
        f"| \u8017\u65f6 | {elapsed:.1f} \u79d2 |",
        "",
    ]

    
    lines += [
        '## \u5206\u7c7b\u7edf\u8ba1',
        "",
        '| \u5206\u7c7b | \u6761\u6570 |',
        "|------|------|",
    ]
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        lines.append(f"| {CATEGORY_EMOJI.get(cat,'📌')} {cat} | {len(items)} |")

    return "\n".join(lines)




def main():
    parser = argparse.ArgumentParser(description='\u60c5\u62a5\u96f7\u8fbe v3 \u2014 \u6d77\u91cf\u6293\u53d6+\u591a\u6a21\u578b\u5e76\u884c\u7b5b\u7cbe\u534e')
    parser.add_argument("--cloud", action="store_true",
                        help='\u4e91\u7aef\u6a21\u5f0f: \u8df3\u8fc7\u672c\u5730\u7f51\u5173,\u7528 ZHIPU/NVIDIA env key')
    parser.add_argument("--no-github", action="store_true", help='\u8df3\u8fc7 GitHub \u6293\u53d6(\u7701\u901f\u7387)')
    parser.add_argument("--no-hf-models", action="store_true", help='\u8df3\u8fc7 HF trending models')
    parser.add_argument("--dry-run", action="store_true",
                        help='\u53ea\u6293\u53d6\u4e0d\u5206\u6790(\u6d4b\u8bd5\u6293\u53d6\u91cf)')
    parser.add_argument("--top", type=int, default=50, help='\u7cbe\u534e TOP N (\u9ed8\u8ba4 50)')
    args = parser.parse_args()

    t0    = time.time()
    today = datetime.date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*64}")
    print(f"\u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 v3 (\u6d77\u91cf\u6293\u53d6 + \u591a\u6a21\u578b\u5e76\u884c) · {today}")
    print(f"{'='*64}\n")

    
    gateway_alive = False
    use_gateway   = False
    models_used   = []

    if not args.cloud:
        print('[\u68c0\u67e5] \u6838\u52a8\u529b\u6c60 localhost:4000 ...', end=" ", flush=True)
        gateway_alive = check_gateway_alive()
        if gateway_alive:
            print('OK (\u5728\u7ebf)')
            use_gateway = True
            models_used = GATEWAY_MODELS
        else:
            print('\u79bb\u7ebf')

    if not use_gateway:
        
        if ZHIPU_KEY:
            models_used = ["glm-4-flash (zhipu cloud)"]
            print(f"[\u6a21\u578b] \u4f7f\u7528\u4e91\u7aef {models_used[0]}")
        elif NVIDIA_KEY:
            models_used = ["deepseek-v4-flash (nvidia)"]
            print(f"[\u6a21\u578b] \u4f7f\u7528\u4e91\u7aef {models_used[0]}")
        else:
            print('[\u8b66\u544a] \u65e0\u53ef\u7528 LLM (\u7f51\u5173\u79bb\u7ebf + \u65e0\u4e91\u7aef key),\u4ec5\u8f93\u51fa\u539f\u59cb\u5217\u8868')
            models_used = ['(\u65e0\u5206\u6790)']

    
    print('\n[=== \u6d77\u91cf\u6293\u53d6\u9636\u6bb5 ===]')
    raw_counts: dict[str, int] = {}

    arxiv_papers  = fetch_arxiv_all()
    raw_counts["arXiv"] = len(arxiv_papers)
    time.sleep(1)

    hf_papers     = fetch_hf_papers()
    raw_counts["HF Daily"] = len(hf_papers)

    if not args.no_hf_models:
        hf_models = fetch_hf_trending_models()
        raw_counts["HF Trending Models"] = len(hf_models)
    else:
        hf_models = []

    pubmed_papers = fetch_pubmed()
    raw_counts["PubMed"] = len(pubmed_papers)

    github_repos  = []
    freebies      = []
    blindspot_cands = []
    if not args.no_github:
        github_repos = fetch_github_trending()
        raw_counts["GitHub Trending"] = len(github_repos)
        freebies = fetch_github_freebies()
        raw_counts['GitHub \u514d\u8d39\u519b\u706b'] = len(freebies)
        try:
            blindspot_cands = fetch_blindspot_radar()
        except Exception as e:
            print(f"  [\u8865\u76f2\u533a\u96f7\u8fbe] \u6293\u53d6\u672a\u9884\u671f\u5f02\u5e38 (\u5df2\u6355\u83b7,\u4e0d\u963b\u65ad): {e}", flush=True)
        raw_counts["\u8865\u76f2\u533a\u96f7\u8fbe"] = len(blindspot_cands)

    
    cn_items = fetch_cn_intel()
    raw_counts['\u4e2d\u6587\u60c5\u62a5'] = len(cn_items)

    hn_items = fetch_hn_intel()
    raw_counts["Hacker News"] = len(hn_items)

    all_items = arxiv_papers + hf_papers + hf_models + pubmed_papers + github_repos + freebies + cn_items + hn_items
    total_raw = len(all_items)

    print(f"\n[\u6293\u53d6\u6c47\u603b] \u603b\u8ba1: {total_raw} \u6761")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt}")

    if not all_items:
        print('[\u9519\u8bef] \u6240\u6709\u60c5\u62a5\u6e90\u5747\u6293\u53d6\u5931\u8d25,\u9000\u51fa')
        sys.exit(1)

    if args.dry_run:
        print('\n[--dry-run] \u8df3\u8fc7 AI \u5206\u6790,\u4ec5\u8f93\u51fa\u6293\u53d6 KPI')
        elapsed = time.time() - t0
        print(f"\n── \u6293\u53d6 KPI (dry-run) ──")
        print(f"  \u603b\u6293\u53d6: {total_raw} \u6761")
        print(f"  \u8017\u65f6: {elapsed:.1f}s")
        return

    
    print('\n[=== \u591a\u6a21\u578b\u5e76\u884c\u5206\u6790\u9636\u6bb5 ===]')

    
    hf_models_sample = hf_models[:50] if hf_models else []

    freebies = prefilter_dedup(freebies)

    analyze_items = (arxiv_papers + hf_papers + pubmed_papers + github_repos
                     + hf_models_sample + freebies + cn_items + hn_items)
    analyze_items = prefilter_dedup(analyze_items)
    total_analyzed = len(analyze_items)
    print(f"  \u5b9e\u9645\u5206\u6790: {total_analyzed} \u6761 (HF Models \u622a\u53d6\u524d 50)")

    if use_gateway or (ZHIPU_KEY or NVIDIA_KEY):
        
        if use_gateway:
            active_models = GATEWAY_MODELS
        elif ZHIPU_KEY:
            active_models = [ZHIPU_MODEL]   
        else:
            active_models = [NVIDIA_MODEL]  

        raw_picks = asyncio.run(
            analyze_all_parallel(analyze_items, use_gateway, active_models)
        )
        print(f"\n[\u5206\u6790\u5b8c\u6210] \u603b\u547d\u4e2d pick: {len(raw_picks)} \u6761")
    else:
        print('[\u5206\u6790] \u65e0 LLM \u53ef\u7528,\u8df3\u8fc7\u7b5b\u9009,\u5217\u51fa\u5168\u90e8')
        raw_picks = [
            {"index": i+1, "score": 1, "category": '\u672a\u5206\u7c7b', "reason": '\u672a\u5206\u6790', "_model": '\u65e0'}
            for i in range(min(len(analyze_items), args.top))
        ]

    
    top_items = merge_picks(analyze_items, raw_picks, top_n=args.top)
    print(f"[\u7cbe\u534e] \u7b5b\u51fa TOP {len(top_items)} \u6761 (score>=1)")


    synthesis_data: Optional[dict] = None
    flagged_items: list = []
    if use_gateway or ZHIPU_KEY or NVIDIA_KEY:
        active_models_syn = (GATEWAY_MODELS if use_gateway
                             else ([ZHIPU_MODEL] if ZHIPU_KEY else [NVIDIA_MODEL]))
        top_items = verify_top_items(top_items, use_gateway, active_models_syn, verify_n=15)
        top_items, flagged_items = flag_action_worthy_items(top_items)
        synthesis_data = generate_synthesis(top_items, use_gateway, active_models_syn)
    else:
        print('\n[\u591a\u5b66\u79d1\u7814\u5224] \u65e0 LLM \u53ef\u7528\uff0c\u8df3\u8fc7')


    # 补盲区雷达: 复用打分链给候选打价值分 + 生成「🆕补盲区新发现」板块
    # (隔离于主管线, 任何异常都不阻断主日报的生成与推送)
    blindspot_md = ""
    scored_blindspot: list = []
    try:
        if use_gateway:
            bs_models = GATEWAY_MODELS
        elif ZHIPU_KEY:
            bs_models = [ZHIPU_MODEL]
        elif NVIDIA_KEY:
            bs_models = [NVIDIA_MODEL]
        else:
            bs_models = []
        scored_blindspot = score_blindspot(blindspot_cands, use_gateway, bs_models)
        blindspot_md = generate_blindspot_section(scored_blindspot)
    except Exception as e:
        print(f"  [补盲区雷达] 打分/板块生成异常 (已捕获,不阻断): {e}", flush=True)

    # 置顶「今日 TOP3 决策就绪」: 从主扫 + 补盲区全部候选里挑最该拍板的 1-3 条
    # (隔离于主管线, 任何异常都不阻断主日报的生成与推送)
    top3_md = ""
    try:
        top3_md = generate_top3_decision_section(top_items, scored_blindspot)
    except Exception as e:
        print(f"  [TOP3决策板块] 生成异常 (已捕获,不阻断): {e}", flush=True)

    # 内视眼:探测本平台各器官真实健康度(隔离软失败,绝不阻断主日报)
    selfcheck_md = ""
    try:
        selfcheck_md = generate_selfcheck_section()
    except Exception as e:
        print(f"  [内视眼] 系统自省异常 (已捕获,不阻断): {e}", flush=True)

    elapsed   = time.time() - t0
    synthesis_md = generate_synthesis_section(synthesis_data)
    action_flags_md = generate_action_flags_section(flagged_items)
    report_md = generate_report_v3(
        today, raw_counts, top_items, elapsed, models_used,
        gateway_alive, total_raw, total_analyzed,
        synthesis_md=synthesis_md,
        action_flags_md=action_flags_md,
        blindspot_md=blindspot_md,
        top3_md=top3_md,
        selfcheck_md=selfcheck_md,
    )

    # safety net: never let one stray lone-surrogate char (e.g. an emoji mistakenly written as a
    # 🆕 UTF-16 surrogate pair in some source label) crash the ENTIRE daily report write /
    # D1 upsert / Issue. Root cause is fixed at the source strings; this only degrades gracefully.
    report_md = report_md.encode("utf-8", "replace").decode("utf-8")

    out_path = REPORTS_DIR / f"{today}_v3.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\n[\u5b8c\u6210] \u62a5\u544a\u5199\u5165: {out_path}")

    
    try:
        d1_title, d1_summary = build_d1_title_summary(today, synthesis_data, len(top_items))
        push_d1_intel_report(today, d1_title, d1_summary, report_md)
    except Exception as e:
        print(f"  [D1\u5199\u5165] \u672a\u9884\u671f\u5f02\u5e38 (\u5df2\u6355\u83b7，\u4e0d\u5f71\u54cd\u4e3b\u6d41\u7a0b): {e}", flush=True)

    
    print(f"\n{'='*64}")
    print(f"\u7cbe\u534e\u9884\u89c8 TOP 10 (\u5171 {len(top_items)} \u6761)")
    print(f"{'='*64}")
    for i, item in enumerate(top_items[:10], 1):
        stars = "*" * item["score"]
        print(f"{i:2d}. [{item['category']}] [{stars}] {item['title'][:60]}")
        print(f"     \u6765\u6e90: {item['source']} | {item['reason'][:80]}")
        print()

    # ── 7. KPI ──
    print(f"── KPI ──")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt}")
    print(f"  \u539f\u59cb\u603b\u91cf:    {total_raw}")
    print(f"  \u5b9e\u9645\u5206\u6790:    {total_analyzed}")
    print(f"  \u7cbe\u534e TOP:    {len(top_items)}")
    print(f"  \u7cbe\u534e\u7387:      {len(top_items)/total_analyzed*100:.1f}%" if total_analyzed else '  \u7cbe\u534e\u7387: N/A')
    print(f"  \u5206\u6790\u6a21\u578b:    {', '.join(models_used)}")
    print(f"  \u8017\u65f6:        {elapsed:.1f} \u79d2")
    print()

    
    if args.cloud:
        _push_issue(today, top_items, raw_counts, total_raw, total_analyzed,
                    elapsed, models_used, synthesis_md=synthesis_md,
                    action_flags_md=action_flags_md, blindspot_md=blindspot_md,
                    top3_md=top3_md, selfcheck_md=selfcheck_md)

    
    push_wechat(today, top_items, raw_counts, total_raw, total_analyzed,
                elapsed, models_used)

    return out_path




#   id, report_date(UNIQUE), title, summary, content_md, created_at







def push_d1_intel_report(report_date: str, title: str, summary: str, content_md: str) -> bool:
    '\u628a\u4eca\u65e5\u62a5\u544a upsert \u8fdb guyaofang-db \u7684 intel_reports \u8868 (Cloudflare D1 REST API)'
    account_id  = os.environ.get("CF_ACCOUNT_ID", "").strip()
    api_token   = os.environ.get("D1_API_TOKEN", "").strip()
    database_id = os.environ.get("D1_DATABASE_ID", "").strip()

    if not (account_id and api_token and database_id):
        print('  [D1\u5199\u5165] CF_ACCOUNT_ID / D1_API_TOKEN / D1_DATABASE_ID \u4efb\u4e00\u7f3a\u5931\uff0c\u8df3\u8fc7'
              ' (\u672c\u5730/\u672a\u914d\u7f6e\u73af\u5883\u7684\u6b63\u5e38\u73b0\u8c61)', flush=True)
        return False

    url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
           f"/d1/database/{database_id}/query")
    sql = (
        "INSERT INTO intel_reports (report_date, title, summary, content_md) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(report_date) DO UPDATE SET "
        "title = excluded.title, summary = excluded.summary, content_md = excluded.content_md;"
    )
    payload = json.dumps({
        "sql": sql,
        "params": [report_date, title, summary, content_md],
    }).encode("utf-8")

    print(f"\n[D1\u5199\u5165] upsert intel_reports.report_date={report_date} ...", flush=True)
    try:
        raw = fetch_url(
            url, timeout=30, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_token}",
            },
        )
        result = json.loads(raw)
    except Exception as e:
        print(f"  [D1\u5199\u5165] \u8bf7\u6c42\u5931\u8d25 (\u4e0d\u4e2d\u65ad\u4e3b\u6d41\u7a0b): {e}", flush=True)
        return False

    if not result.get("success"):
        errs = result.get("errors") or result.get("error") or result
        print(f"  [D1\u5199\u5165] \u5931\u8d25 success=false (\u4e0d\u4e2d\u65ad\u4e3b\u6d41\u7a0b): "
              f"{json.dumps(errs, ensure_ascii=False)[:300]}", flush=True)
        return False

    print(f"  [D1\u5199\u5165] OK -> intel_reports.report_date={report_date} (\u5e42\u7b49 upsert)", flush=True)
    return True


def push_wechat(today: str, top_items: list, raw_counts: dict,
                total_raw: int, total_analyzed: int,
                elapsed: float, models_used: list):
    '\n    \u901a\u8fc7 Server\u9171\u63a8\u5fae\u4fe1\u901a\u77e5\u3002\n    SendKey \u4ece\u73af\u5883\u53d8\u91cf SERVERCHAN_KEY \u8bfb\uff0c\u7edd\u4e0d\u660e\u6587\u5199\u8fdb\u4ee3\u7801\u3002\n    Server\u9171 POST: https://sctapi.ftqq.com/{key}.send\n    body: title=... & desp=...  (Content-Type: application/x-www-form-urlencoded, UTF-8)\n    desp \u5b98\u65b9\u9650\u7ea6 32KB\uff1b\u592a\u957f\u63a8\u6458\u8981 + "\u8be6\u89c1 GitHub Issue"\u3002\n    '
    send_key = os.environ.get("SERVERCHAN_KEY", "").strip()
    if not send_key:
        print('[\u5fae\u4fe1\u63a8\u9001] SERVERCHAN_KEY \u672a\u8bbe\u7f6e\uff0c\u8df3\u8fc7', flush=True)
        return

    top_n = len(top_items)
    rate  = f"{top_n/total_analyzed*100:.1f}%" if total_analyzed else "N/A"

    
    wechat_title = (
        f"\u60c5\u62a5\u65e5\u62a5 {today} | \u6293\u53d6 {total_raw} | \u7cbe\u534e {top_n} ({rate})"
    )

    
    lines = [
        f"## \u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 · {today}",
        "",
        f"> \u6293\u53d6 **{total_raw}** \u6761 | \u7cbe\u534e **{top_n}** \u6761 | \u7cbe\u534e\u7387 {rate}",
        f"> \u6a21\u578b: {', '.join(models_used)} | \u8017\u65f6: {elapsed:.0f}s",
        "",
        '### \u7cbe\u534e TOP 10',
        "",
    ]
    for i, item in enumerate(top_items[:10], 1):
        score = item["score"]
        stars = "⭐" * score
        cat   = item.get("category", '\u672a\u5206\u7c7b')
        title_item = item["title"][:60]
        url   = item.get("url", "")
        reason = item.get("reason", "")[:80]
        if url:
            lines.append(f"{i}. [{title_item}]({url})")
        else:
            lines.append(f"{i}. {title_item}")
        lines.append(f"   {stars} [{cat}] {reason}")
        lines.append("")
    lines += [
        "---",
        "### KPI",
        f"- \u539f\u59cb\u603b\u91cf: **{total_raw}** \u6761",
        f"- \u5b9e\u9645\u5206\u6790: {total_analyzed} \u6761",
        f"- \u7cbe\u534e TOP: **{top_n}** \u6761",
        f"- \u7cbe\u534e\u7387: {rate}",
        "",
        '*\u8be6\u7ec6\u62a5\u544a\u89c1 GitHub Issues \u2192 gufangAI/sync-med*',
    ]
    desp = "\n".join(lines)

    
    MAX_DESP = 30000
    if len(desp.encode("utf-8")) > MAX_DESP:
        desp = desp[:MAX_DESP // 3] + '\n\n...(\u5185\u5bb9\u8fc7\u957f\u5df2\u622a\u65ad\uff0c\u8be6\u89c1 GitHub Issue)'

    url_api = f"https://sctapi.ftqq.com/{send_key}.send"
    payload = urllib.parse.urlencode({
        "title": wechat_title,
        "desp":  desp,
    }).encode("utf-8")

    print(f"\n[\u5fae\u4fe1\u63a8\u9001] Server\u9171\u63a8\u9001\u4e2d ...", flush=True)
    try:
        req = urllib.request.Request(
            url_api, data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            result = json.loads(body)
            if result.get("errno") == 0 or result.get("code") == 0:
                pushid = result.get("data", {}).get("pushid", result.get("pushid", "?"))
                print(f"  [OK] \u5fae\u4fe1\u63a8\u9001\u6210\u529f | pushid={pushid}", flush=True)
            else:
                print(f"  [WARN] Server\u9171\u8fd4\u56de\u975e0: {body[:200]}", flush=True)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"  [ERROR] \u5fae\u4fe1\u63a8\u9001 HTTP {e.code}: {body}", flush=True)
    except Exception as e:
        print(f"  [ERROR] \u5fae\u4fe1\u63a8\u9001\u5931\u8d25: {e}", flush=True)


def _push_issue(today: str, top_items: list, raw_counts: dict,
                total_raw: int, total_analyzed: int,
                elapsed: float, models_used: list,
                synthesis_md: Optional[str] = None,
                action_flags_md: Optional[str] = None,
                blindspot_md: Optional[str] = None,
                top3_md: Optional[str] = None,
                selfcheck_md: Optional[str] = None):
    '\n    \u7528 gh CLI \u521b\u5efa Issue \u5230 gufangAI/sync-med\u3002\n    GH_TOKEN \u7531 Actions \u81ea\u52a8\u6ce8\u5165,\u65e0\u9700\u989d\u5916\u914d\u7f6e\u3002\n    '
    import subprocess

    top_n = len(top_items)
    rate  = f"{top_n/total_analyzed*100:.1f}%" if total_analyzed else "N/A"
    model_short = models_used[0] if models_used else "N/A"

    
    title = (
        f"[\u60c5\u62a5\u96f7\u8fbe v3] {today} | "
        f"\u6293\u53d6 {total_raw} | \u7cbe\u534e {top_n} | {rate} | {model_short}"
    )

    
    body_lines = [
        f"## \u60c5\u62a5\u96f7\u8fbe v3 \u65e5\u62a5 · {today}",
        "",
        f"> \u6d77\u91cf\u6293\u53d6: **{total_raw} \u6761** | AI \u5206\u6790: {total_analyzed} \u6761 | "
        f"\u7cbe\u534e: **{top_n} \u6761** | \u7cbe\u534e\u7387: {rate}",
        f"> \u5206\u6790\u6a21\u578b: {', '.join(models_used)} | \u8017\u65f6: {elapsed:.0f}s",
        "",
    ]
    # \u7f6e\u9876: \u4eca\u65e5 TOP3 \u51b3\u7b56\u5c31\u7eea (\u8ba9\u521b\u59cb\u4eba\u4e00\u773c\u770b\u5230\u8be5\u62cd\u677f\u7684, \u800c\u975e\u626b\u5168\u90e8)
    if selfcheck_md:
        body_lines.append(selfcheck_md)
    if top3_md:
        body_lines.append(top3_md)
    if synthesis_md:

        body_lines.append(synthesis_md)
    else:
        body_lines += ["---", ""]
    if action_flags_md:
        body_lines.append(action_flags_md)
    if blindspot_md:
        body_lines.append(blindspot_md)
    body_lines += [
        '### \u7cbe\u534e TOP 15',
        "",
    ]
    for i, item in enumerate(top_items[:15], 1):
        score = item["score"]
        stars = "⭐" * score
        cat   = item.get("category", '\u672a\u5206\u7c7b')
        title_item = item["title"]
        url   = item.get("url", "")
        reason = item.get("reason", "")
        if url:
            body_lines.append(f"{i}. **[{title_item}]({url})**")
        else:
            body_lines.append(f"{i}. **{title_item}**")
        verified = item.get("verified")
        verify_note = item.get("verify_note", "")
        if verified is True:
            vmark = f" | ✅已核实" + (f"({verify_note})" if verify_note else "")
        elif verified is False:
            vmark = f" | ⚠️待核实" + (f"({verify_note})" if verify_note else "")
        else:
            vmark = ""
        body_lines.append(
            f"   - {stars} [{cat}] {reason}{vmark}"
        )
        body_lines.append("")

    body_lines += [
        "---",
        "",
        "### KPI",
        "",
        '| \u6307\u6807 | \u503c |',
        "|------|-----|",
    ]
    for src, cnt in sorted(raw_counts.items(), key=lambda x: -x[1]):
        body_lines.append(f"| \u6293\u53d6: {src} | {cnt} |")
    body_lines += [
        f"| \u539f\u59cb\u603b\u91cf | **{total_raw}** |",
        f"| \u5b9e\u9645\u5206\u6790 | {total_analyzed} |",
        f"| \u7cbe\u534e\u6761\u6570 | **{top_n}** |",
        f"| \u7cbe\u534e\u7387   | {rate} |",
        f"| \u8017\u65f6     | {elapsed:.0f}s |",
        "",
        "---",
        "",
        '### \u5206\u7c7b\u7edf\u8ba1',
        "",
        '| \u5206\u7c7b | \u6761\u6570 |',
        "|------|------|",
    ]
    by_cat: dict[str, int] = {}
    for item in top_items:
        cat = item.get("category", '\u672a\u5206\u7c7b')
        by_cat[cat] = by_cat.get(cat, 0) + 1
    CATEGORY_EMOJI_LOCAL = {
        "RAG": "🔍", '\u5224\u65ad\u5f15\u64ce': "🧠", 'OCR\u6587\u5b57\u5316': "📄",
        '\u4e2d\u533bNLP': "🏥", '\u65b9\u6cd5\u524d\u6cbf': "🚀", '\u7ade\u54c1\u60c5\u62a5': "👀",
        '\u514d\u8d39\u8d44\u6e90': "🎁", '\u672a\u5206\u7c7b': "📌",
    }
    for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        emoji = CATEGORY_EMOJI_LOCAL.get(cat, "📌")
        body_lines.append(f"| {emoji} {cat} | {cnt} |")

    body_lines.append("")
    body_lines.append(f"*\u81ea\u52a8\u751f\u6210 · \u60c5\u62a5\u96f7\u8fbe v3 · {today}*")

    body = "\n".join(body_lines)

    
    tmp_body = Path("/tmp/intel_radar_issue_body.md")
    tmp_body.write_text(body, encoding="utf-8")

    label_args = []
    
    
    cmd = [
        "gh", "issue", "create",
        "--repo", "gufangAI/sync-med",
        "--title", title,
        "--body-file", str(tmp_body),
    ]

    print(f"\n[Issue] \u63a8\u9001\u4e2d ...", flush=True)
    print(f"  \u6807\u9898: {title}", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            print(f"  [OK] Issue \u521b\u5efa\u6210\u529f: {issue_url}", flush=True)
            
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(f"## \u60c5\u62a5\u96f7\u8fbe v3 · {today}\n\n")
                    f.write(f"- \u6293\u53d6: **{total_raw}** \u6761\n")
                    f.write(f"- \u7cbe\u534e: **{top_n}** \u6761 ({rate})\n")
                    f.write(f"- \u6a21\u578b: {', '.join(models_used)}\n")
                    f.write(f"- Issue: {issue_url}\n")
        else:
            print(f"  [ERROR] gh issue create \u5931\u8d25 (code={result.returncode}):", flush=True)
            print(f"  stdout: {result.stdout[:500]}", flush=True)
            print(f"  stderr: {result.stderr[:500]}", flush=True)
    except subprocess.TimeoutExpired:
        print('  [ERROR] gh issue create \u8d85\u65f6', flush=True)
    except FileNotFoundError:
        print('  [ERROR] gh CLI \u672a\u5b89\u88c5 (runner \u5e94\u81ea\u5e26)', flush=True)


if __name__ == "__main__":
    main()
