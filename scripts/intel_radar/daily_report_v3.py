"""
情报雷达 · 海量抓取 + 多模型并行筛精华 v3
==========================================
升级亮点:
  1. 海量抓取 (vs 旧版 35 条)
     - arXiv: cs.AI + cs.CL + cs.IR 各最多 300 条 (9 天窗口) → ~900 条
     - GitHub Search API: AI/ML/LLM 仓库按 stars + created 时间窗 → 200-400 个
     - HuggingFace: daily papers + trending models
     - PubMed: 中医 AI 相关 (30 天) → 50+ 条
     - 总量: 1000-1500 条/天
  2. 多模型并行分析
     - 优先调本地核动力池 localhost:4000 (glm-4-flash / siliconflow-qwen / modelscope-qwen)
     - 把大量分批 (每批 30 条)，用 asyncio 并发多 worker 同时打不同模型
     - 控速: 每模型限并发 3，批间 sleep 避免薅爆
  3. 筛精华 + 分类
     - 每条打分 1-5 + 分类标签 (RAG/判断引擎/OCR/中医NLP/竞品/方法前沿)
     - 按分数排序，筛出 TOP 50
  4. KPI 报告

本地运行 (需核动力池已启动 localhost:4000):
    python daily_report_v3.py

云端 GitHub Actions 运行 (无本地网关):
    设置 ZHIPU_API_KEY 或 NVIDIA_API_KEY
    python daily_report_v3.py --cloud

控额度:
    - 魔搭 modelscope: 2000 次/天 → 分 33 批 × 30 条,每批 1 次调用
    - 硅基 siliconflow: 1000 RPM → 批间 2s sleep 够用
    - 智谱 glm-4-flash: 永久免费,无明确上限 → 主力
    - NVIDIA: credits 有限 → 仅 fallback,不主动调
"""

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
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import argparse

# Windows GBK 终端: 强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 目录 ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).parent
REPORTS_DIR = SCRIPT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── 核动力池配置 (localhost:4000) ─────────────────────────────────────────────
# 网关内的模型名 (来自 litellm_config.yaml)
GATEWAY_BASE   = "http://localhost:4000"
GATEWAY_KEY    = "sk-litellm-local-dev"

# 并行分析使用的模型列表 (免费额度充足的先用)
GATEWAY_MODELS = [
    "glm-4-flash",        # 智谱，永久免费，无明确并发上限
    "modelscope-qwen",    # 魔搭 Qwen3-8B，2000次/天
    "siliconflow-qwen",   # 硅基 Qwen2.5-7B，1000 RPM
]

# 云端备用 (GitHub Actions 无本地网关时)
NVIDIA_BASE    = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL   = "deepseek-ai/deepseek-v4-flash"
ZHIPU_BASE     = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_MODEL    = "glm-4-flash"
NVIDIA_KEY     = os.environ.get("NVIDIA_API_KEY", "")
ZHIPU_KEY      = os.environ.get("ZHIPU_API_KEY", "")

# ── 抓取配置 ──────────────────────────────────────────────────────────────────
ARXIV_DAYS       = 3      # arXiv 时间窗 (天)
ARXIV_MAX        = 300    # 每个 cat 最多抓
ARXIV_CATS       = ["cs.AI", "cs.CL", "cs.IR"]
GITHUB_MAX       = 300    # GitHub 仓库最多
PUBMED_DAYS      = 30     # PubMed 时间窗
PUBMED_MAX       = 50
HF_TRENDING_MAX  = 100

# 分析批次大小 & 并发
BATCH_SIZE       = 30     # 每批多少条喂给 LLM
MAX_WORKERS      = 3      # 同时并发几个模型
BATCH_SLEEP      = 2.0    # 批间休眠秒数 (节流)

