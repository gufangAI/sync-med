'\n\u60c5\u62a5\u96f7\u8fbe \xb7 \u6bcf\u65e5\u62a5\u544a\u751f\u6210\u5668 v2 (\u4e91\u7aef\u7248)\n======================================\n\u4e13\u4e3a GitHub Actions (\u6d77\u5916 runner) \u8bbe\u8ba1:\n  - \u5206\u6790\u6a21\u578b: \u4f18\u5148 DeepSeek (NVIDIA NIM) / \u5907\u7528 \u667a\u8c31 GLM-4-Flash\n  - \u60c5\u62a5\u6e90: arXiv cs.AI + HuggingFace Daily Papers + PubMed TCM+AI\n  - \u62a5\u544a\u901a\u8fc7 `gh issue create` \u63a8\u9001\u5230 gufangAI/sync-med Issues (\u2192 \u624b\u673a\u901a\u77e5)\n\n\u73af\u5883\u53d8\u91cf (GitHub Secrets):\n    NVIDIA_API_KEY   \u2014 NVIDIA NIM API Key (deepseek-ai/deepseek-r1)\n    ZHIPU_API_KEY    \u2014 \u667a\u8c31 GLM-4-Flash Key (\u5907\u7528,NVIDIA \u4e0d\u53ef\u7528\u65f6\u542f\u52a8)\n\n\u65e0 key \u65f6\u811a\u672c\u4ecd\u53ef\u8fd0\u884c,\u4f46\u5206\u6790\u6b65\u9aa4\u4f1a\u8df3\u8fc7(\u53ea\u51fa\u539f\u59cb\u5217\u8868\u62a5\u544a)\u3002\n\n\u7528\u6cd5:\n    python daily_report_cloud.py\n'

import sys
import os
import re
import time
import json
import datetime
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path




NVIDIA_BASE  = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "deepseek-ai/deepseek-v4-pro"  
ZHIPU_BASE   = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL  = "glm-4-flash"


NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
ZHIPU_KEY  = os.environ.get("ZHIPU_API_KEY", "")


ARXIV_URL = (
    "http://export.arxiv.org/api/query"
    "?search_query=cat:cs.AI"
    "&sortBy=submittedDate&sortOrder=descending"
    "&max_results=15"
)
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"


PUBMED_SEARCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    "?db=pubmed&retmode=json&retmax=10"
    "&term=traditional+Chinese+medicine+AND+artificial+intelligence"
    "&sort=pub+date&datetype=pdat&reldate=7"   
)
PUBMED_FETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


ISSUE_REPO = "gufangAI/sync-med"


SUEAI_CONTEXT = '\n\u6211\u4eec\u7684\u4ea7\u54c1 SueAI \u662f\u4e2d\u533b\u53e4\u7c4d\u667a\u80fd\u5206\u6790\u7cfb\u7edf,\u6838\u5fc3\u6a21\u5757\u5305\u62ec:\n1. \u60c5\u62a5\u96f7\u8fbe \u2014 \u81ea\u52a8\u6293 AI \u524d\u6cbf\u8bba\u6587,\u7b5b\u51fa\u5bf9\u4e2d\u533b AI \u6709\u4ef7\u503c\u7684\n2. RAG \u68c0\u7d22\u5f15\u64ce \u2014 \u57fa\u4e8e 2100+ \u53e4\u5178\u533b\u6848 + 7700+ \u53e4\u7c4d\u7684\u5411\u91cf\u68c0\u7d22\n3. AI \u5bfb\u8109 \u2014 \u7ed9\u75c7\u72b6\u751f\u6210"\u53e4\u7c4d\u8fa8\u8bc1\u53c2\u9605\u62a5\u544a"(\u6587\u732e\u4e3b\u8bed,\u975e\u8bca\u7597)\n4. \u5224\u65ad\u6eaf\u6e90 \u2014 \u7ed9 AI \u8f93\u51fa\u6807\u6ce8\u6587\u732e\u51fa\u5904+\u53ef\u4fe1\u5ea6\u5206\u7ea7\n5. SueAI \u4e13\u5bb6\u5206\u8eab \u2014 \u4e2d\u533b\u540d\u5bb6\u5b66\u6d3e\u89c6\u89d2\u95ee\u7b54\n\n\u5173\u952e\u6280\u672f\u65b9\u5411:\u4e2d\u533b NLP\u3001\u53e4\u7c4d OCR\u3001RAG/GraphRAG\u3001\u6587\u672c\u5206\u7c7b\u3001embedding\u3001\u77e5\u8bc6\u56fe\u8c31\u3001\n\u4e2d\u533b\u672f\u8bed\u6807\u51c6\u5316\u3001\u4f20\u7edf\u533b\u5b66\u6587\u732e\u6316\u6398\n'




