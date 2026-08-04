#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ranks 算子 —— 抓 OpenGithubs 飙升榜(日/周/月),拿**星速**这个别处拿不到的信号

立此因(2026-08-04 创始人指定):https://github.com/OpenGithubs/github-monthly-rank

为什么这个源比我自己写检索式强得多:
  ① **它给的是「月 Star 增长量」—— GitHub Search API 根本拿不到这个**。
     官方搜索只能按总星数排,一个五年前的 45 万星老项目永远排在
     一个月涨 5 万星的新爆款前面。**星速才是"正在发生什么"的信号,总星数是历史。**
  ② 榜单是社区维护的,不需要我们预先知道该搜什么词 ——
     关键词搜索的死穴就是"只能搜到你想得到的词"。
  ③ 质量当场见分晓:2026-07 月榜前三里**两个直接打在我们模块上** ——
       DeusData/codebase-memory-mcp (+27265★)「indexes codebases into a
         persistent **knowledge graph**」→ 知识图谱星图
       Panniantong/Agent-Reach (+25798★)「give your AI agent **eyes to see
         the entire internet**」→ 情报雷达
     而同一天我那套关键词检索捞回来的是 freeCodeCamp 和 jettbrains/-L-。

三个榜一起抓,都能回溯到 2024:
    github-daily-rank   top10  每天  8:30 更新
    github-weekly-rank  top20  每周一 8:00
    github-monthly-rank top30  每月 1 号 8:00

存哪(创始人问的):**D1**。
  这是结构化小数据(估算 6000~9000 行 / 3~5MB),要天天查询、去重、关联判定 —— 正是 D1 的活。
  R2 是放大文件的,放这个等于查一次下载解析一次,还违背零 R2 移动铁律;
  123 是冷归档,不适合天天查。
  **原始 markdown 另存一份进 git 仓**(reports/ranks/)—— 解析逻辑将来改了能
  重新解析历史全量,不用重新抓。原文是根,派生可重洗。

