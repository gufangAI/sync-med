#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly watch on the LATEST RELEASE of our absorb-candidate repos.

Why this exists (2026-08-31, 采集情报线第二缺口·增量监控):
  arsenal_radar already diffs STAR counts week over week, but a star bump is a
  weak, laggy signal. The strong "go look now" signal is a NEW RELEASE -- ragflow
  shipping v0.28, MinerU tagging a new layout model, satori bumping a version we
  vendored. Nothing in intel_radar watched releases (grep-confirmed: zero
  releases/tags calls anywhere). This fills exactly that gap.

Design: a straight clone of noauth_watch.py's proven contract (snapshot -> diff ->
  speak only on change), just pointed at the GitHub Releases API instead of one
  TS file. Same exit codes so it wires into weekly-hunter.yml identically:

Output contract:
  exit 0  -> no new release since last snapshot (stay silent; weekly issue clean)
  exit 10 -> a repo shipped a new release; release_watch.md written with the diff,
             and release_snapshot.json updated (workflow commits it back)
  exit 1  -> every repo failed to fetch (a watch that dies silently is a watch
             nobody has; one or two individual failures are tolerated)

The watch list is the fleet's absorb candidates (RAG / OCR / graph / content /
crawl lines). Keep it curated and small: this is "did the tools we might absorb
ship something", not a firehose.
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "release_snapshot.json")

# 吸收候选仓(五路兵团 2026-08-31 实测锁定的对口开源,按产线分组注释)。
# 只放"出新版=我们该去看"的仓,保持小而准 —— 这不是全网 release 洪流。
WATCH_REPOS = [
    "infiniflow/ragflow",          # RAG 检索
    "HKUDS/LightRAG",              # RAG / 知识图谱实体合并
    "microsoft/graphrag",          # 知识图谱社区
    "castorini/rank_llm",          # RAG reranker
    "PaddlePaddle/PaddleOCR",      # 古籍 OCR 版面
    "opendatalab/MinerU",          # 古籍 OCR 版面结构化
    "RapidAI/RapidOCR",            # 古籍 OCR 文本行
    "adbar/trafilatura",           # 采集正文提取(已吸收,盯它出新版)
    "vercel/satori",               # 内容产线图文卡片
    "dreammis/social-auto-upload",  # 内容产线发布环
    "unclecode/crawl4ai",          # 采集爬虫
    "FlagOpen/FlagEmbedding",      # embedding / reranker 参考(不换供应商,只读趋势)
]


def latest_release(repo):
    """Return (tag, published_at) of the repo's latest release; fall back to the
    newest tag when a repo publishes no formal releases. Raises on hard failure so
    main() can count it toward the all-failed guard."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "IntelRadar/3.0 (SueAI)",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": "Bearer " + os.environ["GH_TOKEN"]}
           if os.environ.get("GH_TOKEN") else {}),
    }
    url = "https://api.github.com/repos/%s/releases/latest" % repo
    try:
        req = urllib.request.Request(url, headers=headers)
        j = json.loads(urllib.request.urlopen(req, timeout=45).read())
        return (j.get("tag_name") or "", j.get("published_at") or "")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        # 404 = 该仓没有正式 release,退回最新 tag(很多活跃仓只打 tag 不发 release)
        turl = "https://api.github.com/repos/%s/tags?per_page=1" % repo
        req = urllib.request.Request(turl, headers=headers)
        arr = json.loads(urllib.request.urlopen(req, timeout=45).read())
        if arr:
            return (arr[0].get("name") or "", "")   # tag 无发布时间
        return ("", "")


def main():
    old = {}
    if os.path.isfile(SNAP):
        try:
            old = json.load(io.open(SNAP, encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            old = {}

    cur, failures = {}, []
    for repo in WATCH_REPOS:
        try:
            tag, pub = latest_release(repo)
            if tag:
                cur[repo] = {"tag": tag, "published_at": pub}
        except Exception as exc:                                 # noqa: BLE001
            failures.append("%s: %s" % (repo, str(exc)[:80]))
            print("  ! %-30s FETCH FAIL %s" % (repo, str(exc)[:80]), flush=True)

    # 每一个都抓失败 = 网络/凭据整体坏了,大声退 1(死掉的 watch 没人看)
    if not cur and failures:
        print("release_watch: ALL %d repos failed to fetch" % len(failures))
        return 1

    # 首见的仓(快照里没有)也算"新",但首跑时全是新会刷屏 —— 首跑只建快照不报警。
    first_run = not old
    added = sorted(r for r in cur if r not in old)
    bumped = sorted(r for r in cur if r in old and cur[r]["tag"] != old[r].get("tag"))

    for repo in WATCH_REPOS:                                     # 保留失败/消失仓的旧快照,别丢
        if repo not in cur and repo in old:
            cur[repo] = old[repo]

    print("release_watch: %d repos checked, %d failed, first_run=%s"
          % (len(WATCH_REPOS), len(failures), first_run), flush=True)

    if first_run:
        json.dump(cur, io.open(SNAP, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1, sort_keys=True)
        print("release_watch: first run -- snapshot seeded, staying silent")
        return 0

    if not (added or bumped):
        print("release_watch: no new release -- staying silent")
        return 0

    lines = ["## 吸收候选仓出新版了(采集情报·增量监控)", ""]
    for repo in bumped:
        lines.append("- **NEW RELEASE** `%s` %s → **%s** (%s)"
                     % (repo, old[repo].get("tag", "?"), cur[repo]["tag"],
                        cur[repo].get("published_at", "")[:10] or "no date"))
    for repo in added:
        lines.append("- **首次纳入监控** `%s` 当前 %s (%s)"
                     % (repo, cur[repo]["tag"], cur[repo].get("published_at", "")[:10] or "no date"))
    lines += ["",
              "新版=可能有我们该吸收的新能力,去 release notes 看变更;红线不变:",
              "只走内部免费池、本地禁算力、绝不换 embedding 供应商。",
              "对表 arsenal_radar(它盯 star,本 watch 盯 release,互补)。"]
    io.open(os.path.join(HERE, "..", "..", "release_watch.md"), "w",
            encoding="utf-8").write("\n".join(lines) + "\n")
    json.dump(cur, io.open(SNAP, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    print("release_watch: %d new release(s), %d newly watched -- wrote release_watch.md"
          % (len(bumped), len(added)))
    return 10


if __name__ == "__main__":
    sys.exit(main())
