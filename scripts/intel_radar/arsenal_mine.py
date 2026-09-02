#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鹰眼挖掘引擎 —— 让雷达的视野**自己长出来**,不再等于我手写查询式的上限。

立此因(创始人 2026-09-02,连着三句把设计打回重做):
  「你可以想到的有限,这些是要去挖掘的!」
  「虽然模型很强大,可你对话就不可能全覆盖」
  「建好在后台,也可以自己配置搜索的范围」

原来的病(实测,不是推测):
  arsenal_radar.scan_plan() 是一张**我手写死的查询式表**。它今天(09-02)跑完一整轮,
  产出 22 个候选,里面躺着 awesome-nodejs / awesome-godot / awesome-geojson /
  garmin_mcp 这类跟平台毫无关系的东西;而同一时刻:
      firecrawl      175,415★   雷达看不见
      browser-use    112,014★   雷达看不见
      puppeteer       95,536★   雷达看不见
      crawl4ai        80,997★   雷达看不见
      Scrapling       77,867★   雷达看不见
  查询表里 "crawl / scrap / spider / browser" 这几个词**一次都没出现过**——
  整条爬虫赛道不在表里,于是整条赛道在雷达上不存在。
  这不是 bug,是结构:**没写进表里的赛道,永远看不见**。我再手写十条查询,
  也只是把盲区挪个位置。

所以这个模块换一种发现方式:**图遍历**。
  给它几个种子仓,它顺着 GitHub 上真实存在的关系边爬出去,
  边是客观事实(谁在同一张榜单里、谁挂同一个 topic、谁被同一批人 star),
  不需要我预先知道"世界上有爬虫这个赛道"。

三条矿脉(都只用 GitHub 公开 API,零本地算力,可在 Actions 上跑):

  ① 榜单矿脉 mine_awesome()
     awesome-* 仓原本被当成噪声想过滤掉 —— 那是看反了。
     awesome-nodejs 自己没价值,但它 README 里躺着几百个项目链接,**它是矿脉不是垃圾**。
     实测判据(6 个榜单仓 vs 6 个真工具仓,0 误判):
         榜单仓 = language 为空 或 topics 含 awesome-list
         真工具 = 一定有主语言(firecrawl=TypeScript, crawl4ai=Python, litellm=Python)
     且**必须递归子文件**:awesome 榜单常把内容拆成 python.md / javascript.md,
     只读主 README 会漏掉绝大部分(实测 awesome-web-scraping 主 README 只挖出 10 条,
     这正是"我在对话里想不全"的活证据)。

  ② 话题矿脉 mine_topics()
     种子仓的 topics 就是它自报的坐标。firecrawl 挂着 ai-crawler / ai-scraping,
     顺着这两个 topic 搜过去,同赛道的项目自己会浮出来 —— 我不需要事先知道
     "ai-crawler"这个词存在。新挖到的项目的 topics 再扩散一层(BFS),
     视野就从种子长成了一片。

  ③ 同好矿脉 mine_costars()
     star 了 firecrawl 的人,还 star 了什么?这是协同过滤,
     能挖出"同一批人在用、但没有共同 topic 也不在同一张榜单里"的项目 ——
     前两条矿脉都够不到的那一类。**最费配额**,默认关闭,按需开。

配额纪律(GitHub authenticated = 5000 次/小时):
  每条矿脉都有硬上限,总调用数在 run 结束时打印。宁可少挖一轮,不许把配额烧穿
  害得当天的雷达主扫描跑不起来。