def fetch_url(url: str, timeout: int = 30, data: bytes = None,
              headers: dict = None) -> str:
    '\u901a\u7528 HTTP \u8bf7\u6c42,\u652f\u6301 GET/POST,\u8fd4\u56de\u54cd\u5e94\u6587\u672c'
    req_headers = {"User-Agent": "IntelRadar/2.0"}
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
        raise RuntimeError(f"HTTP {e.code} {url}: {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"\u7f51\u7edc\u8bf7\u6c42\u5931\u8d25 {url}: {e}")


def llm_chat(messages: list, max_tokens: int = 2000) -> str:
    '\n    \u8c03\u7528 LLM (NVIDIA NIM DeepSeek \u4f18\u5148 / \u667a\u8c31 GLM-4-Flash \u5907\u7528)\u3002\n    \u65e0\u53ef\u7528 key \u65f6\u629b\u51fa RuntimeError\u3002\n    '
    if NVIDIA_KEY:
        base, model, key = NVIDIA_BASE, NVIDIA_MODEL, NVIDIA_KEY
    elif ZHIPU_KEY:
        base, model, key = ZHIPU_BASE, ZHIPU_MODEL, ZHIPU_KEY
    else:
        raise RuntimeError('\u65e0\u53ef\u7528 LLM key (NVIDIA_API_KEY / ZHIPU_API_KEY \u5747\u672a\u8bbe\u7f6e)')

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")

    try:
        raw = fetch_url(
            f"{base}/chat/completions",
            timeout=90,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        result = json.loads(raw)
        return result["choices"][0]["message"]["content"].strip()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"LLM \u8c03\u7528\u5931\u8d25: {e}")




def fetch_arxiv() -> list:
    '\u6293\u53d6 arXiv cs.AI \u6700\u65b0 15 \u7bc7'
    print('[\u6293\u53d6] arXiv cs.AI ...', end=" ", flush=True)
    try:
        raw = fetch_url(ARXIV_URL)
    except RuntimeError as e:
        print(f"\u5931\u8d25: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"XML \u89e3\u6790\u5931\u8d25: {e}")
        return []

    papers = []
    for entry in root.findall("atom:entry", ns):
        title    = (entry.findtext("atom:title", "", ns) or "").replace("\n", " ").strip()
        abstract = (entry.findtext("atom:summary", "", ns) or "").replace("\n", " ").strip()
        arxiv_id = (entry.findtext("atom:id", "", ns) or "").strip()
        if title:
            papers.append({
                "id": arxiv_id, "title": title,
                "abstract": abstract[:500], "url": arxiv_id,
                "source": "arXiv",
            })
    print(f"OK,\u83b7\u5f97 {len(papers)} \u6761")
    return papers


def fetch_hf_daily() -> list:
    '\u6293\u53d6 HuggingFace Daily Papers'
    print('[\u6293\u53d6] HuggingFace Daily Papers ...', end=" ", flush=True)
    try:
        raw = fetch_url(HF_DAILY_URL)
    except RuntimeError as e:
        print(f"\u5931\u8d25: {e}")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON \u89e3\u6790\u5931\u8d25: {e}")
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
                "abstract": abstract[:500], "url": url,
                "source": "HF Daily",
            })
    print(f"OK,\u83b7\u5f97 {len(papers)} \u6761")
    return papers


