#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trending 算子 —— **月榜全量入库**,不靠关键词猜

立此因(2026-08-04 创始人):
  「你现在还用落后的关键字搜索?那怎么可以,不如**直接看月榜,把所有的搜集到我们的后台**,
    按照那来内部消化!」
  「获取信息,之前不是给了你 GitHub 上一些优秀的应用,那获取的信息效果更好」

关键词搜索的死穴(前一版就栽在这):
  **你只能搜到你想得到的词。想不到的,永远进不了视野。**
  而且 GitHub 仓库搜索是关键词 AND 匹配,写长一点就直接搜不到 ——
  实测 28 条检索式只捞回 13 个仓,还多是垃圾。
  更根本的是:靠模型写检索式 = 把鹰眼的视野押在"它这次发挥得怎么样"上。

月榜的路子不一样:**不需要预先知道要找什么**。
  GitHub Search 支持**纯限定符查询**(一个关键词都不带),实测:
    created:>近30天 stars:>100   → 1124 个   ← 本月新爆款,最有价值的一路
    pushed:>近30天  stars:>500   → 33789 个  ← 老项目里还在快速前进的
  单条查询上限 1000 条,所以**按星段切片**绕过去,每段都能取完。

全量进我们自己的库(gh_repo_pool),**采集端不做任何筛选** ——
  筛选在后台内部消化(adopt.py 的两级筛),这样:
    ① 采集端不丢东西,今天用不上的明天可能用得上;
    ② 筛选判据改了可以**重新消化历史全量**,不用重新采集;
    ③ 两条路线(月榜 / 缺口驱动)的采纳率可以直接对比。

铁律:GitHub Search 用 Actions 自带 GITHUB_TOKEN(零成本、限流 30 次/分);
      本算子**一个 AI 调用都不发**(纯采集,零推理成本)。
"""
import os, sys, json, time, argparse, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q                                       # noqa: E402

# 星段切片 —— 每段的 total 都要压在 1000 以下,否则取不完。
#   实测 created:>30d 各段规模:100..200 最大,>2000 很少。
STAR_BANDS = ["100..200", "200..500", "500..1000", "1000..3000", ">3000"]


def gh(q_str, page=1, per_page=100):
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc"
           "&per_page=%d&page=%d" % (urllib.parse.quote(q_str), per_page, page))
    h = {"Accept": "application/vnd.github+json", "User-Agent": "gufangai-trending"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    for attempt in range(3):
        try:
            j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=45).read())
            return j.get("total_count", 0), (j.get("items") or [])
        except Exception as e:
            code = getattr(e, "code", 0)
            if code in (403, 429):          # 限流:退避重试,别放弃这一段
                nap = 20 * (attempt + 1)
                print(f"    限流 {code},退避 {nap}s", flush=True)
                time.sleep(nap)
                continue
            print(f"    查询失败({str(e)[:60]}):{q_str[:60]}", flush=True)
            return 0, []
    return 0, []


def ensure_table():
    d1("""CREATE TABLE IF NOT EXISTS gh_repo_pool (
            repo TEXT PRIMARY KEY, url TEXT, description TEXT, stars INTEGER,
            forks INTEGER, lang TEXT, topics TEXT, license TEXT,
            created_at TEXT, pushed_at TEXT, found_by TEXT,
            first_seen INTEGER, last_seen INTEGER)""")


def sweep(days=30, tracks=("created", "pushed"), max_pages=10, per_page=100):
    """把月榜全量扫下来。返回 {repo: row}。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    got = {}
    for track in tracks:
        # created = 本月新出现的爆款(最有价值);pushed = 老项目里还在快速前进的
        floor = "stars:>500" if track == "pushed" else "stars:>100"
        bands = STAR_BANDS if track == "created" else ["500..1000", "1000..5000", ">5000"]
        for band in bands:
            qs = f"{track}:>{since} stars:{band}" if band != floor else f"{track}:>{since} {floor}"
            total, _ = gh(qs, page=1, per_page=1)
            pages = min(max_pages, (total + per_page - 1) // per_page) if total else 0
            print(f"  [{track} {band}] total={total} → 取 {pages} 页", flush=True)
            for p in range(1, pages + 1):
                _, items = gh(qs, page=p, per_page=per_page)
                for it in items:
                    name = it.get("full_name")
                    if not name:
                        continue
                    prev = got.get(name)
                    got[name] = {
                        "repo": name, "url": it.get("html_url"),
                        "description": (it.get("description") or "")[:600],
                        "stars": it.get("stargazers_count") or 0,
                        "forks": it.get("forks_count") or 0,
                        "lang": it.get("language") or "",
                        "topics": ",".join(it.get("topics") or [])[:300],
                        "license": ((it.get("license") or {}) or {}).get("spdx_id") or "",
                        "created_at": (it.get("created_at") or "")[:10],
                        "pushed_at": (it.get("pushed_at") or "")[:10],
                        "found_by": (prev or {}).get("found_by") or f"trending:{track}",
                    }
                if not items:
                    break
                time.sleep(2.2)          # search 限流 30/分,温和一点
    return got


def save(rows, batch=60):
    """全量写进我们自己的后台。**采集端不筛**,筛在内部消化那一步。"""
    ensure_table()
    now = int(time.time())
    vals, n = [], 0
    for r in rows.values():
        vals.append("(" + ",".join([
            q(r["repo"]), q(r["url"]), q(r["description"]), str(r["stars"]), str(r["forks"]),
            q(r["lang"]), q(r["topics"]), q(r["license"]), q(r["created_at"]), q(r["pushed_at"]),
            q(r["found_by"]), str(now), str(now)]) + ")")
        if len(vals) >= batch:
            n += _flush(vals, now)
            vals = []
    if vals:
        n += _flush(vals, now)
    return n


def _flush(vals, now):
    try:
        # 老行只更新星数与 last_seen,**first_seen 不动** —— 那是"我们什么时候第一次看见它",
        #   星速要靠这个算,覆盖掉就丢了时间维度。
        d1("INSERT INTO gh_repo_pool (repo,url,description,stars,forks,lang,topics,license,"
           "created_at,pushed_at,found_by,first_seen,last_seen) VALUES " + ",".join(vals) +
           f" ON CONFLICT(repo) DO UPDATE SET stars=excluded.stars, forks=excluded.forks, "
           f"description=excluded.description, topics=excluded.topics, "
           f"pushed_at=excluded.pushed_at, last_seen={now}")
        return len(vals)
    except Exception as e:
        print(f"  写库失败({len(vals)} 条):{str(e)[:130]}", flush=True)
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="回看几天(30=月榜)")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    print(f"=== Trending 算子 · 月榜全量({a.days} 天)· 零 AI 调用 ===", flush=True)
    rows = sweep(days=a.days, max_pages=a.max_pages)
    n = save(rows)
    try:
        tot = d1("SELECT COUNT(*) c FROM gh_repo_pool")[0]["c"]
        fresh = d1(f"SELECT COUNT(*) c FROM gh_repo_pool WHERE first_seen>={int(time.time())-3600}")[0]["c"]
    except Exception:
        tot = fresh = -1
    print(f"\n=== 本轮扫到 {len(rows)} 个,写库 {n} 条 → 后台累计 {tot} 个(本轮新增 {fresh})===", flush=True)
    for r in sorted(rows.values(), key=lambda x: -x["stars"])[:10]:
        print(f"  {r['stars']:6d}★ {r['repo'][:44]:44s} {r['description'][:50]}", flush=True)
    if a.report:
        json.dump({"swept": len(rows), "saved": n, "pool_total": tot, "new": fresh},
                  open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