# ── SueAI 上下文 ──────────────────────────────────────────────────────────────
SUEAI_CONTEXT = """
SueAI 是中医古籍智能分析系统:
1. 情报雷达 — 扫 AI 前沿,筛对中医 AI 有价值的
2. RAG 检索 — 2100+ 古典医案 + 7700+ 古籍向量检索
3. AI 寻脉 — 症状 → 古籍辨证参阅报告 (文献主语,非诊疗)
4. 判断溯源 — AI 输出标注文献出处 + 可信度分级
5. 专家分身 — 中医名家学派视角问答
6. 古籍 OCR — 扫描版医书/古籍文字化

关键技术方向: 中医 NLP、古籍 OCR、RAG/GraphRAG、文本分类、embedding、
知识图谱、中医术语标准化、传统医学文献挖掘、多文档推理、可解释 AI

分类标签定义:
- RAG: 检索增强生成、向量检索、embedding、向量数据库
- 判断引擎: 可解释 AI、溯源、证据链、知识图谱推理
- OCR/文字化: 文档 OCR、版面分析、古文识别
- 中医NLP: 中医术语、传统医学、古籍分析
- 方法前沿: 新型架构/训练方法,通用但对我们有借鉴价值
- 竞品情报: 同类医疗 AI 产品、中医 AI 平台
- 免费资源: 可直接复用的开源模型/数据集/工具
"""

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def fetch_url(url: str, timeout: int = 30, data: bytes = None,
              headers: dict = None) -> str:
    """同步 HTTP 请求"""
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
        raise RuntimeError(f"网络请求失败: {e}")


# ── 情报源抓取 ────────────────────────────────────────────────────────────────

def fetch_arxiv_cat(cat: str, max_results: int = ARXIV_MAX) -> list:
    """抓取单个 arXiv 分类，返回论文列表"""
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
        print(f"    [{cat}] 失败: {e}")
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"    [{cat}] XML 解析失败: {e}")
        return []

    cutoff = datetime.date.today() - datetime.timedelta(days=ARXIV_DAYS)
    papers = []
    for entry in root.findall("atom:entry", ns):
        title    = (entry.findtext("atom:title", "", ns) or "").replace("\n", " ").strip()
        abstract = (entry.findtext("atom:summary", "", ns) or "").replace("\n", " ").strip()
        arxiv_id = (entry.findtext("atom:id", "", ns) or "").strip()
        published = (entry.findtext("atom:published", "", ns) or "").strip()

        # 过滤时间窗
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


def fetch_arxiv_all() -> list:
    """并行抓取 cs.AI + cs.CL + cs.IR"""
    print(f"[抓取] arXiv ({', '.join(ARXIV_CATS)}) 最近 {ARXIV_DAYS} 天 ...", flush=True)
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
        print(f"    [{cat}] +{new} 条 (去重后)")
        time.sleep(1)  # arXiv 礼貌延迟
    print(f"  arXiv 合计: {len(all_papers)} 条")
    return all_papers


def fetch_github_trending() -> list:
    """
    用 GitHub Search API 抓最近 7 天新建的 AI/ML 相关热门仓库。
    无需 token (匿名 60次/h,够用)。
    """
    print(f"[抓取] GitHub Trending AI/ML (7天内,≤{GITHUB_MAX}) ...", flush=True)
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
            print(f"    [GitHub] 查询失败 ({q[:50]}...): {e}")
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
                "abstract": f"{desc} | 话题: {topics} | ⭐{item.get('stargazers_count', 0)}",
                "url": item.get("html_url", ""),
                "source": "GitHub Trending",
                "stars": item.get("stargazers_count", 0),
                "lang": item.get("language", ""),
            })
            new += 1
        print(f"    [GitHub] q='{q[:40]}...' +{new} 条")
        time.sleep(1.5)  # GitHub API 礼貌延迟

    print(f"  GitHub 合计: {len(repos)} 个仓库")
    return repos


def fetch_hf_papers() -> list:
    """抓取 HuggingFace Daily Papers"""
    print("[抓取] HuggingFace Daily Papers ...", end=" ", flush=True)
    try:
        raw = fetch_url("https://huggingface.co/api/daily_papers", timeout=30)
        data = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"失败: {e}")
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
    print(f"OK, {len(papers)} 条")
    return papers


def fetch_hf_trending_models() -> list:
    """抓取 HuggingFace trending models (API)"""
    print(f"[抓取] HuggingFace Trending Models (≤{HF_TRENDING_MAX}) ...", end=" ", flush=True)
    # HF API: sort by likes (trending 已改参数), 取最近上传/热门
    url = f"https://huggingface.co/api/models?sort=likes&limit={HF_TRENDING_MAX}&direction=-1"
    try:
        raw = fetch_url(url, timeout=30)
        data = json.loads(raw)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"失败: {e}")
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
            "abstract": f"标签: {tags} | 👍{likes} | ⬇{dl}",
            "url": f"https://huggingface.co/{mid}",
            "source": "HF Trending Models",
        })
    print(f"OK, {len(models)} 条")
    return models


