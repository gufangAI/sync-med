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

    [ ] scripts/workflow_sentry.py     每 6 小时
    [ ] scripts/gateway_sentry.py      每 2 小时  ← 频率最高，最不能每轮都吵
    [ ] scripts/intake_sentry.py       每 4 小时
    [ ] scripts/team_report_audit.py   每天
    [x] scripts/evolve_controller.py

四个哨兵是**在跑的生产脚本**，迁移应各自单独一个 PR、各自验证，
不在一次改动里一起动 —— 那正是"一次改一大片，出事说不清是哪一处"的老路。

⚠️ **迁移时必须传 state=**，别直接用默认的"每轮都发评论"。

  （2026-08-28 更正我自己上一条 commit 里说过头的话：我当时写"光 gateway 一个
    就是 360 条/月"，那是**故障持续不修**时的上限，不是常态 —— gateway_sentry
    全绿时本来就直接 return、根本不碰 Issue。但方向没变，反而更该治：
    一次持续三天的链头故障，按"每轮都发"就是 **36 条内容几乎相同的评论**，
    而它们要传达的信息只有一条。真正需要出声的是"**状态变了**"那一刻。）

state 传一个能代表健康度的短签名即可（如 `ok` / `head_down:nvidia`），
稳定时静默更新正文、变化时才出声。恢复也是一次变化 —— fail→ok 会自动
发一条"已恢复"，这是原来那四份都没有的能力（它们恢复后只是不再更新，
一个写着"挂了"的 Issue 就那么一直开着，比不报还误导人）。

═══════════════════════════════════════════════════════════════════════════
边界

  · 只碰 GitHub Issue。不碰 D1、不碰生产数据、不做任何部署。
  · 没有 token 时原样跳过并返回 None —— 开 Issue 从来不是致命路径，
    不许它拖垮调用方的主流程。
  · 任何 API 失败都只打印、返回 None，不抛。
"""
import json
import os
import re
import urllib.request

# 把"这一轮的状态"藏进正文里的一个 HTML 注释:渲染出来看不见，但下一轮取回正文就能读到。
# 为什么不另找地方存：多一个存储就多一处会和 Issue 本身漂移的东西，而正文**必然**
# 跟着 Issue 走 —— Issue 被删了状态也就该没了，这正是我们想要的语义。
_STATE_MARK = "<!-- gh_issue-state: %s -->"
_RE_STATE = re.compile(r"<!--\s*gh_issue-state:\s*(.*?)\s*-->", re.S)


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


def upsert(repo, label, title_prefix, title, body, token=None, notify=True, state=None,
           create=True):
    """维护一个常驻 Issue。

    Args:
        repo:          "owner/name"
        label:         用来找回它的 label，也用于新建时打标
        title_prefix:  匹配用的标题前缀。标题其余部分（时间戳、计数）随便变，
                       前缀必须稳定 —— 找回它靠的就是这个。
        title:         这一轮的完整标题
        body:          这一轮的完整正文
        token:         默认读 GITHUB_TOKEN
        notify:        允不允许发评论。False = 绝对静音。
                       **默认 True 是有原因的**：只 PATCH 正文不会给任何人发通知，
                       那等于又变回"只写进日志"。
        state:         这一轮的**状态签名**（短字符串，如 "ok" / "fail:3"）。
                       给了它就切换成"变了才出声"：签名和上一轮相同 → 静默更新正文；
                       不同 → 更新 + 发评论。不给（None）= 每轮都发。
        create:        找不到常驻 Issue 时要不要新建。默认 True。
                       传 False 用于**报平安那一路**：哨兵全绿时也想把 Issue 更新成
                       "已恢复"（这样 fail→ok 能发出一条恢复通知），但绝不该为了
                       报平安而凭空开一个 Issue —— 那就是纯噪音。
                       所以"有就更新、没有就算了"。

    Returns:
        Issue 的 html_url，或 None（无 token / API 失败）。

    ── 为什么需要 state 这一档（2026-08-28）────────────────────────────────
    四个待迁移哨兵的实际频率：
        gateway-sentry   每 2 小时   → 12 条/天
        intake-sentry    每 4 小时   →  6 条/天
        workflow-sentry  每 6 小时   →  4 条/天
        team-report-audit 每天       →  1 条/天
    照"每轮都发评论"迁过去，光 gateway 一个就是 **360 条/月堆在同一个 Issue 上**。
    那不是把哨兵救活，是把它变成另一种墙纸 —— 而且被静音之后比现在更死：
    现在是"没人被通知"，那时是"所有人都主动屏蔽了这个 Issue"。

    所以正确的判据不是"跑了没有"，是"**有没有变化 / 是不是坏的**"。
    稳定绿的那些轮次照常更新正文（要查随时能查），但不吵人。
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

    # 状态签名藏进正文,供下一轮比对
    new_body = body if state is None else (body + "\n\n" + _STATE_MARK % state)

    if found is None:
        if not create:
            print("  [gh_issue] 没有常驻 Issue,且 create=False → 不新建", flush=True)
            return None
        created = _api(token, "/repos/%s/issues" % repo, "POST",
                       {"title": title, "body": new_body, "labels": [label]})
        if created:
            print("  [gh_issue] 常驻 Issue 不存在,已新建 #%s" % created.get("number"),
                  flush=True)
        # 新建本身就会通知订阅者,不再追评论
        return (created or {}).get("html_url")

    num = found["number"]
    # 变了才出声。取不到旧签名(第一次带 state 跑 / 正文被人手改过)按"变了"处理 ——
    # 宁可多吵一次,也不要在真出事那次因为读不到旧值而静音。
    if state is None:
        should_notify = notify
        why = ""
    else:
        m = _RE_STATE.search(str(found.get("body") or ""))
        prev = m.group(1) if m else None
        changed = (prev != state)
        should_notify = notify and changed
        why = "(状态未变:%s)" % state if not changed else "(状态 %s → %s)" % (prev, state)

    updated = _api(token, "/repos/%s/issues/%d" % (repo, num), "PATCH",
                   {"title": title, "body": new_body})
    if should_notify:
        _api(token, "/repos/%s/issues/%d/comments" % (repo, num), "POST",
             {"body": body})
    print("  [gh_issue] 已更新常驻 Issue #%d %s%s"
          % (num, "已发通知" if should_notify else "静默", why), flush=True)
    return (updated or found).get("html_url")