def fetch_pubmed() -> list:
    '\n    \u6293\u53d6 PubMed \u6700\u8fd1 7 \u5929 "traditional Chinese medicine AND artificial intelligence"\n    E-utilities \u5b8c\u5168\u514d\u8d39,\u65e0\u9700 API key\u3002\n    '
    print('[\u6293\u53d6] PubMed TCM+AI ...', end=" ", flush=True)
    try:
        raw = fetch_url(PUBMED_SEARCH_URL)
        search = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"\u641c\u7d22\u5931\u8d25: {e}")
        return []

    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        print('OK,0 \u6761 (\u8fd1 7 \u5929\u65e0\u65b0\u6587\u7ae0)')
        return []

    
    fetch_url_pm = (
        f"{PUBMED_FETCH_URL}?db=pubmed&retmode=xml&rettype=abstract"
        f"&id={','.join(pmids)}"
    )
    try:
        xml_raw = fetch_url(fetch_url_pm, timeout=40)
    except RuntimeError as e:
        print(f"efetch \u5931\u8d25: {e}")
        return []

    try:
        root = ET.fromstring(xml_raw)
    except ET.ParseError as e:
        print(f"XML \u89e3\u6790\u5931\u8d25: {e}")
        return []

    papers = []
    for article in root.findall(".//PubmedArticle"):
        
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""

        
        abstract_texts = article.findall(".//AbstractText")
        abstract = " ".join("".join(el.itertext()) for el in abstract_texts).strip()

        # PMID
        pmid_el = article.find(".//PMID")
        pmid    = pmid_el.text.strip() if pmid_el is not None else ""
        url     = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

        if title:
            papers.append({
                "id": pmid, "title": title,
                "abstract": abstract[:500], "url": url,
                "source": "PubMed",
            })

    print(f"OK,\u83b7\u5f97 {len(papers)} \u6761")
    return papers




def analyze_relevance(papers: list) -> list:
    '\n    \u628a\u6240\u6709\u8bba\u6587\u5582\u7ed9 LLM,\u7b5b\u51fa\u5bf9 SueAI \u6709\u4ef7\u503c\u7684\u6761\u76ee\u3002\n    \u8fd4\u56de [{title, url, source, reason}, ...]\n    '
    if not papers:
        return []

    items_text = "\n\n".join(
        f"[{i+1}] \u6765\u6e90:{p['source']} | \u6807\u9898: {p['title']}\n    \u6458\u8981: {p['abstract']}"
        for i, p in enumerate(papers)
    )

    prompt = f"""\u4f60\u662f SueAI \u60c5\u62a5\u96f7\u8fbe\u7684\u5206\u6790\u5927\u8111。\u4ee5\u4e0b\u662f\u4eca\u65e5 AI \u524d\u6cbf\u8bba\u6587\u5217\u8868。

{SUEAI_CONTEXT}

\u8bf7\u4ece\u4e0b\u9762\u7684\u8bba\u6587\u4e2d\u7b5b\u9009\u51fa\u5bf9 SueAI \u6709\u4ef7\u503c\u6216\u6709\u501f\u9274\u610f\u4e49\u7684\u6761\u76ee。
\u5224\u65ad\u6807\u51c6:RAG/\u68c0\u7d22\u589e\u5f3a、\u77e5\u8bc6\u56fe\u8c31、\u6587\u672c\u5206\u7c7b、\u53e4\u7c4d/\u533b\u5b66NLP、embedding、OCR、
\u591a\u6587\u6863\u63a8\u7406、\u6eaf\u6e90/\u53ef\u89e3\u91caAI、\u4e2d\u533b\u76f8\u5173\u4efb\u4f55\u65b9\u5411、\u4f20\u7edf\u533b\u5b66+AI \u7ed3\u5408\u7814\u7a76。
\u82f1\u6587\u8bba\u6587\u4e5f\u8981\u5206\u6790,\u4e0d\u8981\u56e0\u4e3a\u662f\u82f1\u6587\u5c31\u8df3\u8fc7。
\u6765\u81ea PubMed \u7684\u4e2d\u533bAI\u8bba\u6587\u4f18\u5148\u91cd\u70b9\u5173\u6ce8。

\u8bba\u6587\u5217\u8868:
{items_text}

\u8bf7\u4ee5\u5982\u4e0b JSON \u683c\u5f0f\u8f93\u51fa(\u53ea\u8f93\u51fa JSON,\u4e0d\u8981\u591a\u4f59\u6587\u5b57):
[
  {{
    "index": <\u539f\u5217\u8868\u7f16\u53f7>,
    "reason": "<\u4e00\u53e5\u8bdd\u8bf4\u660e\u5bf9 SueAI \u54ea\u4e2a\u6a21\u5757\u6709\u4ef7\u503c,\u4e3a\u4ec0\u4e48>"
  }},
  ...
]
\u5982\u679c\u6ca1\u6709\u4efb\u4f55\u76f8\u5173\u8bba\u6587,\u8f93\u51fa\u7a7a\u6570\u7ec4 []。"""

    model_name = NVIDIA_MODEL if NVIDIA_KEY else (ZHIPU_MODEL if ZHIPU_KEY else '\u65e0key')
    print(f"[\u5206\u6790] \u5171 {len(papers)} \u7bc7 → LLM({model_name}) ...", end=" ", flush=True)

    try:
        response = llm_chat([{"role": "user", "content": prompt}], max_tokens=2000)
    except RuntimeError as e:
        print(f"\u5931\u8d25: {e}")
        return []

    
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                picks = json.loads(m.group())
            except json.JSONDecodeError:
                print(f"JSON \u89e3\u6790\u5931\u8d25,\u539f\u59cb\u56de\u590d:\n{text[:300]}")
                return []
        else:
            print(f"\u65e0\u6cd5\u63d0\u53d6 JSON,\u539f\u59cb\u56de\u590d:\n{text[:300]}")
            return []

    results = []
    for pick in picks:
        idx = pick.get("index", 0) - 1
        if 0 <= idx < len(papers):
            p = papers[idx]
            results.append({
                "title":  p["title"],
                "url":    p["url"],
                "source": p["source"],
                "reason": pick.get("reason", ""),
            })

    print(f"OK,\u7b5b\u51fa {len(results)} \u6761\u76f8\u5173")
    return results




