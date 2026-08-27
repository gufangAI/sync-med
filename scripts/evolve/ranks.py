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
# 2026-08-27 修:冒号后原本是 `\s*`,而 `\s` 含换行 —— 描述为空时它跨行吃到
# 下一条目的标题(诊断实证:抓到 "- **榜单增长第2名 : stablyai/orca")。
# 改 `[ \t]*` 不跨行,并以行尾为界:描述本来就是单行的。
RE_DESC = re.compile(r"项目描述\s*[:：][ \t]*(.*?)(?:\n|\Z)")

# ── 日榜 / 周榜:markdown 表格 + 详情块(2026-08-27 新增)────────────────────
# 月榜是「第N名」句式(上面的 RE_ITEM);日榜周榜是表格,句式完全不同,
# 所以此前即便目录修好了也解析不出来 —— 两处都要补,只补一处等于没补。
#
# 新格式(2024-10 起,实测 27 期抽样 100% 命中):
#   | 1 |  [tt-a1i/archify](https://github.com/tt-a1i/archify)| 17.5k  | 🔺1413 |
# 详情块里还有三个时间尺度的增量,这是别处拿不到的信号:
#   - 🔺 日增长数量:1413⭐   - 🔺 上周增长数量:3083⭐   - 🔺 上月增长数量:10601⭐
RE_ROW = re.compile(
    r"\|\s*(\d+)\s*\|\s*\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\]\([^)]*\)\|"
    r"\s*([0-9.]+[kKmM]?)\s*\|\s*[^\d|]*(\d+)\s*\|")
# 旧格式(2024-09 前,表头多一列 avatar;有日增+周增+开源时间,缺月增)——
# 按创始人红线「格式受限的不许直接划掉」,它同样入库,月增留空即可。
RE_ROW_OLD = re.compile(
    r"\|\s*(?:🥇|🥈|🥉|(\d+))\s*\|[^|]*\|\s*\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\]\([^)]*\)\|"
    r"\s*([0-9.]+[kKmM]?)\s*\|\s*[^\d|]*(\d+)\s*\|\s*[^\d|]*(\d+)\s*\|\s*(\d{4}-\d{2}-\d{2})?")
RE_DET_BLOCK = re.compile(
    r"https://github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)</span>(.*?)(?=<h3|\Z)", re.S)
RE_D_DELTA = re.compile(r"日增长数量\s*[:：]\s*(\d+)")
RE_W_DELTA = re.compile(r"(?:上周增长数量|周Star增长量)\s*[:：]\s*(\d+)")
RE_M_DELTA = re.compile(r"(?:上月增长数量|月Star增长量)\s*[:：]\s*(\d+)")


def _kstar(txt):
    """把 "17.5k" / "1.2m" / "8930" 还原成整数。

    注意:总星数在表格里是缩写(精度有损),增量 🔺1413 才是精确整数 ——
    所以判据一律只用增量,总星数只用于展示排序。
    """
    t = str(txt).strip().lower()
    try:
        if t.endswith("k"):
            return int(float(t[:-1]) * 1000)
        if t.endswith("m"):
            return int(float(t[:-1]) * 1000000)
        return int(float(t))
    except Exception:
        return 0


def parse_table(md, kind, period):
    """解析日榜/周榜(表格式)。返回与 parse() 同构的行,外加三尺度增量。"""
    out = {}
    for m in RE_ROW.finditer(md):
        rank, repo, star, delta = int(m.group(1)), m.group(2), _kstar(m.group(3)), int(m.group(4))
        out[repo] = {"repo": repo, "url": f"https://github.com/{repo}", "description": "",
                     "stars": star, "star_delta": delta, "born": "", "rank": rank,
                     "found_by": f"rank:{kind}:{period}",
                     "d_delta": delta if kind == "daily" else 0,
                     "w_delta": delta if kind == "weekly" else 0, "m_delta": 0}
    if not out:                       # 新格式没命中 → 试旧格式(2024-09 前)
        for i, m in enumerate(RE_ROW_OLD.finditer(md), 1):
            repo = m.group(2)
            out[repo] = {"repo": repo, "url": f"https://github.com/{repo}", "description": "",
                         "stars": _kstar(m.group(3)), "star_delta": int(m.group(4)),
                         "born": m.group(6) or "", "rank": int(m.group(1) or i),
                         "found_by": f"rank:{kind}:{period}",
                         "d_delta": int(m.group(4)), "w_delta": int(m.group(5)), "m_delta": 0}
    # 详情块补齐三尺度增量 + 生日 + 描述
    for m in RE_DET_BLOCK.finditer(md):
        repo, body = m.group(1), m.group(2)
        r = out.get(repo)
        if r is None:
            continue
        for key, rx in (("d_delta", RE_D_DELTA), ("w_delta", RE_W_DELTA), ("m_delta", RE_M_DELTA)):
            g = rx.search(body)
            if g:
                r[key] = int(g.group(1))
        g = RE_BORN.search(body)
        if g:
            r["born"] = g.group(1)
        g = RE_DESC.search(body)
        if g:
            r["description"] = re.sub(r"\s+", " ", g.group(1)).strip()[:600]
    return list(out.values())


