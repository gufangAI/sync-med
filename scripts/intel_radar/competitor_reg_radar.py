# -*- coding: utf-8 -*-
"""
情报雷达 · 竞品/监管定向雷达 v1
================================
背景:
  情报雷达(daily_report_v3.py / intel-radar.yml)只扫通用 AI 技术趋势
  (arXiv/GitHub/HuggingFace/PubMed/中文AI资讯),对以下两类完全没有覆盖:
    1. 直接竞品动态 —— 识典古籍(字节跳动)、国家中医药古籍数字图书馆、
       中国中医科学院中医药信息研究所、上海中医药大学古籍/知识图谱项目、
       广医岐智、灵兰秘典 等同类中医古籍/中医AI平台
    2. 中国AI/医疗监管动态 —— 卫健委互联网诊疗监管、网信办《人工智能生成合成
       内容标识办法》《人工智能拟人化互动服务管理暂行办法》、中医药AI行业标准

  2026-07-17 创始人钦定接入。不新建独立系统,复用情报雷达同一套引擎:
    - LLM 调用: 直接 import daily_report_v3._call_llm_sync (同一免费模型池:
      智谱 glm-4-flash 主力 / NVIDIA 兜底,与日报完全相同的 secrets/路由逻辑)
    - RSS 抓取: 直接 import daily_report_v3.fetch_rss / fetch_url (同一套
      HTTP/RSS 抓取引擎,不重写)
    - 新增: AnySearch(api.anysearch.com 匿名免费, 无 key)做通用网页搜索,
      补 Bing News RSS 覆盖不到的"竞品官网/百科/研究机构介绍"类内容
  daily_report_v3.py 本身零改动一行,不影响原有 AI 技术趋势扫描。

Prompt 与日报的 ANALYZE_PROMPT_TPL 不同 —— 日报判"是否与 SueAI 技术方向相关",
本雷达判"是否是竞品/监管的真实动态",两者相关性标准不同,必须用独立 prompt,
否则直接套用日报 prompt 会把监管类内容误判成"不相关"而丢弃。

频率: 每周一次(见 .github/workflows/competitor-reg-radar.yml),明显低于日报
的每日频率 —— 竞品/监管类信息本身变化较慢,没必要天天扫,避免无谓调用和噪音。

不写 D1 intel_reports 表: 该表 report_date 是 UNIQUE 字段,daily_report_v3.py
每天 upsert 一行;本雷达如果也写同一张表,遇到运行日期撞车会互相覆盖对方的报告。
本版本刻意只发 GitHub Issue,不碰 D1,避免数据覆盖风险(可作为后续独立表再接入)。
"""

import sys
import os
import re
import json
import time
import datetime
import subprocess
import urllib.parse
from pathlib import Path
import argparse

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))  # 确保能 import 同目录下的 daily_report_v3

from daily_report_v3 import (  # 复用情报雷达同一套引擎,不重造轮子
    fetch_url,
    fetch_rss,
    _call_llm_sync,
    ZHIPU_KEY,
    NVIDIA_KEY,
    ZHIPU_MODEL,
    NVIDIA_MODEL,
)

REPORTS_DIR = SCRIPT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

ANYSEARCH_ENDPOINT = "https://api.anysearch.com/mcp"  # 匿名免费访问,无需 key

BATCH_SIZE = 20
BATCH_SLEEP = 2.0


# ============================================================
# 定向搜索目标 —— 竞品 + 监管
# ============================================================

COMPETITOR_QUERIES = [
    ("识典古籍(字节跳动)", "识典古籍 字节跳动 古籍数字化 最新动态"),
    ("国家中医药古籍数字图书馆", "国家中医药古籍数字图书馆 中国中医科学院 中医药信息研究所"),
    ("上海中医药大学古籍知识图谱", "上海中医药大学 古籍 知识图谱 中医药人工智能"),
    ("广医岐智", "广医岐智 中医 人工智能 平台"),
    ("灵兰秘典", "灵兰秘典 中言 中医 人工智能"),
    ("中医药AI新入局竞品", "中医药 古籍数字化 人工智能平台 上线 发布"),
]

REGULATORY_QUERIES = [
    ("卫健委互联网诊疗监管", "国家卫健委 互联网诊疗监管细则 最新规定"),
    ("网信办AI生成内容标识办法", "人工智能生成合成内容标识办法 执行 落地 解读"),
    ("网信办拟人化互动服务管理办法", "人工智能拟人化互动服务管理暂行办法 解读 影响"),
    ("中医药人工智能行业标准", "中医药 人工智能 团体标准 行业标准 政策 2026"),
    ("生成式AI服务管理最新动态", "生成式人工智能服务管理办法 最新 监管 执法"),
]