铁律:纯采集 + 正则解析,**一个 AI 调用都不发**(零推理成本);只写 gh_repo_pool,不碰别的表。
"""
import os, re, sys, json, time, base64, argparse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q                                       # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SNAP_DIR = os.path.join(REPO_ROOT, "reports", "ranks")

SOURCES = [("OpenGithubs/github-monthly-rank", "monthly"),
           ("OpenGithubs/github-weekly-rank", "weekly"),
           ("OpenGithubs/github-daily-rank", "daily")]

# 条目形如:
#   - **榜单增长第1名 : DietrichGebert/ponytail  **
#       - 开源地址:https://github.com/DietrichGebert/ponytail
#       - 📅 开源时间:2026-06-15
#       - ⭐ 总星标数量:89309⭐
#       - 🔺 月Star增长量:49611⭐
#       - 📝 项目描述: ...
RE_ITEM = re.compile(
    r"第\s*(\d+)\s*名\s*[:：]\s*([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)"      # 名次 + repo
    r"(.*?)(?=第\s*\d+\s*名\s*[:：]|\Z)", re.S)
RE_STARS = re.compile(r"总星标数量\s*[:：]\s*(\d+)")
RE_DELTA = re.compile(r"Star\s*增长量\s*[:：]\s*(\d+)")
RE_BORN = re.compile(r"开源时间\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
RE_DESC = re.compile(r"项目描述\s*[:：]\s*(.*?)(?:\n\s*[-*]|\n\n|\Z)", re.S)


def gh_json(path):
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gufangai-ranks"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"), headers=h)
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def list_files(repo, years):
    """列出该榜所有期次文件(按年目录)。"""
    out = []
    for y in years:
        try:
            for f in gh_json(f"repos/{repo}/contents/{y}"):
                if f.get("type") == "file" and str(f.get("name", "")).endswith(".md"):
                    out.append((y, f["name"], f["path"]))
        except Exception as e:
            if getattr(e, "code", 0) != 404:
                print(f"    列目录失败 {repo}/{y}:{str(e)[:60]}", flush=True)
    return out


def fetch_md(repo, path):
    j = gh_json(f"repos/{repo}/contents/{path}")
    return base64.b64decode(j.get("content", "")).decode("utf-8", "replace")


def parse(md, kind, period):
    """从一期榜单里解析出条目。**纯正则,不问模型。**"""
    rows = []
    for m in RE_ITEM.finditer(md):
        rank, repo, body = int(m.group(1)), m.group(2).strip(), m.group(3)
        if repo.lower().startswith(("http", "www")):
            continue
        st = RE_STARS.search(body)
        dl = RE_DELTA.search(body)
        bo = RE_BORN.search(body)
        de = RE_DESC.search(body)
        rows.append({
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "description": re.sub(r"\s+", " ", (de.group(1) if de else "")).strip()[:600],
            "stars": int(st.group(1)) if st else 0,
            "star_delta": int(dl.group(1)) if dl else 0,
            "born": bo.group(1) if bo else "",
            "rank": rank,
            "found_by": f"rank:{kind}:{period}",
        })
    return rows


def ensure_table():
    d1("""CREATE TABLE IF NOT EXISTS gh_repo_pool (
            repo TEXT PRIMARY KEY, url TEXT, description TEXT, stars INTEGER,
            forks INTEGER, lang TEXT, topics TEXT, license TEXT,
            created_at TEXT, pushed_at TEXT, found_by TEXT,
            first_seen INTEGER, last_seen INTEGER)""")
    # 星速是这个源独有的信号,必须有位置放。ALTER 失败 = 列已存在,忽略。
    for col, typ in (("star_delta", "INTEGER"), ("rank_best", "INTEGER"), ("rank_hits", "INTEGER")):
        try:
            d1(f"ALTER TABLE gh_repo_pool ADD COLUMN {col} {typ}")
            print(f"  [表] 加列 {col}", flush=True)
        except Exception:
            pass


def save(rows, batch=50):
    ensure_table()
    now, n = int(time.time()), 0
    vals = []
    for r in rows:
        vals.append("(" + ",".join([
            q(r["repo"]), q(r["url"]), q(r["description"]), str(r["stars"]),
            q(r["born"]), q(r["found_by"]), str(now), str(now),
            str(r["star_delta"]), str(r["rank"]), "1"]) + ")")
        if len(vals) >= batch:
            n += _flush(vals, now)
            vals = []
    if vals:
        n += _flush(vals, now)
    return n


def _flush(vals, now):
    try:
        d1("INSERT INTO gh_repo_pool (repo,url,description,stars,created_at,found_by,"
           "first_seen,last_seen,star_delta,rank_best,rank_hits) VALUES " + ",".join(vals) +
           " ON CONFLICT(repo) DO UPDATE SET "
           "  stars=MAX(excluded.stars, COALESCE(gh_repo_pool.stars,0)),"
           # 星速取历史最高:一个仓可能上过好几期榜,最高那次才是它的爆发力
           "  star_delta=MAX(excluded.star_delta, COALESCE(gh_repo_pool.star_delta,0)),"
           "  rank_best=MIN(excluded.rank_best, COALESCE(gh_repo_pool.rank_best,999)),"
           # 上榜次数 —— 连着几期都在榜上的,比只闪一次的更值得看
           "  rank_hits=COALESCE(gh_repo_pool.rank_hits,0)+1,"
           "  description=CASE WHEN LENGTH(COALESCE(gh_repo_pool.description,''))<"
           "    LENGTH(excluded.description) THEN excluded.description ELSE gh_repo_pool.description END,"
           f"  last_seen={now}")
        return len(vals)
    except Exception as e:
        print(f"  写库失败({len(vals)} 条):{str(e)[:140]}", flush=True)
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2026,2025,2024", help="回溯哪几年(逗号分隔)")
    ap.add_argument("--kinds", default="monthly,weekly,daily")
    ap.add_argument("--snapshot", action="store_true", help="把原始 markdown 存进 reports/ranks/")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    years = [y.strip() for y in a.years.split(",") if y.strip()]
    kinds = {k.strip() for k in a.kinds.split(",")}
    print(f"=== Ranks 算子 · 榜单={','.join(sorted(kinds))} · 年份={years} · 零 AI 调用 ===", flush=True)

    all_rows, stat = [], {}
    for repo, kind in SOURCES:
        if kind not in kinds:
            continue
        files = list_files(repo, years)
        print(f"\n  [{kind}] {repo}:{len(files)} 期", flush=True)
        got = 0
        for y, name, path in files:
            period = f"{y}-{name.replace('.md','')}"
            try:
                md = fetch_md(repo, path)
            except Exception as e:
                print(f"    取 {period} 失败:{str(e)[:60]}", flush=True)
                continue
            if a.snapshot:
                dst = os.path.join(SNAP_DIR, kind, y)
                os.makedirs(dst, exist_ok=True)
                open(os.path.join(dst, name), "w", encoding="utf-8").write(md)
            rows = parse(md, kind, period)
            got += len(rows)
            all_rows += rows
            time.sleep(0.6)         # 温和,别把 API 配额打满
        stat[kind] = {"periods": len(files), "items": got}
        print(f"  [{kind}] 解析出 {got} 条", flush=True)

    n = save(all_rows)
    try:
        tot = d1("SELECT COUNT(*) c FROM gh_repo_pool")[0]["c"]
        top = d1("SELECT repo,stars,star_delta,rank_hits FROM gh_repo_pool "
                 "WHERE star_delta>0 ORDER BY star_delta DESC LIMIT 10")
    except Exception:
        tot, top = -1, []
    print(f"\n=== 解析 {len(all_rows)} 条 → 写库 {n} → 库内唯一仓 {tot} 个 ===", flush=True)
    print("  星速榜前十(这是 GitHub Search 拿不到的信号):", flush=True)
    for r in top:
        print(f"    +{r['star_delta']:>6}★/期  总{r['stars']:>7}★  上榜{r['rank_hits']}次  {r['repo']}", flush=True)
    if a.report:
        json.dump({"parsed": len(all_rows), "saved": n, "pool_total": tot, "by_kind": stat},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