def gh_json(path):
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gufangai-ranks"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"), headers=h)
    return json.loads(urllib.request.urlopen(req, timeout=45).read())


def list_files(repo, years):
    """列出该榜所有期次文件。

    【2026-08-27 修复·此前 23 天日榜周榜恒为 0 期】
    三个榜的目录深度不一样,旧实现只看一层,于是:
        月榜 2026/01.md          → 一层就是 .md   ✅ 8 期进来了
        周榜 2026/01/20260803.md → 一层只有目录   ❌ 0 期
        日榜 2026/08/20260826.md → 一层只有目录   ❌ 0 期
    实测被丢掉的量:日榜 886 期 + 周榜 169 期(2024-01 起),只吃进月榜 8 期 = 0.74%。
    而日榜每条同时给「日增/周增/月增/开源时间」——**这是星速信号的唯一来源**,
    丢了它,排序就只能退回总星数,于是雷达永远推老牌大项目(实证:tesseract 上了今日 TOP3)。
    修法:年目录下遇到子目录再列一层(只递归一层,榜单结构不会更深)。
    """
    out = []
    for y in years:
        try:
            entries = gh_json(f"repos/{repo}/contents/{y}")
        except Exception as e:
            if getattr(e, "code", 0) != 404:
                print(f"    列目录失败 {repo}/{y}:{str(e)[:60]}", flush=True)
            continue
        for f in entries:
            t, nm = f.get("type"), str(f.get("name", ""))
            if t == "file" and nm.endswith(".md"):
                out.append((y, nm, f["path"]))
            elif t == "dir":
                try:
                    for g in gh_json(f"repos/{repo}/contents/{y}/{nm}"):
                        gn = str(g.get("name", ""))
                        if g.get("type") == "file" and gn.endswith(".md"):
                            out.append((y, gn, g["path"]))
                except Exception as e:
                    print(f"    列子目录失败 {repo}/{y}/{nm}:{str(e)[:60]}", flush=True)
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


# 一期榜单里,两套句式可能**同时存在**(周榜:详情段 3 条 + 表格 20 条)。
# 「谁先出货用谁」会漏掉 17 条 —— 自测实证。所以两套都跑、按 repo 合并:
# 表格给全量与名次,详情段给更完整的字段(三尺度增量 / 生日 / 描述)。
_DELTA_FIELD = {"daily": "d_delta", "weekly": "w_delta", "monthly": "m_delta"}


def parse_period(md, kind, period):
    """解析一期榜单,返回合并、补齐尺度字段后的行。"""
    merged = {}
    for r in parse_table(md, kind, period) + parse(md, kind, period):
        cur = merged.setdefault(r["repo"], {})
        for k, v in r.items():
            # 只用"更有信息量"的值覆盖:非空、非零、更长的描述
            if v in (None, "", 0):
                continue
            if k == "description" and len(str(v)) <= len(str(cur.get(k, ""))):
                continue
            cur[k] = v
        cur["repo"] = r["repo"]
    out = []
    for repo, r in merged.items():
        r.setdefault("url", f"https://github.com/{repo}")
        r.setdefault("description", "")
        r.setdefault("born", "")
        r.setdefault("stars", 0)
        r.setdefault("rank", 999)
        r.setdefault("found_by", f"rank:{kind}:{period}")
        r["period"], r["kind"] = period, kind
        # parse() 只填 star_delta,而它的语义随榜而变:月榜=月增、周榜=周增、日榜=日增。
        # 不做这一步映射,轨迹表里三个尺度会全是 0(自测实证)。
        fld = _DELTA_FIELD.get(kind)
        if fld and not r.get(fld):
            r[fld] = r.get("star_delta") or 0
        for k in ("d_delta", "w_delta", "m_delta"):
            r.setdefault(k, 0)
        r["star_delta"] = r.get("star_delta") or r.get(fld) or 0
        out.append(r)
    out.sort(key=lambda x: x.get("rank") or 999)
    return out