红线:纯 stdlib + gh CLI/HTTP,不装浏览器,不跑模型,不写 D1,不碰 R2。
"""
import io
import json
import os
import re
import sys
import time
import base64
import urllib.request
import urllib.error
import urllib.parse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

UA = "gufang-arsenal-mine/1.0"
API = "https://api.github.com"

# 配额闸门。每次真实 API 调用 +1,任何矿脉都不许越过 BUDGET。
_calls = 0
BUDGET = int(os.environ.get("MINE_API_BUDGET", "900"))


class BudgetExhausted(Exception):
    """配额用尽。故意用异常而不是静默返回空 —— 静默返回空会让调用方
    以为"这条矿脉没东西",跟真的没东西分不开,正是本文件要治的那种瞎。"""


def _token():
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "ARSENAL_GH_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return ""


def _headers(accept="application/vnd.github+json"):
    h = {"Accept": accept, "User-Agent": UA}
    t = _token()
    if t:
        h["Authorization"] = "Bearer " + t
    return h


def _get(url, accept="application/vnd.github+json", timeout=40, retries=2):
    """一次 API 调用。计入配额;429/403 退避重试,不硬撞。

    (平台铁律:遇错误码禁止硬重试 —— 这里的退避是读取 GitHub 自己给的
     Retry-After / X-RateLimit-Reset,属于按服务端指示等待,不是盲目重试。)
    """
    global _calls
    if _calls >= BUDGET:
        raise BudgetExhausted("已用 %d 次调用,达到 MINE_API_BUDGET=%d" % (_calls, BUDGET))
    last = None
    for attempt in range(retries + 1):
        _calls += 1
        req = urllib.request.Request(url, headers=_headers(accept))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if accept.endswith("raw"):
                    return raw.decode("utf-8", "replace")
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:                       # noqa: PERF203
            last = e
            if e.code in (403, 429):
                wait = 0
                try:
                    wait = int(e.headers.get("Retry-After") or 0)
                except Exception:                                 # noqa: BLE001
                    wait = 0
                if not wait:
                    try:
                        reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                        wait = max(0, reset - int(time.time())) if reset else 0
                    except Exception:                             # noqa: BLE001
                        wait = 0
                wait = min(wait or 20, 90)
                if attempt < retries:
                    print("    限流,按服务端指示等 %ds" % wait)
                    time.sleep(wait)
                    continue
            if e.code == 404:
                return None
            break
        except Exception as e:                                    # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(2)
                continue
            break
    if isinstance(last, urllib.error.HTTPError) and last.code == 404:
        return None
    return None


def api_calls_used():
    return _calls


# ---------------------------------------------------------------------------
# 矿脉 ① 榜单
# ---------------------------------------------------------------------------

# 实测定的判据(2026-09-02,6 榜单仓 + 6 真工具仓,零误判):
#   awesome-nodejs / awesome-generative-ai / awesome-godot / awesome-geojson /
#   awesome-qt-qml            → language 为空
#   awesome-hacker-search-engines → language=Shell,但 topics 含 awesome-list
#   firecrawl(TypeScript) / crawl4ai(Python) / browser-use(Python) /
#   superpowers(Shell) / MoneyPrinterTurbo(Python) / litellm(Python) → 全部有主语言
# 注意这是**覆盖判断**(它属不属于榜单仓)不是**门槛**,所以靠特征不靠阈值。
AWESOME_TOPICS = {"awesome", "awesome-list", "awesome-lists", "list", "lists",
                  "resources", "collection", "curated-list"}

_GH_LINK = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)")
# 这些是 GitHub 自己的路径段,不是用户名 —— 不过滤会把 github.com/sponsors/xxx
# 这类链接当成项目挖回来。
_NOT_OWNER = {"sponsors", "topics", "features", "about", "pricing", "orgs",
              "settings", "apps", "marketplace", "collections", "explore",
              "search", "notifications", "login", "join", "site", "readme"}
# 仓名尾部常见的非仓库后缀
_STRIP_SUFFIX = (".git", ".md", ".png", ".svg", ".jpg", ".gif", ".json", ".txt")


def is_awesome_repo(meta):
    """这个仓是不是"榜单/资源清单"型。是 → 它本身没有吸收价值,但它是矿脉。"""
    if not meta:
        return False
    topics = {str(t).lower() for t in (meta.get("topics") or [])}
    if topics & AWESOME_TOPICS:
        return True
    if not meta.get("language"):
        name = str(meta.get("name") or "").lower()
        return name.startswith("awesome") or "awesome" in name
    return False


def _clean_repo_ref(s):
    """把 README 里抠出来的 owner/repo 洗成规范形式;不是仓库就返回 None。"""
    s = s.strip().rstrip(".,);:'\"")
    for suf in _STRIP_SUFFIX:
        if s.lower().endswith(suf):
            s = s[: -len(suf)]
    parts = s.split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts[0], parts[1]
    if not owner or not repo:
        return None
    if owner.lower() in _NOT_OWNER:
        return None
    # 形如 owner/repo#L12 或 owner/repo?tab=x 的残尾
    repo = repo.split("#")[0].split("?")[0].strip()
    if not repo or repo in (".", ".."):
        return None
    return owner + "/" + repo


def _repo_markdown_files(repo, max_files=12):
    """列出仓里的 markdown 文件路径。

    **这一步是这条矿脉的成败所在。** awesome 榜单极常把内容拆成子文件
    (awesome-web-scraping 就是 python.md / javascript.md / php.md ...),
    只读主 README 会漏掉绝大多数条目 —— 实测主 README 只挖出 10 条链接,
    而整个仓远不止。用 git tree 一次列全,比逐层 contents 省调用。
    """
    meta = _get("%s/repos/%s" % (API, repo))
    if not meta:
        return []
    branch = meta.get("default_branch") or "main"
    tree = _get("%s/repos/%s/git/trees/%s?recursive=1"
                % (API, repo, urllib.parse.quote(branch, safe="")))
    if not tree or not isinstance(tree.get("tree"), list):
        return []
    mds = []
    for node in tree["tree"]:
        p = str(node.get("path") or "")
        if node.get("type") == "blob" and p.lower().endswith(".md"):
            # 跳过纯流程文件,它们只会带来噪声链接
            base = p.lower().rsplit("/", 1)[-1]
            if base in ("contributing.md", "code_of_conduct.md", "license.md",
                        "changelog.md", "security.md", "code-of-conduct.md"):
                continue
            mds.append(p)
    # 主 README 优先,其余按路径浅到深
    mds.sort(key=lambda p: (0 if p.lower().count("/") == 0 and "readme" in p.lower()
                            else 1, p.count("/"), p))
    return mds[:max_files]


def mine_awesome(seed_lists, max_per_list=12, verbose=True):
    """从榜单仓挖出它推荐的所有项目。

    返回 {repo_full_name: [挖到它的榜单, ...]} —— 被多张榜单同时推荐的项目,
    是比"星数高"更硬的信号,所以来源要留着不能丢。
    """
    found = {}
    for src in seed_lists:
        try:
            paths = _repo_markdown_files(src, max_files=max_per_list)
        except BudgetExhausted:
            if verbose:
                print("  配额用尽,榜单矿脉在 %s 处停下" % src)
            break
        if not paths:
            if verbose:
                print("  %s: 列不出 markdown 文件" % src)
            continue
        hits = set()
        for p in paths:
            try:
                txt = _get("%s/repos/%s/contents/%s"
                           % (API, src, urllib.parse.quote(p)),
                           accept="application/vnd.github.raw")
            except BudgetExhausted:
                break
            if not isinstance(txt, str):
                continue
            for m in _GH_LINK.findall(txt):
                r = _clean_repo_ref(m)
                if r and r.lower() != src.lower():
                    hits.add(r)
        for r in hits:
            found.setdefault(r, []).append(src)
        if verbose:
            print("  %s: %d 个 md 文件 → 挖出 %d 个项目" % (src, len(paths), len(hits)))
    return found


# ---------------------------------------------------------------------------
# 矿脉 ② 话题图
# ---------------------------------------------------------------------------

def mine_topics(seeds, hops=2, per_topic=30, min_stars=500, verbose=True):
    """顺着 topic 边做 BFS。

    种子仓自己挂的 topics 就是它在 GitHub 上的坐标;顺着坐标搜过去,
    同赛道的项目会自己浮出来。**我不需要预先知道 "ai-crawler" 这个词存在** ——
    这正是它和"我手写查询表"的根本区别。
    """
    seen_topics, found = set(), {}
    frontier = list(seeds)
    for hop in range(hops):
        topics = []
        for repo in frontier:
            try:
                meta = _get("%s/repos/%s" % (API, repo))
            except BudgetExhausted:
                if verbose:
                    print("  配额用尽,话题矿脉停在第 %d 跳" % (hop + 1))
                return found
            if not meta:
                continue
            for t in (meta.get("topics") or []):
                t = str(t).lower()
                if t and t not in seen_topics and t not in AWESOME_TOPICS:
                    seen_topics.add(t)
                    topics.append(t)
        if verbose:
            print("  第 %d 跳:%d 个新话题" % (hop + 1, len(topics)))
        nxt = []
        for t in topics:
            q = "topic:%s stars:>%d" % (t, min_stars)
            url = ("%s/search/repositories?q=%s&sort=stars&order=desc&per_page=%d"
                   % (API, urllib.parse.quote(q), per_topic))
            try:
                res = _get(url)
            except BudgetExhausted:
                if verbose:
                    print("  配额用尽,话题矿脉停在话题 %s" % t)
                return found
            for it in ((res or {}).get("items") or []):
                fn = it.get("full_name")
                if not fn or fn in found:
                    continue
                found[fn] = {
                    "stars": it.get("stargazers_count") or 0,
                    "lang": it.get("language"),
                    "topics": it.get("topics") or [],
                    "desc": (it.get("description") or "")[:200],
                    "license": ((it.get("license") or {}).get("spdx_id") or ""),
                    "via_topic": t,
                    "hop": hop + 1,
                }
                nxt.append(fn)
            time.sleep(0.6)   # 搜索接口另有更严的限速,温和些
        # 下一跳只从**本跳挖到的高星项目**继续扩散,否则话题会爆炸
        nxt.sort(key=lambda r: -(found[r]["stars"]))
        frontier = nxt[:8]
        if not frontier:
            break
    return found


# ---------------------------------------------------------------------------
# 矿脉 ③ 同好(协同过滤)
# ---------------------------------------------------------------------------

def mine_costars(seeds, stargazer_sample=25, per_user=40, verbose=True):
    """star 了种子仓的人,还 star 了什么。

    能挖出"同一批人在用、却既不共享 topic 也不在同一张榜单里"的项目 ——
    前两条矿脉够不到的那一类。**配额很贵**(每个种子约 1+N 次调用),默认不开。
    """
    tally = {}
    for repo in seeds:
        try:
            users = _get("%s/repos/%s/stargazers?per_page=%d"
                         % (API, repo, min(stargazer_sample, 100)))
        except BudgetExhausted:
            if verbose:
                print("  配额用尽,同好矿脉停在 %s" % repo)
            break
        if not isinstance(users, list):
            continue
        for u in users[:stargazer_sample]:
            login = (u or {}).get("login")
            if not login:
                continue
            try:
                starred = _get("%s/users/%s/starred?per_page=%d"
                               % (API, urllib.parse.quote(login), min(per_user, 100)))
            except BudgetExhausted:
                if verbose:
                    print("  配额用尽,同好矿脉停在用户 %s" % login)
                return tally
            if not isinstance(starred, list):
                continue
            for s in starred:
                fn = (s or {}).get("full_name")
                if not fn or fn in seeds:
                    continue
                row = tally.setdefault(fn, {"co_star": 0, "stars": s.get("stargazers_count") or 0,
                                            "lang": s.get("language"),
                                            "desc": (s.get("description") or "")[:200]})
                row["co_star"] += 1
        if verbose:
            print("  %s: 采样 %d 人 → 累计 %d 个共现项目" % (repo, min(stargazer_sample, len(users)), len(tally)))
    return tally


# ---------------------------------------------------------------------------
# 矿脉 ④ 现成排行榜(配额效率最高的一条)
# ---------------------------------------------------------------------------

def mine_rankings(sources, verbose=True):
    """吃别人**已经排好序**的榜单文件。

    创始人 2026-09-02 直接甩过来的源,验真后接入:
      EvanLi/Github-Ranking      12,051★ MIT  每日全球 star 排行,Top100/ 下按语言分 md
      OpenGithubs/github-monthly-rank 1,530★  每月飙升榜 top30

    为什么这条最划算:一个 md 文件里躺着 100 个已排好序的项目,**一次 API 调用**
    就全拿到。相比之下话题矿脉挖 2818 个项目烧掉了 234 次调用。
    榜单是别人替我们跑好的全局扫描,不吃白不吃。

    注意仍**不采信文件里写的星数** —— 榜单文件按天/按月更新,数字会过期,
    统一交给 hydrate() 实测。这里只要仓名。
    """
    found = {}
    for src in sources:
        spec = src if isinstance(src, dict) else {"repo": src}
        repo = spec.get("repo")
        paths = spec.get("paths")
        if not repo:
            continue
        try:
            if not paths:
                paths = [p for p in _repo_markdown_files(repo, max_files=30)]
            got = 0
            for p in paths:
                txt = _get("%s/repos/%s/contents/%s" % (API, repo, urllib.parse.quote(p)),
                           accept="application/vnd.github.raw")
                if not isinstance(txt, str):
                    continue
                for m in _GH_LINK.findall(txt):
                    r = _clean_repo_ref(m)
                    if r and r.lower() != repo.lower():
                        found.setdefault(r, []).append(repo)
                        got += 1
            if verbose:
                print("  %s: %d 个榜单文件 → %d 条记录" % (repo, len(paths), got))
        except BudgetExhausted:
            if verbose:
                print("  配额用尽,排行榜矿脉停在 %s" % repo)
            break
    return found


# ---------------------------------------------------------------------------
# 矿脉 ⑤ GitHub Trending(唯一一条要真抓 HTML 的)
# ---------------------------------------------------------------------------

# trending 页面没有官方 API(GitHub 从没开放过),只能解析 HTML。
# 结构:每个条目是 <h2 class="h3 lh-condensed"><a href="/owner/repo">。
# 只认这一个锚点,不做花哨解析 —— 页面改版时宁可挖到 0 条并报出来,
# 也不要用宽松正则把导航链接当项目挖回去。
_TREND_ITEM = re.compile(r'<h2[^>]*class="[^"]*lh-condensed[^"]*"[^>]*>\s*<a[^>]+href="/([^/"]+/[^/"?#]+)"')


def mine_trending(ranges=("daily", "weekly", "monthly"), langs=(None,), verbose=True):
    """抓 github.com/trending。

    创始人 2026-09-02 直接指了这个源。它和排行榜矿脉互补:
    排行榜看的是**存量总星**(老仓占优),trending 看的是**当下涨势**(新仓才露头)。
    只看总星会永远错过刚起来的东西 —— 这正是鹰眼此前"只发现老项目"的另一半原因。

    走 urllib + 环境代理(国内直连 github.com 常超时;Actions 上无需代理)。
    """
    found = {}
    for lang in langs:
        for rng in ranges:
            url = "https://github.com/trending"
            if lang:
                url += "/" + urllib.parse.quote(lang)
            url += "?since=" + rng
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                })
                html = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
            except Exception as e:                                # noqa: BLE001
                if verbose:
                    print("  trending %s/%s 抓取失败:%s" % (lang or "all", rng, str(e)[:60]))
                continue
            hits = set()
            for m in _TREND_ITEM.findall(html):
                r = _clean_repo_ref(m.strip())
                if r:
                    hits.add(r)
            if not hits and verbose:
                # 页面改版的显式信号。静默返回 0 会被当成"今天没有趋势项目",
                # 那正是本文件要治的病。
                print("  trending %s/%s: 解析出 0 条 —— 页面结构可能变了,该查正则"
                      % (lang or "all", rng))
            for r in hits:
                found.setdefault(r, []).append("trending:" + rng + ("/" + lang if lang else ""))
            if verbose and hits:
                print("  trending %s/%s: %d 个" % (lang or "all", rng, len(hits)))
            time.sleep(1.2)
    return found


# ---------------------------------------------------------------------------
# 汇总:把各条矿脉挖到的东西补全元数据、判类、落盘
# ---------------------------------------------------------------------------

def hydrate(repos, verbose=True, cap=400):
    """给挖到的仓名补全真实元数据。

    **star 数一律实测,禁止沿用榜单里写的数字** —— 榜单常年不更新,
    照抄等于把过期数据当事实(平台已有血证:自媒体星数与实测差一倍)。
    """
    out = {}
    todo = list(repos)[:cap]
    for i, r in enumerate(todo, 1):
        try:
            m = _get("%s/repos/%s" % (API, r))
        except BudgetExhausted:
            if verbose:
                print("  配额用尽,补全停在第 %d/%d 个" % (i, len(todo)))
            break
        if not m or m.get("archived"):
            continue
        out[m.get("full_name") or r] = {
            "stars": m.get("stargazers_count") or 0,
            "lang": m.get("language"),
            "topics": m.get("topics") or [],
            "desc": (m.get("description") or "")[:240],
            "license": ((m.get("license") or {}).get("spdx_id") or ""),
            "pushed_at": m.get("pushed_at"),
            "is_list": is_awesome_repo(m),
        }
        if verbose and i % 50 == 0:
            print("  补全 %d/%d(已用 %d 次调用)" % (i, len(todo), _calls))
    return out


# ---------------------------------------------------------------------------
# 配置 + 主流程
# ---------------------------------------------------------------------------

CONFIG = os.path.join(HERE, "mine_config.yml")


def load_config():
    """读挖掘范围配置。**改这个文件就能扩鹰眼视野,不用动代码** —— 这是
    创始人 2026-09-02「也可以自己配置搜索的范围」那条要求的落点。

    配置读不到时**不静默降级**:直接报错退出。静默用内置默认值会让人以为
    "我改的配置生效了",而实际上鹰眼还在按老范围扫 —— 那比崩掉更难发现。
    """
    if not os.path.isfile(CONFIG):
        raise SystemExit("找不到配置 %s —— 挖掘范围没有配置就不该开工" % CONFIG)
    try:
        import yaml
    except ImportError:
        raise SystemExit("缺 pyyaml:pip install pyyaml")
    with io.open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("seeds"):
        raise SystemExit("配置里 seeds 是空的,没有种子就无从挖起")
    return cfg


def score(name, meta, cfg):
    """相关性打分。**只影响排序与呈现,不影响挖掘** ——
    挖要挖全(视野问题),筛是另一回事(注意力问题),两件事混在一起就会
    重演"为了少看垃圾而把 firecrawl 一起滤掉"的老病。
    """
    rel = cfg.get("relevance") or {}
    ours = [w.lower() for w in (rel.get("our_lines") or [])]
    noise = [w.lower() for w in (rel.get("noise") or [])]
    hay = " ".join([
        name.lower(),
        (meta.get("desc") or "").lower(),
        " ".join(str(t).lower() for t in (meta.get("topics") or [])),
    ])
    hit = sum(1 for w in ours if w in hay)
    bad = sum(1 for w in noise if w in hay)
    s = hit * 10 - bad * 25
    if meta.get("is_list"):
        s -= 40                      # 榜单仓本身不是可吸收对象,但它是矿脉,只降权不剔除
    if not meta.get("lang"):
        s -= 15                      # 无主语言 ≈ 文档仓
    st = meta.get("stars") or 0
    if st >= 100000:
        s += 12
    elif st >= 20000:
        s += 8
    elif st >= 3000:
        s += 4
    return s


def main():
    cfg = load_config()
    m = cfg.get("mining") or {}
    global BUDGET
    BUDGET = int(os.environ.get("MINE_API_BUDGET", m.get("api_budget", 900)))

    seeds = []
    for lane, rows in (cfg.get("seeds") or {}).items():
        seeds.extend(rows or [])
    print("鹰眼挖掘引擎 · 种子 %d 个 · 配额上限 %d 次调用" % (len(seeds), BUDGET))

    all_hits = {}
    origin = {}

    # 顺序是按**配额性价比**排的,不是按重要性:先吃一次调用能拿 100 个项目的
    # 现成榜单,再吃 trending(零 API 配额,走 HTML),最后才轮到费配额的搜索类矿脉。
    # 配额万一中途耗尽,损失的是最贵的那条,不是最值钱的那条。
    print("\n⓪ 排行榜矿脉(别人已排好序的榜,一次调用吃一批)")
    try:
        rk = mine_rankings(cfg.get("ranking_sources") or [])
    except BudgetExhausted as e:
        print("  " + str(e)); rk = {}
    for r, srcs in rk.items():
        all_hits.setdefault(r, None)
        origin.setdefault(r, []).append("排行榜:" + srcs[0])
    print("  小计 %d 个项目(已用 %d 次调用)" % (len(rk), _calls))

    print("\n⓪′ Trending 矿脉(不吃 API 配额;看的是涨势不是存量)")
    tr = mine_trending(
        ranges=tuple((cfg.get("trending") or {}).get("ranges") or ["daily", "weekly"]),
        langs=tuple((cfg.get("trending") or {}).get("langs") or [None]))
    for r, srcs in tr.items():
        all_hits.setdefault(r, None)
        origin.setdefault(r, []).append(srcs[0])
    print("  小计 %d 个项目(仍是 %d 次 API 调用)" % (len(tr), _calls))

    print("\n① 榜单矿脉")
    try:
        aw = mine_awesome(cfg.get("awesome_sources") or [],
                          max_per_list=int(m.get("awesome_max_files", 12)))
    except BudgetExhausted as e:
        print("  " + str(e)); aw = {}
    for r, srcs in aw.items():
        all_hits[r] = None
        origin.setdefault(r, []).append("榜单:" + ",".join(srcs[:2]))
    print("  小计 %d 个项目(已用 %d 次调用)" % (len(aw), _calls))

    print("\n② 话题矿脉")
    try:
        tp = mine_topics(seeds, hops=int(m.get("topic_hops", 2)),
                         per_topic=int(m.get("per_topic", 30)),
                         min_stars=int(m.get("topic_min_stars", 500)))
    except BudgetExhausted as e:
        print("  " + str(e)); tp = {}
    for r, meta in tp.items():
        all_hits[r] = meta
        origin.setdefault(r, []).append("话题:" + str(meta.get("via_topic")))
    print("  小计 %d 个项目(已用 %d 次调用)" % (len(tp), _calls))

    if m.get("enable_costars"):
        print("\n③ 同好矿脉")
        try:
            cs = mine_costars(seeds[:4])
        except BudgetExhausted as e:
            print("  " + str(e)); cs = {}
        for r, meta in cs.items():
            if meta.get("co_star", 0) >= 2:
                all_hits.setdefault(r, None)
                origin.setdefault(r, []).append("同好x%d" % meta["co_star"])
        print("  小计 %d 个项目(已用 %d 次调用)" % (len(cs), _calls))
    else:
        print("\n③ 同好矿脉:配置里关着(enable_costars: false),跳过")

    need = [r for r, v in all_hits.items() if not v or "stars" not in (v or {})]
    print("\n④ 补全元数据(%d 个待补,星数一律实测不抄榜单)" % len(need))
    hy = hydrate(need)
    for r, meta in hy.items():
        all_hits[r] = meta
    rows = []
    for r, meta in all_hits.items():
        if not meta or "stars" not in meta:
            continue
        meta = dict(meta)
        meta["repo"] = r
        meta["origin"] = origin.get(r, [])
        meta["score"] = score(r, meta, cfg)
        rows.append(meta)
    rows.sort(key=lambda x: (-x["score"], -(x.get("stars") or 0)))

    outdir = os.path.join(ROOT, "reports", "arsenal")
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    out = os.path.join(outdir, "mined.json")
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump({"mined": rows, "api_calls": _calls,
                   "seeds": seeds, "total": len(rows)},
                  f, ensure_ascii=False, indent=1)

    print("\n══ 挖掘完成 ══")
    print("  挖到 %d 个项目 · 用了 %d 次 API 调用 · 落盘 %s" % (len(rows), _calls, out))
    print("\n  相关性最高的 15 个:")
    for r in rows[:15]:
        print("    %+4d  ★%-8s %-42s %s"
              % (r["score"], r.get("stars"), r["repo"][:42],
                 (r.get("origin") or [""])[0][:26]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
