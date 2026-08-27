# -*- coding: utf-8 -*-
"""常驻 Issue —— 复用同一个 Issue，并且**真的通知到人**。

═══════════════════════════════════════════════════════════════════════════
为什么要有这一份（2026-08-28 实测）

这段逻辑仓里已经手抄了四遍，每遍都长得几乎一样：

    scripts/workflow_sentry.py:220     labels=pipeline
    scripts/gateway_sentry.py:108      labels=gateway
    scripts/intake_sentry.py:123       labels=intake
    scripts/team_report_audit.py:112   labels=team-audit

四份都做对了「复用同一个 Issue，别每天新开一个」这一半 —— 但四份都漏了另一半：

    **它们只 PATCH 标题和正文，从不发评论。而 GitHub 编辑正文不产生任何通知。**

两条独立路径交叉验证过：
    代码层   四个文件里 "comments" 出现次数   全是 0
    API 层   四个 label 下共 22 个 Issue，评论总数  0

也就是说：这四个哨兵**每天勤勤恳恳更新一个没有人会被提醒的页面**。
配上另一头「自进化控制器每天新开 4 个 Issue、91 个全 open 零回复」，
「出口存在却没人看」这件事的两种死法就凑齐了 ——
一种是新开太多变成墙纸，一种是安静更新根本没人知道。

所以这一份要同时做对两件事：**复用**，且**出声**。

═══════════════════════════════════════════════════════════════════════════
迁移清单（本文件是规范实现，上面四处应逐个换过来）

    [ ] scripts/workflow_sentry.py
    [ ] scripts/gateway_sentry.py
    [ ] scripts/intake_sentry.py
    [ ] scripts/team_report_audit.py
    [x] scripts/evolve_controller.py

四个哨兵是**在跑的生产脚本**，迁移应各自单独一个 PR、各自验证，
不在一次改动里一起动 —— 那正是"一次改一大片，出事说不清是哪一处"的老路。

═══════════════════════════════════════════════════════════════════════════
边界

  · 只碰 GitHub Issue。不碰 D1、不碰生产数据、不做任何部署。
  · 没有 token 时原样跳过并返回 None —— 开 Issue 从来不是致命路径，
    不许它拖垮调用方的主流程。
  · 任何 API 失败都只打印、返回 None，不抛。
"""
import json
import os
import urllib.request


def _api(token, path, method="GET", payload=None, timeout=45):
    """GitHub API。失败返回 None 并打印，绝不抛给调用方。"""
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "gufangai-gh-issue"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as exc:                                     # noqa: BLE001
        print("  [gh_issue] %s %s 失败:%s" % (method, path[:52], str(exc)[:110]),
              flush=True)
        return None


def upsert(repo, label, title_prefix, title, body, token=None, notify=True):
    """维护一个常驻 Issue。

    Args:
        repo:          "owner/name"
        label:         用来找回它的 label，也用于新建时打标
        title_prefix:  匹配用的标题前缀。标题其余部分（时间戳、计数）随便变，
                       前缀必须稳定 —— 找回它靠的就是这个。
        title:         这一轮的完整标题
        body:          这一轮的完整正文
        token:         默认读 GITHUB_TOKEN
        notify:        True 时在更新后追一条评论。**默认 True 是有原因的**：
                       只 PATCH 正文不会给任何人发通知，那等于又变回
                       "只写进日志"。只有确定不需要惊动人时才关掉。

    Returns:
        Issue 的 html_url，或 None（无 token / API 失败）。
    """
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("  [gh_issue] 无 GITHUB_TOKEN,跳过", flush=True)
        return None

    found = None
    listing = _api(token,
                   "/repos/%s/issues?state=open&labels=%s&per_page=100" % (repo, label))
    for it in (listing or []):
        # PR 也会出现在 issues 列表里 —— 必须排掉，否则会把一个 PR 当成常驻 Issue
        # 去 PATCH 它的标题。
        if it.get("pull_request"):
            continue
        if str(it.get("title", "")).startswith(title_prefix):
            found = it
            break

    if found is None:
        created = _api(token, "/repos/%s/issues" % repo, "POST",
                       {"title": title, "body": body, "labels": [label]})
        if created:
            print("  [gh_issue] 常驻 Issue 不存在,已新建 #%s" % created.get("number"),
                  flush=True)
        return (created or {}).get("html_url")

    num = found["number"]
    updated = _api(token, "/repos/%s/issues/%d" % (repo, num), "PATCH",
                   {"title": title, "body": body})
    if notify:
        _api(token, "/repos/%s/issues/%d/comments" % (repo, num), "POST",
             {"body": body})
    print("  [gh_issue] 已更新常驻 Issue #%d%s"
          % (num, "" if notify else "(静默,未发通知)"), flush=True)
    return (updated or found).get("html_url")