# ============================================================
# 抓取层 —— AnySearch(新) + Bing News RSS(复用 weekly_hunter_v1 同款套路)
# ============================================================

def fetch_anysearch(query: str, max_results: int = 6, timeout: int = 30) -> list:
    """匿名调用 AnySearch (JSON-RPC 2.0, tools/call, 免费无 key)。
    通用网页搜索,覆盖竞品官网/百科/研究机构介绍等 Bing News 抓不到的内容。
    失败静默返回空,不阻断整轮抓取(与本文件其它 fetch_* 一致的容错风格)。
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "max_results": max_results}},
    }).encode("utf-8")
    try:
        raw = fetch_url(ANYSEARCH_ENDPOINT, timeout=timeout, data=payload,
                         headers={"Content-Type": "application/json"})
        data = json.loads(raw)
    except Exception as e:
        print(f"    [AnySearch] '{query[:30]}' 失败: {e}")
        return []

    if "error" in data:
        print(f"    [AnySearch] '{query[:30]}' API 错误: {str(data['error'])[:150]}")
        return []

    content = (data.get("result") or {}).get("content") or []
    text = ""
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            break
    if not text:
        return []

    # 解析 "### N. 标题\n- **URL**: xxx\n- 摘要..." 格式(AnySearch 固定输出格式)
    out = []
    blocks = re.split(r"\n(?=###\s*\d+\.)", text)
    for b in blocks:
        m_title = re.search(r"###\s*\d+\.\s*(.+)", b)
        if not m_title:
            continue
        m_url = re.search(r"\*\*URL\*\*:\s*(\S+)", b)
        title = m_title.group(1).strip()
        url = m_url.group(1).strip() if m_url else ""
        rest = b[m_url.end():] if m_url else b[m_title.end():]
        abstract = re.sub(r"^\s*-\s*", "", rest.strip())[:400]
        if title:
            out.append({"title": title, "url": url, "abstract": abstract, "source": "AnySearch"})
    return out


def _bing_news_url(query: str) -> str:
    return "https://www.bing.com/news/search?q=" + urllib.parse.quote(query) + "&format=rss"


def fetch_bucket(label: str, query: str, lane: str) -> list:
    """对一个定向目标同时跑 AnySearch(通用网页) + Bing News RSS(新闻类),
    合并后统一打上 lane(竞品/监管) + label 标签,方便后续溯源/展示。"""
    out = []
    out.extend(fetch_anysearch(query, max_results=6))
    time.sleep(0.5)
    out.extend(fetch_rss(_bing_news_url(query), source=f"BingNews:{label}", max_items=8))
    time.sleep(0.5)
    for it in out:
        it["lane_hint"] = lane
        it["source"] = f"[{lane}] {label} · {it.get('source', '')}"
    return out


def dedup_items(items: list) -> list:
    seen = set()
    out = []
    for it in items:
        key = (it.get("url") or "").strip() or (it.get("title") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ============================================================
# 分析层 —— 复用 daily_report_v3._call_llm_sync,自定义 prompt
# ============================================================

RADAR_CONTEXT = """古方AI星圖 是中医古籍+AI的平台,核心能力:
1. 古籍RAG检索(2100+古典医案 + 7700+古籍向量检索)
2. AI寻脉——症状 -> 古籍辨证参阅报告(文献主语,非诊疗)
3. 判断溯源——AI输出标注文献出处 + 可信度分级
4. 专家分身——中医名家学派视角问答
5. 古籍OCR——扫描版医书/古籍文字化

本轮任务不找技术前沿,只找两类此前完全没盯的定向情报:
- 【竞品】同类中医古籍/中医AI平台的产品动态(识典古籍/国家中医药古籍数字图书馆/
  中国中医科学院中医药信息研究所/上海中医药大学古籍或知识图谱项目/广医岐智/
  灵兰秘典 等,以及其它新出现的直接竞品)
- 【监管】会圈定平台合规边界的中国AI/医疗监管动态(卫健委互联网诊疗监管、网信办
  人工智能生成合成内容标识办法/拟人化互动服务管理暂行办法、中医药AI行业标准等)
"""

RADAR_PROMPT_TPL = RADAR_CONTEXT + """
以下是一批搜索命中条目,可能混有大量噪音(旧闻转载、无关同名词条、纯首页/索引页、
广告)。请逐条判断:

条目列表:
{items}

对每条:
1. 判断是否是【竞品】或【监管】的真实、有实质信息量的动态(不是纯首页/索引/广告/
   无关同名内容)
2. 相关则打分 1-5(5=对平台决策直接重要,例如竞品发布重大新功能、新规直接影响
   我们能不能上线某功能;1=弱相关背景信息),给出 lane("竞品"或"监管") + 一句话
   理由(说清对我们意味着什么,而不是复述标题)
3. 不相关或纯噪音: 跳过,不要输出

只输出 JSON 数组(无多余文字):
[
  {{"index": <编号>, "score": <1-5>, "lane": "竞品|监管", "reason": "<一句话:对古方AI星圖意味着什么>"}},
  ...
]
无相关条目时输出 []。
"""


def build_items_text(batch: list, offset: int = 0) -> str:
    lines = []
    for i, p in enumerate(batch):
        title = p.get("title", "")
        abstract = (p.get("abstract", "") or "")[:300]
        source = p.get("source", "")
        lines.append(f"[{offset + i + 1}] [{source}] {title}\n    {abstract}")
    return "\n\n".join(lines)


def analyze_batch(batch: list, offset: int, model: str, use_gateway: bool) -> list:
    items_text = build_items_text(batch, offset)
    prompt = RADAR_PROMPT_TPL.format(items=items_text)
    messages = [{"role": "user", "content": prompt}]
    try:
        response = _call_llm_sync(model, messages, max_tokens=1500, use_gateway=use_gateway)
    except Exception as e:
        print(f"    [分析] 批次失败: {e}")
        return []

    text = response.strip()
    if text.startswith("```"):
        inner = []
        for line in text.split("\n")[1:]:
            if line.strip() == "```":
                break
            inner.append(line)
        text = "\n".join(inner)

    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            picks = json.loads(m.group())
        except json.JSONDecodeError:
            print(f"    [分析] JSON 解析失败,原始: {text[:150]}")
            return []

    if not isinstance(picks, list):
        return []

    cleaned = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        score = pick.get("score", 0)
        try:
            score = int(float(str(score).strip()))
        except (ValueError, TypeError):
            score = 0
        pick["score"] = max(0, min(5, score))
        if pick["score"] > 0:
            cleaned.append(pick)
    return cleaned


def analyze_all(all_items: list, models: list, use_gateway: bool,
                batch_size: int = BATCH_SIZE, sleep_s: float = BATCH_SLEEP) -> list:
    """顺序批处理(非 asyncio)—— 本雷达每周一次、条目量级(~百条)远小于日报的
    每日千条级,没必要复用日报 analyze_all_parallel 的并发实现,顺序循环更简单
    可靠,出错也更容易定位。"""
    n = len(all_items)
    n_batches = (n + batch_size - 1) // batch_size
    print(f"\n[分析] {n} 条 -> {n_batches} 批 x {batch_size} | 模型: {models}", flush=True)
    picks = []
    for bi in range(n_batches):
        offset = bi * batch_size
        batch = all_items[offset:offset + batch_size]
        model = models[bi % len(models)]
        got = analyze_batch(batch, offset, model, use_gateway)
        print(f"    批{bi + 1}/{n_batches} [{model}] -> {len(got)} 条命中 (共 {len(batch)} 条)", flush=True)
        picks.extend(got)
        time.sleep(sleep_s)
    return picks


def merge_picks(all_items: list, raw_picks: list, top_n: int = 40, min_score: int = 2) -> list:
    idx_map = {}
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
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "abstract": (item.get("abstract", "") or "")[:220],
            "score": pick.get("score", 1),
            "lane": pick.get("lane", "未分类"),
            "reason": pick.get("reason", ""),
        })
    results.sort(key=lambda x: -x["score"])
    return results[:top_n]


# ============================================================
# 报告生成 + Issue 推送(风格参照日报,但明确区分竞品/监管两个板块)
# ============================================================

LANE_EMOJI = {"竞品": "🏯", "监管": "⚖️", "未分类": "📌"}


def generate_report(date_str: str, raw_counts: dict, top_items: list,
                     total_raw: int, elapsed: float, model_desc: str) -> str:
    by_lane: dict = {}
    for it in top_items:
        by_lane.setdefault(it.get("lane", "未分类"), []).append(it)

    lines = [
        f"# 🎯 竞品/监管定向雷达 · {date_str}",
        "",
        "> 本报告与《情报雷达 v3》(AI技术趋势日报)相互独立 —— 本报告只看**同类竞品"
        "动态**和**中国AI/医疗监管动态**,不含 arXiv/GitHub 等技术前沿内容。",
        f"> 抓取(去重后): **{total_raw} 条** | 精华: **{len(top_items)} 条** | "
        f"分析模型: {model_desc} | 耗时: {elapsed:.0f}s",
        "",
        "---",
        "",
    ]

    for lane in ["竞品", "监管"]:
        items = by_lane.get(lane, [])
        emoji = LANE_EMOJI.get(lane, "📌")
        lines.append(f"## {emoji} {lane} ({len(items)} 条)")
        lines.append("")
        if not items:
            lines.append("_本轮无实质相关信号(已如实标注,未编造)_")
            lines.append("")
            continue
        for it in sorted(items, key=lambda x: -x["score"]):
            stars = "⭐" * it["score"]
            title, url = it["title"], it.get("url", "")
            if url:
                lines.append(f"**[{title}]({url})**")
            else:
                lines.append(f"**{title}**")
            lines.append(f"- 分值: {stars} ({it['score']}/5) | 来源: {it['source']}")
            lines.append(f"- 意味着什么: {it['reason']}")
            lines.append("")
        lines.append("")

    other = by_lane.get("未分类", [])
    if other:
        lines.append(f"## 📌 未分类 ({len(other)} 条)")
        lines.append("")
        for it in sorted(other, key=lambda x: -x["score"]):
            title, url = it["title"], it.get("url", "")
            lines.append(f"- [{title}]({url})" if url else f"- {title}")
        lines.append("")

    lines += ["---", "", "## KPI", "", "| 指标 | 值 |", "|------|-----|"]
    for k, v in sorted(raw_counts.items(), key=lambda x: -x[1]):
        lines.append(f"| 抓取: {k} | {v} 条 |")
    lines += [
        f"| 原始总量(去重后) | **{total_raw} 条** |",
        f"| 精华条数 | **{len(top_items)} 条** |",
        f"| 分析模型 | {model_desc} |",
        f"| 耗时 | {elapsed:.0f} 秒 |",
        "",
    ]
    return "\n".join(lines)


def push_issue(date_str: str, top_items: list, raw_counts: dict,
               total_raw: int, elapsed: float, model_desc: str):
    """用 gh CLI 创建 Issue 到 gufangAI/sync-med。GH_TOKEN 由 Actions 自动注入。
    标题固定带 [情报雷达-竞品监管] 前缀,与日报的 [情报雷达 v3] 明显区分,
    一眼能分清"这是AI趋势"还是"这是竞品/监管情报"。"""
    n_comp = len([i for i in top_items if i.get("lane") == "竞品"])
    n_reg = len([i for i in top_items if i.get("lane") == "监管"])
    title = f"[情报雷达-竞品监管] {date_str} | 竞品 {n_comp} 条 · 监管 {n_reg} 条 · 抓取 {total_raw}"

    body = generate_report(date_str, raw_counts, top_items, total_raw, elapsed, model_desc)
    body += f"\n*自动生成 · 竞品/监管定向雷达 v1 · {date_str}*\n"

    tmp_body = SCRIPT_DIR / f"_tmp_competitor_reg_issue_{date_str}.md"
    tmp_body.write_text(body, encoding="utf-8")

    base_cmd = ["gh", "issue", "create", "--repo", "gufangAI/sync-med",
                "--title", title, "--body-file", str(tmp_body)]
    cmd_with_label = base_cmd + ["--label", "intel-competitor-reg"]

    print(f"\n[Issue] 推送中 ...", flush=True)
    print(f"  标题: {title}", flush=True)
    try:
        result = subprocess.run(cmd_with_label, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"  [WARN] 带 label 创建失败,退化为不带 label 重试: {result.stderr[:200]}", flush=True)
            result = subprocess.run(base_cmd, capture_output=True, text=True, timeout=60)

        if result.returncode == 0:
            issue_url = result.stdout.strip()
            print(f"  [OK] Issue 创建成功: {issue_url}", flush=True)
            summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as f:
                    f.write(f"## 竞品/监管定向雷达 · {date_str}\n\n")
                    f.write(f"- 抓取(去重后): **{total_raw}** 条\n")
                    f.write(f"- 精华: **{len(top_items)}** 条 (竞品{n_comp}/监管{n_reg})\n")
                    f.write(f"- 模型: {model_desc}\n")
                    f.write(f"- Issue: {issue_url}\n")
        else:
            print(f"  [ERROR] gh issue create 失败 (code={result.returncode}):", flush=True)
            print(f"  stdout: {result.stdout[:500]}", flush=True)
            print(f"  stderr: {result.stderr[:500]}", flush=True)
    except subprocess.TimeoutExpired:
        print("  [ERROR] gh issue create 超时", flush=True)
    except FileNotFoundError:
        print("  [ERROR] gh CLI 未安装 (runner 应自带)", flush=True)
    finally:
        try:
            tmp_body.unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="情报雷达 · 竞品/监管定向雷达")
    parser.add_argument("--dry-run", action="store_true", help="只抓取不分析(测试抓取量)")
    parser.add_argument("--top", type=int, default=40, help="精华 TOP N(默认 40)")
    args = parser.parse_args()

    t0 = time.time()
    today = datetime.date.today().strftime("%Y-%m-%d")

    print(f"\n{'=' * 64}")
    print(f"情报雷达 · 竞品/监管定向雷达 · {today}")
    print(f"{'=' * 64}\n")

    raw_counts: dict = {}
    all_items = []

    print("[=== 竞品抓取 ===]", flush=True)
    for label, query in COMPETITOR_QUERIES:
        items = fetch_bucket(label, query, "竞品")
        raw_counts[f"竞品:{label}"] = len(items)
        all_items.extend(items)
        print(f"  [{label}] {len(items)} 条", flush=True)

    print("\n[=== 监管抓取 ===]", flush=True)
    for label, query in REGULATORY_QUERIES:
        items = fetch_bucket(label, query, "监管")
        raw_counts[f"监管:{label}"] = len(items)
        all_items.extend(items)
        print(f"  [{label}] {len(items)} 条", flush=True)

    all_items = dedup_items(all_items)
    total_raw = len(all_items)
    print(f"\n[汇总] 去重后共 {total_raw} 条", flush=True)
    for k, v in raw_counts.items():
        print(f"  {k}: {v}")

    if not all_items:
        print("[错误] 全部抓取失败或为空,退出", flush=True)
        sys.exit(1)

    if args.dry_run:
        elapsed = time.time() - t0
        print(f"\n[--dry-run] 跳过分析,仅输出抓取 KPI")
        print(f"  去重后总量: {total_raw} 条")
        print(f"  耗时: {elapsed:.1f}s")
        return

    use_gateway = False  # GH Actions 云端 runner 无本地网关,与日报 --cloud 模式一致
    if ZHIPU_KEY:
        models = [ZHIPU_MODEL]
        model_desc = f"{ZHIPU_MODEL} (zhipu cloud)"
    elif NVIDIA_KEY:
        models = [NVIDIA_MODEL]
        model_desc = f"{NVIDIA_MODEL} (nvidia)"
    else:
        models = None
        model_desc = "(无可用 LLM)"

    if models:
        raw_picks = analyze_all(all_items, models, use_gateway)
        print(f"\n[分析完成] 总命中 pick: {len(raw_picks)} 条", flush=True)
    else:
        print("[警告] 无可用 LLM key (ZHIPU_API_KEY/NVIDIA_API_KEY 均未配置),跳过筛选,原样列出", flush=True)
        raw_picks = [
            {"index": i + 1, "score": 1, "lane": "未分类", "reason": "未分析(无LLM key)"}
            for i in range(min(len(all_items), args.top))
        ]

    top_items = merge_picks(all_items, raw_picks, top_n=args.top)
    elapsed = time.time() - t0
    print(f"[筛选] 筛出 TOP {len(top_items)} 条 (score>=2),耗时 {elapsed:.1f}s", flush=True)

    report_md = generate_report(today, raw_counts, top_items, total_raw, elapsed, model_desc)
    out_path = REPORTS_DIR / f"{today}_competitor_reg.md"
    out_path.write_text(report_md, encoding="utf-8")
    print(f"\n[完成] 报告写入: {out_path}", flush=True)

    print(f"\n{'=' * 64}")
    print(f"精华预览 (共 {len(top_items)} 条)")
    print(f"{'=' * 64}")
    for i, item in enumerate(top_items[:10], 1):
        stars = "*" * item["score"]
        print(f"{i:2d}. [{item['lane']}] [{stars}] {item['title'][:60]}")
        print(f"     来源: {item['source']} | {item['reason'][:80]}")
        print()

    push_issue(today, top_items, raw_counts, total_raw, elapsed, model_desc)

    return out_path


if __name__ == "__main__":
    main()