def fetch_pubmed() -> list:
    """PubMed 中医 AI 相关论文"""
    print(f"[抓取] PubMed TCM+AI (最近 {PUBMED_DAYS} 天) ...", end=" ", flush=True)
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
        print(f"搜索失败: {e}")
        return []

    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        print("0 条")
        return []

    fetch_url_pm = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&retmode=xml&rettype=abstract&id={','.join(pmids)}"
    )
    try:
        xml_raw = fetch_url(fetch_url_pm, timeout=60)
        root = ET.fromstring(xml_raw)
    except (RuntimeError, ET.ParseError) as e:
        print(f"efetch 失败: {e}")
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

    print(f"OK, {len(papers)} 条")
    return papers


# ── LLM 调用 (同步，用于 asyncio executor) ────────────────────────────────────

def _call_llm_sync(model: str, messages: list, max_tokens: int = 1500,
                   use_gateway: bool = True) -> str:
    """
    同步调用 LLM。
    use_gateway=True → 本地核动力池 localhost:4000
    use_gateway=False → 云端 API (ZHIPU / NVIDIA)
    """
    if use_gateway:
        base, key = GATEWAY_BASE, GATEWAY_KEY
    elif ZHIPU_KEY:
        base, key, model = ZHIPU_BASE, ZHIPU_KEY, ZHIPU_MODEL
    elif NVIDIA_KEY:
        base, key, model = NVIDIA_BASE, NVIDIA_KEY, NVIDIA_MODEL
    else:
        raise RuntimeError("无可用 API key (网关/ZHIPU/NVIDIA 均不可用)")

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode("utf-8")

    raw = fetch_url(
        f"{base}/chat/completions",
        timeout=150,  # 网关有 60s upstream timeout; 本地到网关保留 90s 余量
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    result = json.loads(raw)
    return result["choices"][0]["message"]["content"].strip()


def check_gateway_alive() -> bool:
    """检查核动力池 localhost:4000 是否可用"""
    try:
        raw = fetch_url(f"{GATEWAY_BASE}/models", timeout=5,
                        headers={"Authorization": f"Bearer {GATEWAY_KEY}"})
        return True
    except Exception:
        return False


# ── 分析提示词 ────────────────────────────────────────────────────────────────

ANALYZE_PROMPT_TPL = """你是 SueAI 情报雷达分析大脑。

{context}

以下是一批情报条目 (论文/仓库/模型)。请筛选出对 SueAI 有价值的条目,每条打分并分类。

条目列表:
{items}

对每条条目:
1. 判断是否与 SueAI 技术方向相关 (RAG/判断引擎/OCR/中医NLP/方法前沿/竞品情报/免费资源)
2. 如相关: 打分 1-5 (5=极高价值,1=弱相关),给出分类标签 + 一句话理由
3. 如不相关: 跳过

只输出 JSON 数组 (无多余文字):
[
  {{
    "index": <条目编号>,
    "score": <1-5>,
    "category": "<RAG|判断引擎|OCR文字化|中医NLP|方法前沿|竞品情报|免费资源>",
    "reason": "<一句话: 对 SueAI 哪个模块有价值>"
  }},
  ...
]
无相关条目时输出 []。"""


def build_items_text(batch: list, offset: int = 0) -> str:
    lines = []
    for i, p in enumerate(batch):
        title    = p.get("title", "")
        abstract = p.get("abstract", "")[:300]
        source   = p.get("source", "")
        lines.append(f"[{offset + i + 1}] [{source}] {title}\n    {abstract}")
    return "\n\n".join(lines)


# ── 并行分析引擎 ──────────────────────────────────────────────────────────────

async def analyze_batch_async(
    batch: list,
    batch_idx: int,
    offset: int,
    model: str,
    use_gateway: bool,
    loop: asyncio.AbstractEventLoop,
) -> list:
    """
    异步分析单批，在 executor 中调同步 LLM 函数。
    返回 [{index, score, category, reason}, ...]
    """
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
        print(f"    [批{batch_idx}|{model}] LLM 失败: {e}", flush=True)
        return []

    # 解析 JSON
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
                print(f"    [批{batch_idx}|{model}] JSON 解析失败,原始: {text[:150]}", flush=True)
                return []
        else:
            return []

    # 标注模型来源 + 修复 score 字段 (siliconflow 有时返回文字描述而非数字)
    SCORE_TEXT_MAP = {
        "极高价值": 5, "非常高": 5, "高价值": 4, "有价值": 3,
        "弱相关": 1, "低相关": 1, "无关": 0, "不相关": 0,
        "中等": 3, "较高": 4, "一般": 2,
    }
    cleaned = []
    for pick in picks:
        score = pick.get("score", 0)
        if isinstance(score, str):
            # 尝试转数字
            try:
                score = int(float(score.strip()))
            except (ValueError, TypeError):
                # 从文字映射
                score = SCORE_TEXT_MAP.get(score.strip(), 0)
        pick["score"] = max(0, min(5, int(score)))
        pick["_model"] = model
        if pick["score"] > 0:  # 过滤掉 score=0 的垃圾
            cleaned.append(pick)
    return cleaned


async def analyze_all_parallel(
    all_items: list,
    use_gateway: bool,
    models: list,
) -> list:
    """
    把 all_items 分批，用多个模型并发分析。
    返回全部 pick 列表 (含 index/score/category/reason/_model)
    """
    batches = [all_items[i:i+BATCH_SIZE] for i in range(0, len(all_items), BATCH_SIZE)]
    n_batches = len(batches)
    print(f"\n[并行分析] {len(all_items)} 条 -> {n_batches} 批 x {BATCH_SIZE} | "
          f"模型: {models} | 并发 worker: {MAX_WORKERS}", flush=True)

    loop = asyncio.get_event_loop()
    all_picks = []
    sem = asyncio.Semaphore(MAX_WORKERS)

    async def bounded_analyze(batch, batch_idx, offset, model):
        async with sem:
            picks = await analyze_batch_async(
                batch, batch_idx, offset, model, use_gateway, loop
            )
            hit = len(picks)
            print(f"    [OK] 批{batch_idx:03d}/{n_batches} [{model}] "
                  f"-> {hit} 条命中 (共 {len(batch)} 条)", flush=True)
            if hit:
                all_picks.extend(picks)
            await asyncio.sleep(BATCH_SLEEP)

    # 分派任务：每批只给一个模型（轮询，均衡负载）
    tasks = []
    for bi, batch in enumerate(batches):
        model = models[bi % len(models)]
        offset = bi * BATCH_SIZE
        tasks.append(bounded_analyze(batch, bi + 1, offset, model))

    await asyncio.gather(*tasks)
    return all_picks


# ── 精华整理 ──────────────────────────────────────────────────────────────────

def merge_picks(all_items: list, raw_picks: list, top_n: int = 50,
               min_score: int = 2) -> list:
    """
    把 raw_picks 映射回 all_items，去重，按分数排序，取 TOP N。
    min_score: 最低入选分 (默认 2，过滤 modelscope 过宽松的全量命中)
    """
    # index 是 1-based (相对整个 all_items 列表)
    idx_map: dict[int, dict] = {}
    for pick in raw_picks:
        idx = pick.get("index", 0)
        # index 可能是字符串
        try:
            idx = int(str(idx).strip().strip('"').strip("'"))
        except (ValueError, TypeError):
            continue
        if idx <= 0 or idx > len(all_items):
            continue
        score = pick.get("score", 1)
        if score < min_score:
            continue  # 过滤低分
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
            "category": pick.get("category", "未分类"),
            "reason":   pick.get("reason", ""),
            "_model":   pick.get("_model", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


# ── 报告生成 ──────────────────────────────────────────────────────────────────

CATEGORY_EMOJI = {
    "RAG":       "🔍",
    "判断引擎":  "🧠",
    "OCR文字化": "📄",
    "中医NLP":   "🏥",
    "方法前沿":  "🚀",
    "竞品情报":  "👀",
    "免费资源":  "🎁",
    "未分类":    "📌",
}

def generate_report_v3(
    date_str: str,
    raw_counts: dict,
    top_items: list,
    elapsed: float,
    models_used: list,
    gateway_alive: bool,
    total_raw: int,
    total_analyzed: int,
) -> str:
    """生成 Markdown 报告"""
    total_top = len(top_items)
    rate_str  = f"{total_top/total_analyzed*100:.1f}%" if total_analyzed else "N/A"

    # 按分类分组
    by_cat: dict[str, list] = {}
    for item in top_items:
        cat = item.get("category", "未分类")
        by_cat.setdefault(cat, []).append(item)

    lines = [
        f"# 情报雷达日报 v3 · {date_str}",
        "",
        f"> 自动生成 | 海量抓取: **{total_raw} 条** | AI 分析: {total_analyzed} 条 | "
        f"精华: **{total_top} 条** | 核动力池: {'✅ 在线' if gateway_alive else '❌ 离线(备用)'}",
        f"> 分析模型: {', '.join(models_used)}",
        "",
        "---",
        "",
        "## 精华情报 (按分值排序)",
        "",
    ]

    for cat, items in sorted(by_cat.items(), key=lambda x: -max(i["score"] for i in x[1])):
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        lines.append(f"### {emoji} {cat} ({len(items)} 条)")
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
            lines.append(f"- 分值: {stars} ({score}/5) | 来源: {item['source']}")
            lines.append(f"- 价值: {item['reason']}")
            lines.append("")
        lines.append("")

    # KPI 表
    lines += [
        "---",
        "",
        "## KPI",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
    ]
    for src, cnt in sorted(raw_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| 抓取: {src} | {cnt} 条 |")
    lines += [
        f"| 原始总量 | **{total_raw} 条** |",
        f"| 实际分析 | {total_analyzed} 条 |",
        f"| 筛出精华 | **{total_top} 条** |",
        f"| 精华率 | {rate_str} |",
        f"| 核动力池 | {'在线' if gateway_alive else '离线(备用)'} |",
        f"| 分析模型 | {', '.join(models_used)} |",
        f"| 耗时 | {elapsed:.1f} 秒 |",
        "",
    ]

    # 分类统计
    lines += [
        "## 分类统计",
        "",
        "| 分类 | 条数 |",
        "|------|------|",
    ]
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        lines.append(f"| {CATEGORY_EMOJI.get(cat,'📌')} {cat} | {len(items)} |")

    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="情报雷达 v3 — 海量抓取+多模型并行筛精华")
    parser.add_argument("--cloud", action="store_true",
                        help="云端模式: 跳过本地网关,用 ZHIPU/NVIDIA env key")
    parser.add_argument("--no-github", action="store_true", help="跳过 GitHub 抓取(省速率)")
    parser.add_argument("--no-hf-models", action="store_true", help="跳过 HF trending models")
    parser.add_argument("--dry-run", action="store_true",
                        help="只抓取不分析(测试抓取量)")
    parser.add_argument("--top", type=int, default=50, help="精华 TOP N (默认 50)")
    args = parser.parse_args()

    t0    = time.time()
    today = datetime.date.today().strftime("%Y-%m-%d")

    print(f"\n{'='*64}")
    print(f"情报雷达日报 v3 (海量抓取 + 多模型并行) · {today}")
    print(f"{'='*64}\n")

    # ── 1. 检查网关 ──
    gateway_alive = False
    use_gateway   = False
    models_used   = []

    if not args.cloud:
        print("[检查] 核动力池 localhost:4000 ...", end=" ", flush=True)
        gateway_alive = check_gateway_alive()
        if gateway_alive:
            print("OK (在线)")
            use_gateway = True
            models_used = GATEWAY_MODELS
        else:
            print("离线")

    if not use_gateway:
        # 云端备用
        if ZHIPU_KEY:
            models_used = ["glm-4-flash (zhipu cloud)"]
            print(f"[模型] 使用云端 {models_used[0]}")
        elif NVIDIA_KEY:
            models_used = ["deepseek-v4-flash (nvidia)"]
            print(f"[模型] 使用云端 {models_used[0]}")
        else:
            print("[警告] 无可用 LLM (网关离线 + 无云端 key),仅输出原始列表")
            models_used = ["(无分析)"]

    # ── 2. 海量抓取 ──
    print("\n[=== 海量抓取阶段 ===]")
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
    if not args.no_github:
        github_repos = fetch_github_trending()
        raw_counts["GitHub Trending"] = len(github_repos)

    all_items = arxiv_papers + hf_papers + hf_models + pubmed_papers + github_repos
    total_raw = len(all_items)

    print(f"\n[抓取汇总] 总计: {total_raw} 条")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt}")

    if not all_items:
        print("[错误] 所有情报源均抓取失败,退出")
        sys.exit(1)

    if args.dry_run:
        print("\n[--dry-run] 跳过 AI 分析,仅输出抓取 KPI")
        elapsed = time.time() - t0
        print(f"\n── 抓取 KPI (dry-run) ──")
        print(f"  总抓取: {total_raw} 条")
        print(f"  耗时: {elapsed:.1f}s")
        return

    # ── 3. 多模型并行分析 ──
    print("\n[=== 多模型并行分析阶段 ===]")

    # HF Trending Models 数量大但含噪多,用前 50 参与分析节省额度
    hf_models_sample = hf_models[:50] if hf_models else []
    analyze_items = arxiv_papers + hf_papers + pubmed_papers + github_repos + hf_models_sample
    total_analyzed = len(analyze_items)
    print(f"  实际分析: {total_analyzed} 条 (HF Models 截取前 50)")

    if use_gateway or (ZHIPU_KEY or NVIDIA_KEY):
        # 确定调用模型列表
        if use_gateway:
            active_models = GATEWAY_MODELS
        else:
            active_models = ["fallback"]  # _call_llm_sync 里会走云端

        raw_picks = asyncio.run(
            analyze_all_parallel(analyze_items, use_gateway, active_models)
        )
        print(f"\n[分析完成] 总命中 pick: {len(raw_picks)} 条")
    else:
        print("[分析] 无 LLM 可用,跳过筛选,列出全部")
        raw_picks = [
            {"index": i+1, "score": 1, "category": "未分类", "reason": "未分析", "_model": "无"}
            for i in range(min(len(analyze_items), args.top))
        ]

    # ── 4. 筛精华 ──
    top_items = merge_picks(analyze_items, raw_picks, top_n=args.top)
    print(f"[精华] 筛出 TOP {len(top_items)} 条 (score>=1)")

    # ── 5. 出报告 ──
    elapsed   = time.time() - t0
    report_md = generate_report_v3(
        today, raw_counts, top_items, elapsed, models_used,
        gateway_alive, total_raw, total_analyzed,
    )

    out_path = REPORTS_DIR / f"{today}_v3.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\n[完成] 报告写入: {out_path}")

    # ── 6. 打印精华预览 ──
    print(f"\n{'='*64}")
    print(f"精华预览 TOP 10 (共 {len(top_items)} 条)")
    print(f"{'='*64}")
    for i, item in enumerate(top_items[:10], 1):
        stars = "*" * item["score"]
        print(f"{i:2d}. [{item['category']}] [{stars}] {item['title'][:60]}")
        print(f"     来源: {item['source']} | {item['reason'][:80]}")
        print()

    # ── 7. KPI ──
    print(f"── KPI ──")
    for src, cnt in raw_counts.items():
        print(f"  {src}: {cnt}")
    print(f"  原始总量:    {total_raw}")
    print(f"  实际分析:    {total_analyzed}")
    print(f"  精华 TOP:    {len(top_items)}")
    print(f"  精华率:      {len(top_items)/total_analyzed*100:.1f}%" if total_analyzed else "  精华率: N/A")
    print(f"  分析模型:    {', '.join(models_used)}")
    print(f"  耗时:        {elapsed:.1f} 秒")
    print()

    # ── 8. 云端模式: gh issue create 推手机 ──
    if args.cloud:
        _push_issue(today, top_items, raw_counts, total_raw, total_analyzed,
                    elapsed, models_used)

    return out_path