def generate_report(
    date_str: str,
    sources: list,
    raw_counts: dict,
    relevant: list,
    elapsed: float,
    model_used: str,
) -> str:
    '\u751f\u6210 Markdown \u62a5\u544a\u5b57\u7b26\u4e32'
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    rate_str       = f"{total_relevant/total_raw*100:.1f}%" if total_raw else "N/A"

    lines = [
        f"# \u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 · {date_str}",
        "",
        f"> \u81ea\u52a8\u751f\u6210 | \u626b\u63cf\u6e90: {', '.join(sources)} | \u5206\u6790\u6a21\u578b: {model_used}",
        "",
        "---",
        "",
        '## \u76f8\u5173\u8bba\u6587\u7cbe\u9009',
        "",
    ]

    if relevant:
        for i, item in enumerate(relevant, 1):
            lines.append(f"### {i}. [{item['title']}]({item['url']})" if item["url"]
                         else f"### {i}. {item['title']}")
            lines.append(f"- **\u6765\u6e90**: {item['source']}")
            lines.append(f"- **\u4ef7\u503c**: {item['reason']}")
            lines.append("")
    else:
        lines.append('\u4eca\u65e5\u672a\u53d1\u73b0\u4e0e SueAI \u9ad8\u5ea6\u76f8\u5173\u7684\u8bba\u6587\u3002')
        lines.append("")

    lines += [
        "---",
        "",
        "## KPI",
        "",
        '| \u6307\u6807 | \u503c |',
        "|------|-----|",
        f"| \u626b\u63cf\u6e90\u6570 | {len(sources)} |",
    ]
    for src, cnt in raw_counts.items():
        lines.append(f"| \u6293\u53d6\u539f\u59cb\u6761\u76ee ({src}) | {cnt} \u6761 |")
    lines += [
        f"| \u539f\u59cb\u603b\u6761\u76ee | {total_raw} \u6761 |",
        f"| \u7b5b\u51fa\u76f8\u5173\u6761\u76ee | {total_relevant} \u6761 |",
        f"| \u76f8\u5173\u7387 | {rate_str} |",
        f"| \u5206\u6790\u6a21\u578b | {model_used} |",
        f"| \u751f\u6210\u8017\u65f6 | {elapsed:.1f} \u79d2 |",
        "",
    ]

    return "\n".join(lines)


def build_issue_title(date_str: str, relevant: list, raw_counts: dict) -> str:
    '\u6784\u5efa Issue \u6807\u9898,\u542b KPI \u6458\u8981'
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    return (
        f"[\u60c5\u62a5\u96f7\u8fbe] {date_str} | "
        f"\u626b\u63cf {total_raw} \u7bc7 · \u7b5b\u51fa {total_relevant} \u6761\u76f8\u5173"
    )




