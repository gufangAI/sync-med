"""
情报雷达 · 每日报告生成器 v2 (云端版)
======================================
专为 GitHub Actions (海外 runner) 设计:
  - 分析模型: 优先 DeepSeek (NVIDIA NIM) / 备用 智谱 GLM-4-Flash
  - 情报源: arXiv cs.AI + HuggingFace Daily Papers + PubMed TCM+AI
  - 报告通过 `gh issue create` 推送到 gufangAI/sync-med Issues (→ 手机通知)

环境变量 (GitHub Secrets):
    NVIDIA_API_KEY   — NVIDIA NIM API Key (deepseek-ai/deepseek-r1)
    ZHIPU_API_KEY    — 智谱 GLM-4-Flash Key (备用,NVIDIA 不可用时启动)

无 key 时脚本仍可运行,但分析步骤会跳过(只出原始列表报告)。

用法:
    python daily_report_cloud.py
"""

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

# ── 配置 ──────────────────────────────────────────────────────────────────────

# 模型优先级: NVIDIA NIM DeepSeek → 智谱 GLM-4-Flash
NVIDIA_BASE  = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "deepseek-ai/deepseek-r1"
ZHIPU_BASE   = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL  = "glm-4-flash"

# key 从环境变量读,绝不明文
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
ZHIPU_KEY  = os.environ.get("ZHIPU_API_KEY", "")

# 情报源
ARXIV_URL = (
    "http://export.arxiv.org/api/query"
    "?search_query=cat:cs.AI"
    "&sortBy=submittedDate&sortOrder=descending"
    "&max_results=15"
)
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"

# PubMed E-utilities (免费,无需 key)
PUBMED_SEARCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    "?db=pubmed&retmode=json&retmax=10"
    "&term=traditional+Chinese+medicine+AND+artificial+intelligence"
    "&sort=pub+date&datetype=pdat&reldate=7"   # 最近 7 天
)
PUBMED_FETCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# 报告输出目录 (Actions 环境用当前目录下的 reports/)
REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 目标 Issue 仓库
ISSUE_REPO = "gufangAI/sync-med"

# SueAI 定位上下文
SUEAI_CONTEXT = """
我们的产品 SueAI 是中医古籍智能分析系统,核心模块包括:
1. 情报雷达 — 自动抓 AI 前沿论文,筛出对中医 AI 有价值的
2. RAG 检索引擎 — 基于 2100+ 古典医案 + 7700+ 古籍的向量检索
3. AI 寻脉 — 给症状生成"古籍辨证参阅报告"(文献主语,非诊疗)
4. 判断溯源 — 给 AI 输出标注文献出处+可信度分级
5. SueAI 专家分身 — 中医名家学派视角问答

关键技术方向:中医 NLP、古籍 OCR、RAG/GraphRAG、文本分类、embedding、知识图谱、
中医术语标准化、传统医学文献挖掘
"""


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def fetch_url(url: str, timeout: int = 30, data: bytes = None,
              headers: dict = None) -> str:
    """通用 HTTP 请求,支持 GET/POST,返回响应文本"""
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
        raise RuntimeError(f"网络请求失败 {url}: {e}")


def llm_chat(messages: list, max_tokens: int = 2000) -> str:
    """
    调用 LLM (NVIDIA NIM DeepSeek 优先 / 智谱 GLM-4-Flash 备用)。
    无可用 key 时抛出 RuntimeError。
    """
    if NVIDIA_KEY:
        base, model, key = NVIDIA_BASE, NVIDIA_MODEL, NVIDIA_KEY
    elif ZHIPU_KEY:
        base, model, key = ZHIPU_BASE, ZHIPU_MODEL, ZHIPU_KEY
    else:
        raise RuntimeError("无可用 LLM key (NVIDIA_API_KEY / ZHIPU_API_KEY 均未设置)")

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
        raise RuntimeError(f"LLM 调用失败: {e}")


# ── 情报源抓取 ────────────────────────────────────────────────────────────────