def _push_issue(today: str, top_items: list, raw_counts: dict,
                total_raw: int, total_analyzed: int,
                elapsed: float, models_used: list):
    """
    用 gh CLI 创建 Issue 到 gufangAI/sync-med。
    GH_TOKEN 由 Actions 自动注入,无需额外配置。
    """
    import subprocess

    top_n = len(top_items)
    rate  = f"{top_n/total_analyzed*100:.1f}%" if total_analyzed else "N/A"
    model_short = models_used[0] if models_used else "N/A"

    # Issue 标题: 带日期 + 核心 KPI
    title = (
        f"[情报雷达 v3] {today} | "
        f"抓取 {total_raw} | 精华 {top_n} | {rate} | {model_short}"
    )

    # 构建 Issue body (TOP 15 精华 + 完整 KPI + 分类统计)
    body_lines = [
        f"## 情报雷达 v3 日报 · {today}",
        "",
        f"> 海量抓取: **{total_raw} 条** | AI 分析: {total_analyzed} 条 | "
        f"精华: **{top_n} 条** | 精华率: {rate}",
        f"> 分析模型: {', '.join(models_used)} | 耗时: {elapsed:.0f}s",
        "",
        "---",
        "",
        "### 精华 TOP 15",
        "",
    ]
    for i, item in enumerate(top_items[:15], 1):
        score = item["score"]
        stars = "⭐" * score
        cat   = item.get("category", "未分类")
        title_item = item["title"]
        url   = item.get("url", "")
        reason = item.get("reason", "")
        if url:
            body_lines.append(f"{i}. **[{title_item}]({url})**")
        else:
            body_lines.append(f"{i}. **{title_item}**")
        body_lines.append(
            f"   - {stars} [{cat}] {reason}"
        )
        body_lines.append("")

    body_lines += [
        "---",
        "",
        "### KPI",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
    ]
    for src, cnt in sorted(raw_counts.items(), key=lambda x: -x[1]):
        body_lines.append(f"| 抓取: {src} | {cnt} |")
    body_lines += [
        f"| 原始总量 | **{total_raw}** |",
        f"| 实际分析 | {total_analyzed} |",
        f"| 精华条数 | **{top_n}** |",
        f"| 精华率   | {rate} |",
        f"| 耗时     | {elapsed:.0f}s |",
        "",
        "---",
        "",
        "### 分类统计",
        "",
        "| 分类 | 条数 |",
        "|------|------|",
    ]
    by_cat: dict[str, int] = {}
    for item in top_items:
        cat = item.get("category", "未分类")
        by_cat[cat] = by_cat.get(cat, 0) + 1
    CATEGORY_EMOJI_LOCAL = {
        "RAG": "🔍", "判断引擎": "🧠", "OCR文字化": "📄",
        "中医NLP": "🏥", "方法前沿": "🚀", "竞品情报": "👀",
        "免费资源": "🎁", "未分类": "📌",
    }
    for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
        emoji = CATEGORY_EMOJI_LOCAL.get(cat, "📌")
        body_lines.append(f"| {emoji} {cat} | {cnt} |")

    body_lines.append("")
    body_lines.append(f"*自动生成 · 情报雷达 v3 · {today}*")

    body = "\n".join(body_lines)

    # 写临时文件(避免 shell 引号转义地狱)
    tmp_body = Path("/tmp/intel_radar_issue_body.md")
    tmp_body.write_text(body, encoding="utf-8")

    label_args = []
    # 检查 label 是否存在,不强制创建(公开 repo 可能没有这个 label)
    # 直接不加 label 避免失败
    cmd = [
        "gh", "issue", "create",
        "--repo", "gufangAI/sync-med",
        "--title", title,
        "--body-file", str(tmp_body),
    ]

    print(f"\n[Issue] 推送中 ...", flush=True)
    print(f"  标题: {title}", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            print(f"  [OK] Issue 创建成功: {issue_url}", flush=True)
            # 写入 GITHUB_STEP_SUMMARY (Actions 显示)
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(f"## 情报雷达 v3 · {today}\n\n")
                    f.write(f"- 抓取: **{total_raw}** 条\n")
                    f.write(f"- 精华: **{top_n}** 条 ({rate})\n")
                    f.write(f"- 模型: {', '.join(models_used)}\n")
                    f.write(f"- Issue: {issue_url}\n")
        else:
            print(f"  [ERROR] gh issue create 失败 (code={result.returncode}):", flush=True)
            print(f"  stdout: {result.stdout[:500]}", flush=True)
            print(f"  stderr: {result.stderr[:500]}", flush=True)
    except subprocess.TimeoutExpired:
        print("  [ERROR] gh issue create 超时", flush=True)
    except FileNotFoundError:
        print("  [ERROR] gh CLI 未安装 (runner 应自带)", flush=True)


if __name__ == "__main__":
    main()