def push_issue(title: str, body: str) -> bool:
    '\n    \u7528 gh CLI \u628a\u62a5\u544a\u63a8\u9001\u5230 gufangAI/sync-med Issues\u3002\n    gh CLI \u5728 GitHub Actions ubuntu \u4e0a\u9ed8\u8ba4\u53ef\u7528,GITHUB_TOKEN \u81ea\u52a8\u6ce8\u5165\u3002\n    \u8fd4\u56de True = \u6210\u529f\u3002\n    '
    import subprocess
    import tempfile

    
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                    encoding="utf-8", delete=False) as f:
        f.write(body)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", ISSUE_REPO,
                "--title", title,
                "--body-file", tmp_path,
                "--label", "intel-radar",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            print(f"[Issue] \u63a8\u9001\u6210\u529f: {url}")
            return True
        else:
            
            if "label" in result.stderr.lower() or "could not resolve" in result.stderr.lower():
                print('[Issue] label \u4e0d\u5b58\u5728,\u53bb\u6389 --label \u91cd\u8bd5 ...')
                result2 = subprocess.run(
                    [
                        "gh", "issue", "create",
                        "--repo", ISSUE_REPO,
                        "--title", title,
                        "--body-file", tmp_path,
                    ],
                    capture_output=True, text=True, timeout=60,
                )
                if result2.returncode == 0:
                    print(f"[Issue] \u63a8\u9001\u6210\u529f: {result2.stdout.strip()}")
                    return True
                else:
                    print(f"[Issue] \u63a8\u9001\u5931\u8d25: {result2.stderr[:300]}")
                    return False
            print(f"[Issue] \u63a8\u9001\u5931\u8d25 (rc={result.returncode}): {result.stderr[:300]}")
            return False
    except subprocess.TimeoutExpired:
        print('[Issue] gh \u8c03\u7528\u8d85\u65f6')
        return False
    except FileNotFoundError:
        print('[Issue] gh CLI \u672a\u5b89\u88c5 (\u672c\u5730\u8fd0\u884c\u65f6\u6b63\u5e38,Actions \u73af\u5883\u5185\u7f6e)')
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass




def main():
    t0    = time.time()
    today = datetime.date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"\u60c5\u62a5\u96f7\u8fbe\u65e5\u62a5 v2 (\u4e91\u7aef\u7248) · {today}")
    print(f"{'='*60}\n")

    
    if NVIDIA_KEY:
        model_used = f"DeepSeek R1 (NVIDIA NIM)"
    elif ZHIPU_KEY:
        model_used = f"GLM-4-Flash (\u667a\u8c31)"
    else:
        model_used = '\u65e0 (\u8df3\u8fc7 AI \u5206\u6790)'
    print(f"[\u914d\u7f6e] \u5206\u6790\u6a21\u578b: {model_used}")

    
    arxiv_papers  = fetch_arxiv()
    hf_papers     = fetch_hf_daily()
    pubmed_papers = fetch_pubmed()

    all_papers = arxiv_papers + hf_papers + pubmed_papers
    raw_counts = {
        "arXiv cs.AI":    len(arxiv_papers),
        "HF Daily":       len(hf_papers),
        "PubMed TCM+AI":  len(pubmed_papers),
    }
    sources = [k for k, v in raw_counts.items() if v > 0]

    if not all_papers:
        print('[\u8b66\u544a] \u6240\u6709\u60c5\u62a5\u6e90\u5747\u6293\u53d6\u5931\u8d25,\u9000\u51fa')
        sys.exit(1)

    
    if NVIDIA_KEY or ZHIPU_KEY:
        relevant = analyze_relevance(all_papers)
    else:
        print('[\u5206\u6790] \u65e0 LLM key,\u8df3\u8fc7 AI \u7b5b\u9009 (\u5c06\u5217\u51fa\u6240\u6709\u6761\u76ee)')
        relevant = [
            {"title": p["title"], "url": p["url"],
             "source": p["source"], "reason": '\u672a\u5206\u6790'}
            for p in all_papers
        ]

    
    elapsed   = time.time() - t0
    report_md = generate_report(today, sources, raw_counts, relevant, elapsed, model_used)

    
    out_path = REPORTS_DIR / f"{today}.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\n[\u5b8c\u6210] \u62a5\u544a\u5df2\u5199\u5165: {out_path}")

    
    issue_title = build_issue_title(today, relevant, raw_counts)
    push_issue(issue_title, report_md)

    
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    print(f"\n── KPI ──")
    print(f"  \u626b\u63cf\u6e90:  {len(sources)} \u4e2a ({', '.join(sources)})")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt} \u6761")
    print(f"  \u539f\u59cb\u603b\u8ba1:  {total_raw} \u6761")
    print(f"  \u7b5b\u51fa\u76f8\u5173:  {total_relevant} \u6761")
    print(f"  \u76f8\u5173\u7387:    {total_relevant/total_raw*100:.1f}%" if total_raw else '  \u76f8\u5173\u7387: N/A')
    print(f"  \u5206\u6790\u6a21\u578b:  {model_used}")
    print(f"  \u8017\u65f6:     {elapsed:.1f} \u79d2")
    print()

    return out_path


if __name__ == "__main__":
    main()