def ensure_growth_table():
    """轨迹表:一条观测一行,永不覆盖。

    立此因(2026-08-27 全库体检):gh_repo_pool 是 repo 做主键的**覆盖式**表 ——
    今天的星数覆盖昨天的,7,131 个仓里 96% 我们天天在看,却每天把看到的数字扔掉。
    结果是「这周它是在涨还是在跌」这个问题,数据层根本答不出来。
    范式抄 gw_health(设计对、只是没人写):(实体, 期次) 复合主键 + 只 INSERT。
    """
    d1("""CREATE TABLE IF NOT EXISTS gh_growth (
            repo TEXT NOT NULL, period TEXT NOT NULL, kind TEXT NOT NULL,
            rank INTEGER, stars INTEGER,
            d_delta INTEGER, w_delta INTEGER, m_delta INTEGER,
            born TEXT, descr TEXT, ingested_at INTEGER,
            PRIMARY KEY (repo, period, kind))""")
    try:
        d1("CREATE INDEX IF NOT EXISTS idx_gh_growth_period ON gh_growth(period)")
    except Exception as e:
        print(f"  [表] gh_growth 建索引失败:{str(e)[:80]}", flush=True)


def save_growth(rows, batch=60):
    """把每一期的观测追加进 gh_growth。历史快照不会变,冲突即跳过 = 回填/重跑幂等。"""
    ensure_growth_table()
    now, n = int(time.time()), 0
    vals = []
    for r in rows:
        vals.append("(" + ",".join([
            q(r["repo"]), q(r.get("period", "")), q(r.get("kind", "")),
            str(r.get("rank") or 0), str(r.get("stars") or 0),
            str(r.get("d_delta") or 0), str(r.get("w_delta") or 0), str(r.get("m_delta") or 0),
            q(r.get("born", "")), q((r.get("description") or "")[:300]), str(now)]) + ")")
        if len(vals) >= batch:
            n += _flush_growth(vals)
            vals = []
    if vals:
        n += _flush_growth(vals)
    return n


def _flush_growth(vals):
    try:
        d1("INSERT INTO gh_growth (repo,period,kind,rank,stars,d_delta,w_delta,m_delta,"
           "born,descr,ingested_at) VALUES " + ",".join(vals) +
           " ON CONFLICT(repo,period,kind) DO NOTHING")
        return len(vals)
    except Exception as e:
        print(f"  [轨迹] 写库失败({len(vals)} 条):{str(e)[:130]}", flush=True)
        return 0


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
    # 日榜有 886 期(2024 起),每天全量重扫是浪费:--recent N 只取每个榜最新 N 期。
    # 回填历史用 --recent 0(全量)+ --years 2026,2025,2024,跑一次即可;
    # 轨迹表主键冲突时 DO NOTHING,所以重跑幂等、可随时中断续跑。
    ap.add_argument("--recent", type=int, default=0, help="每个榜只取最新 N 期(0=全量)")
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
        if a.recent > 0:
            files = sorted(files, key=lambda t: (t[0], t[1]), reverse=True)[:a.recent]
        print(f"\n  [{kind}] {repo}:{len(files)} 期", flush=True)
        if not files:
            # 哨兵:2026-08-27 之前这里恒为 0 却一声不吭,整整 23 天没人发现
            # (404 被 list_files 静默吞 + workflow 那行 `|| echo 失败不致命` 两层盖住)。
            print(f"  [{kind}] ⚠️ 零期次 —— 上游目录结构可能又变了,或 years 参数不对。"
                  f"这不是正常状态,请查 {repo}/{years}", flush=True)
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
            rows = parse_period(md, kind, period)
            got += len(rows)
            all_rows += rows
            time.sleep(0.6)         # 温和,别把 API 配额打满
        if files and got == 0:
            # 有期次却零解析 = 上游改版式了。原文已 --snapshot 存仓,解析逻辑改了能重洗。
            print(f"  [{kind}] WARN {len(files)} 期全部解析为 0 条 —— 上游格式疑似变更,"
                  f"两套解析器都没命中,请人工看一期原文", flush=True)
        stat[kind] = {"periods": len(files), "items": got}
        print(f"  [{kind}] 解析出 {got} 条", flush=True)

    n = save(all_rows)
    # 轨迹表(纯加法,不影响 gh_repo_pool 的任何下游):每期一行,保住历史。
    gn = save_growth(all_rows)
    print(f"  [轨迹] gh_growth 追加 {gn} 条观测", flush=True)
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
        # by_kind 的期次/条数必须进报告 —— workflow 侧据此判定要不要开 Issue,
        # 治「只写进日志的产线一律视为没人看」(CLAUDE.md 第 8 条)。
        empty = [k for k, v in stat.items() if v["items"] == 0]
        json.dump({"parsed": len(all_rows), "saved": n, "growth_saved": gn,
                   "pool_total": tot, "by_kind": stat, "empty_kinds": empty},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