def fetch_arxiv() -> list:
    """抓取 arXiv cs.AI 最新 15 篇"""
    print("[抓取] arXiv cs.AI ...", end=" ", flush=True)
    try:
        raw = fetch_url(ARXIV_URL)
    except RuntimeError as e:
        print(f"失败: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"XML 解析失败: {e}")
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
    print(f"OK,获得 {len(papers)} 条")
    return papers


def fetch_hf_daily() -> list:
    """抓取 HuggingFace Daily Papers"""
    print("[抓取] HuggingFace Daily Papers ...", end=" ", flush=True)
    try:
        raw = fetch_url(HF_DAILY_URL)
    except RuntimeError as e:
        print(f"失败: {e}")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败: {e}")
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
    print(f"OK,获得 {len(papers)} 条")
    return papers


def fetch_pubmed() -> list:
    """
    抓取 PubMed 最近 7 天 "traditional Chinese medicine AND artificial intelligence"
    E-utilities 完全免费,无需 API key。
    """
    print("[抓取] PubMed TCM+AI ...", end=" ", flush=True)
    try:
        raw = fetch_url(PUBMED_SEARCH_URL)
        search = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"搜索失败: {e}")
        return []

    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        print("OK,0 条 (近 7 天无新文章)")
        return []

    # 批量获取摘要 (efetch XML)
    fetch_url_pm = (
        f"{PUBMED_FETCH_URL}?db=pubmed&retmode=xml&rettype=abstract"
        f"&id={','.join(pmids)}"
    )
    try:
        xml_raw = fetch_url(fetch_url_pm, timeout=40)
    except RuntimeError as e:
        print(f"efetch 失败: {e}")
        return []

    try:
        root = ET.fromstring(xml_raw)
    except ET.ParseError as e:
        print(f"XML 解析失败: {e}")
        return []

    papers = []
    for article in root.findall(".//PubmedArticle"):
        # 标题
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""

        # 摘要 (可能分段)
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

    print(f"OK,获得 {len(papers)} 条")
    return papers


# ── 核动力池分析 ──────────────────────────────────────────────────────────────

def analyze_relevance(papers: list) -> list:
    """
    把所有论文喂给 LLM,筛出对 SueAI 有价值的条目。
    返回 [{title, url, source, reason}, ...]
    """
    if not papers:
        return []

    items_text = "\n\n".join(
        f"[{i+1}] 来源:{p['source']} | 标题: {p['title']}\n    摘要: {p['abstract']}"
        for i, p in enumerate(papers)
    )

    prompt = f"""你是 SueAI 情报雷达的分析大脑。以下是今日 AI 前沿论文列表。

{SUEAI_CONTEXT}

请从下面的论文中筛选出对 SueAI 有价值或有借鉴意义的条目。
判断标准:RAG/检索增强、知识图谱、文本分类、古籍/医学NLP、embedding、OCR、
多文档推理、溯源/可解释AI、中医相关任何方向、传统医学+AI 结合研究。
英文论文也要分析,不要因为是英文就跳过。
来自 PubMed 的中医AI论文优先重点关注。

论文列表:
{items_text}

请以如下 JSON 格式输出(只输出 JSON,不要多余文字):
[
  {{
    "index": <原列表编号>,
    "reason": "<一句话说明对 SueAI 哪个模块有价值,为什么>"
  }},
  ...
]
如果没有任何相关论文,输出空数组 []。"""

    model_name = NVIDIA_MODEL if NVIDIA_KEY else (ZHIPU_MODEL if ZHIPU_KEY else "无key")
    print(f"[分析] 共 {len(papers)} 篇 → LLM({model_name}) ...", end=" ", flush=True)

    try:
        response = llm_chat([{"role": "user", "content": prompt}], max_tokens=2000)
    except RuntimeError as e:
        print(f"失败: {e}")
        return []

    # 清理 markdown 代码块
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
                print(f"JSON 解析失败,原始回复:\n{text[:300]}")
                return []
        else:
            print(f"无法提取 JSON,原始回复:\n{text[:300]}")
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

    print(f"OK,筛出 {len(results)} 条相关")
    return results


# ── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report(
    date_str: str,
    sources: list,
    raw_counts: dict,
    relevant: list,
    elapsed: float,
    model_used: str,
) -> str:
    """生成 Markdown 报告字符串"""
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    rate_str       = f"{total_relevant/total_raw*100:.1f}%" if total_raw else "N/A"

    lines = [
        f"# 情报雷达日报 · {date_str}",
        "",
        f"> 自动生成 | 扫描源: {', '.join(sources)} | 分析模型: {model_used}",
        "",
        "---",
        "",
        "## 相关论文精选",
        "",
    ]

    if relevant:
        for i, item in enumerate(relevant, 1):
            lines.append(f"### {i}. [{item['title']}]({item['url']})" if item["url"]
                         else f"### {i}. {item['title']}")
            lines.append(f"- **来源**: {item['source']}")
            lines.append(f"- **价值**: {item['reason']}")
            lines.append("")
    else:
        lines.append("今日未发现与 SueAI 高度相关的论文。")
        lines.append("")

    lines += [
        "---",
        "",
        "## KPI",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 扫描源数 | {len(sources)} |",
    ]
    for src, cnt in raw_counts.items():
        lines.append(f"| 抓取原始条目 ({src}) | {cnt} 条 |")
    lines += [
        f"| 原始总条目 | {total_raw} 条 |",
        f"| 筛出相关条目 | {total_relevant} 条 |",
        f"| 相关率 | {rate_str} |",
        f"| 分析模型 | {model_used} |",
        f"| 生成耗时 | {elapsed:.1f} 秒 |",
        "",
    ]

    return "\n".join(lines)


def build_issue_title(date_str: str, relevant: list, raw_counts: dict) -> str:
    """构建 Issue 标题,含 KPI 摘要"""
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    return (
        f"[情报雷达] {date_str} | "
        f"扫描 {total_raw} 篇 · 筛出 {total_relevant} 条相关"
    )


# ── 推送 Issue ────────────────────────────────────────────────────────────────

def push_issue(title: str, body: str) -> bool:
    """
    用 gh CLI 把报告推送到 gufangAI/sync-med Issues。
    gh CLI 在 GitHub Actions ubuntu 上默认可用,GITHUB_TOKEN 自动注入。
    返回 True = 成功。
    """
    import subprocess
    import tempfile

    # 把 body 写临时文件,避免 shell 转义问题
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
            print(f"[Issue] 推送成功: {url}")
            return True
        else:
            # label 不存在时去掉 --label 重试
            if "label" in result.stderr.lower() or "could not resolve" in result.stderr.lower():
                print("[Issue] label 不存在,去掉 --label 重试 ...")
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
                    print(f"[Issue] 推送成功: {result2.stdout.strip()}")
                    return True
                else:
                    print(f"[Issue] 推送失败: {result2.stderr[:300]}")
                    return False
            print(f"[Issue] 推送失败 (rc={result.returncode}): {result.stderr[:300]}")
            return False
    except subprocess.TimeoutExpired:
        print("[Issue] gh 调用超时")
        return False
    except FileNotFoundError:
        print("[Issue] gh CLI 未安装 (本地运行时正常,Actions 环境内置)")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    t0    = time.time()
    today = datetime.date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"情报雷达日报 v2 (云端版) · {today}")
    print(f"{'='*60}\n")

    # 显示使用的模型
    if NVIDIA_KEY:
        model_used = f"DeepSeek R1 (NVIDIA NIM)"
    elif ZHIPU_KEY:
        model_used = f"GLM-4-Flash (智谱)"
    else:
        model_used = "无 (跳过 AI 分析)"
    print(f"[配置] 分析模型: {model_used}")

    # 1. 抓取情报源
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
        print("[警告] 所有情报源均抓取失败,退出")
        sys.exit(1)

    # 2. 核动力池分析 (无 key 时跳过)
    if NVIDIA_KEY or ZHIPU_KEY:
        relevant = analyze_relevance(all_papers)
    else:
        print("[分析] 无 LLM key,跳过 AI 筛选 (将列出所有条目)")
        relevant = [
            {"title": p["title"], "url": p["url"],
             "source": p["source"], "reason": "未分析"}
            for p in all_papers
        ]

    # 3. 生成报告
    elapsed   = time.time() - t0
    report_md = generate_report(today, sources, raw_counts, relevant, elapsed, model_used)

    # 4. 写入本地文件
    out_path = REPORTS_DIR / f"{today}.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\n[完成] 报告已写入: {out_path}")

    # 5. 推送 Issue (→ 手机通知)
    issue_title = build_issue_title(today, relevant, raw_counts)
    push_issue(issue_title, report_md)

    # 6. 打印 KPI
    total_raw      = sum(raw_counts.values())
    total_relevant = len(relevant)
    print(f"\n── KPI ──")
    print(f"  扫描源:  {len(sources)} 个 ({', '.join(sources)})")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt} 条")
    print(f"  原始总计:  {total_raw} 条")
    print(f"  筛出相关:  {total_relevant} 条")
    print(f"  相关率:    {total_relevant/total_raw*100:.1f}%" if total_raw else "  相关率: N/A")
    print(f"  分析模型:  {model_used}")
    print(f"  耗时:     {elapsed:.1f} 秒")
    print()

    return out_path


if __name__ == "__main__":
    main()
