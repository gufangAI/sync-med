# -*- coding: utf-8 -*-
"""
网关链头哨兵 —— 供应商挂了要有人知道，而不是等用户先发现。

立此因（2026-07-31 平台CTO 亲历）：
讯飞 14 把 key 的 chat 模型在某个时刻全部授权失效（AppIdNoAuthError）。
AI 寻脉的链头就是它，一挂就自动落回默认链的 modelscope 8B——而这一档
「面对这套大契约结构给得很瘦」，三个结构化字段整片交白卷。

**故障降级得极其隐蔽**：接口返回 200，JSON 结构完整，免责声明照常挂着，
用户看到的是"古籍里没有找到直接记载"，不是错误页。没有任何告警、
没有 5xx、监控上一片绿。我自己是在质量指标从 8/10 掉到 0/10、
先后怀疑并改了三轮自己的代码、回滚一次之后，才想到去打健康检查的。

CLAUDE.md 第 8 条：「凡是只写进日志的产线，一律视为没人看」。
/api/gateway/health 就是这样一个端点——它一直诚实地报着 xf_qwen 403，
只是没有人会主动去打开它。

它盯什么：链头（寻脉/生成走的那家）与整体可用家数。链头挂 = 立即开 Issue；
可用家数跌破阈值 = 免费池整体在退化，也要报。全绿时不开 Issue，
避免每日噪音把真信号淹掉。

═══════════════════════════════════════════════════════════════════════════
2026-08-28 接上 scripts/gh_issue.py（全仓唯一一份常驻 Issue 实现）

改之前这里手抄了一份"找回 Issue → PATCH"的逻辑（仓里一共四份，各抄各的）。
四份都做对了"复用同一个 Issue"，四份都漏了同一半：

  ① **只 PATCH 正文，从不发评论。而 GitHub 编辑正文不产生任何通知。**
     所以链头挂了这件事，Issue 上确实写着，但没有任何人会被提醒。
     这正是本文件开头引的 CLAUDE.md 第 8 条在说的事 ——
     它自己就掉进了自己要治的那个坑里。

  ② **没有"恢复"这一半。** 全绿时直接 return，于是恢复之后那个写着
     「链头挂了」的 Issue 还一直开着。看的人无从知道它说的是现在还是三周前。
     一个永远开着的告警，等于没有告警。

改成走 gh_issue.upsert 之后：
  · 故障时更新 Issue **并发评论**（真的通知到人）
  · 状态签名没变的轮次静默更新正文 —— 一次持续三天的故障不会刷出 36 条
    几乎相同的评论（每 2 小时一轮），只在**状态变化**那一刻出声
  · 全绿时带 create=False 调一次：有 Issue 就更新成"已恢复"并发通知，
    没有就什么都不做（绝不为了报平安凭空开 Issue）
"""
import os, re, sys, urllib.request

# 本文件的 print 里带 ✅/❌，在 Windows 默认 GBK 终端上会整脚本崩掉，
# 而且崩出来的话是「网关哨兵失败：'gbk' codec can't encode...」——
# 看起来像哨兵报了故障，其实只是本机跑不了。生产在 Actions（Ubuntu/UTF-8）不受影响，
# 但这挡着本地验证。写法照抄仓里既有的那一种（book_health_check.py 等十余处），不另造。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gh_issue import upsert                                   # noqa: E402

SITE     = os.environ.get('GUYAOFANG_SITE', 'https://guyaofang-web.pages.dev')
REPO     = os.environ.get('GITHUB_REPOSITORY', 'hosonzuo8848/sync-med')
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')

# 寻脉链头 —— 与 functions/api/_lib/local_ollama_router.js 的 callWithFallback 首参保持一致。
# 改那边的链头时，这里要同步改，否则哨兵会盯着一个已经没人用的供应商。
HEAD = os.environ.get('XUNMAI_HEAD', 'nvidia')
MIN_HEALTHY = int(os.environ.get('MIN_HEALTHY', '6'))   # 免费池可用家数下限


# 常驻 Issue 的标题前缀 —— 靠它找回同一个 Issue，所以必须稳定。
# 标题其余部分（哪家挂了 / 可用几家 / 已恢复）随状态变，前缀不变。
TITLE_PREFIX = '🔌 '


def provider_table(rows):
    out = ['## 全部供应商', '', '| 供应商 | 状态 | 延迟 | 错误 |', '|---|---|---|---|']
    for r in rows:
        out.append(f"| {r['name']} | {'✅' if r['ok'] else '❌ ' + str(r['status'])} "
                   f"| {r['cost_ms']}ms | {r['error'][:60]} |")
    return out


def main():
    # 必须带正常 UA：urllib 默认发 "Python-urllib/3.11"，会被 CF 的 Bot 防护
    # 直接 403 拦掉（2026-07-31 第一次跑就栽在这，日志里只有一句 HTTP 403 Forbidden，
    # 看不出是被谁拦的——端点本身没有任何鉴权）。
    req = urllib.request.Request(
        f'{SITE}/api/gateway/health',
        headers={'User-Agent': 'gufangai-gateway-sentry/1.0 (+https://www.gufangai.com)',
                 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as r:
        txt = r.read().decode('utf-8', 'replace')

    # 用正则逐条抠而不是整体 json.loads：供应商的 error 字段里常带未转义的引号，
    # 整体解析会挂在某一条上，把其余 20 多家的状态一起丢掉。
    rows = []
    for m in re.finditer(
            r'\{"name":"([^"]+)","ok":(\w+),"status":(\d+),"cost_ms":(\d+)(?:,"error":"(.*?)")?\}', txt):
        n, ok, st, ms, err = m.groups()
        rows.append({'name': n, 'ok': ok == 'true', 'status': int(st),
                     'cost_ms': int(ms), 'error': (err or '')[:160]})

    if not rows:
        print('健康检查没解析出任何供应商 —— 端点可能变了，需要人看', file=sys.stderr)
        sys.exit(1)

    healthy = [r for r in rows if r['ok']]
    head    = next((r for r in rows if r['name'] == HEAD), None)
    head_down = (head is not None and not head['ok'])
    pool_low  = len(healthy) < MIN_HEALTHY

    print(f'供应商 {len(rows)} 家 · 可用 {len(healthy)} 家 · 链头 {HEAD} '
          f'{"❌ " + str(head["status"]) if head_down else "✅" if head else "⚠️ 不在名单里"}')
    for r in rows:
        if not r['ok']:
            print(f"  ❌ {r['name']:14} {r['status']} {r['error'][:70]}")

    # ── 状态签名：决定这一轮要不要出声 ──────────────────────────────────
    # 只装"会改变处置动作"的东西。延迟毫秒数这类每轮都在变的不能进来，
    # 否则签名永远在变、静音档形同虚设。
    sig = []
    if head_down:
        sig.append(f"head_down:{HEAD}:{head['status']}")
    if pool_low:
        sig.append(f'pool_low:{len(healthy)}')
    state = '|'.join(sig) if sig else 'ok'

    if not head_down and not pool_low:
        # 全绿。**不新开 Issue**（create=False），但如果之前开过一个，
        # 把它更新成"已恢复"——fail→ok 是一次状态变化，会自动发出恢复通知。
        # 改之前这里是直接 return，于是恢复后那个写着"挂了"的 Issue 一直开着。
        print(f'链头健康、池子充足（可用 {len(healthy)}/{len(rows)} 家）')
        body = '\n'.join(
            [f'> 数据源：`{SITE}/api/gateway/health`', '',
             '## ✅ 已恢复', '',
             f'链头 **{HEAD}** 正常，免费池可用 **{len(healthy)}/{len(rows)}** 家'
             f'（阈值 {MIN_HEALTHY}）。', ''] + provider_table(rows))
        upsert(REPO, 'gateway', TITLE_PREFIX,
               f'{TITLE_PREFIX}网关已恢复 · 可用 {len(healthy)}/{len(rows)} 家',
               body, token=GH_TOKEN or None, state=state, create=False)
        return

    title = (f'{TITLE_PREFIX}网关链头挂了 · ' + HEAD) if head_down \
        else f'{TITLE_PREFIX}免费池可用家数跌到 {len(healthy)}'
    lines = [f'> 数据源：`{SITE}/api/gateway/health`', '']
    if head_down:
        lines += ['## 🔴 链头不可用', '',
                  f"寻脉/生成走的是 **{HEAD}**，现在 `status={head['status']}`：",
                  '', f"```\n{head['error']}\n```", '',
                  '**注意故障形态**：链头挂了会自动落回默认链的小模型，接口仍返回 200、',
                  'JSON 结构完整，用户看到的是"古籍里没有找到直接记载"而不是错误页——',
                  '监控一片绿，但功能实际已不可用。',
                  '', '处置：换一个健康的链头（改 `local_ollama_router.js` 的 `callWithFallback` 首参），',
                  '或修复该供应商的凭据。', '']
    if pool_low:
        lines += [f'## 🟡 免费池可用 {len(healthy)}/{len(rows)} 家（阈值 {MIN_HEALTHY}）', '']
    lines += provider_table(rows)
    body = '\n'.join(lines)

    if not GH_TOKEN:
        print('无 GITHUB_TOKEN，仅打印：\n' + body); return

    # 走全仓唯一那份实现：复用同一个 Issue + 状态变化时**真的发评论通知到人**。
    upsert(REPO, 'gateway', TITLE_PREFIX, title, body, token=GH_TOKEN, state=state)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('网关哨兵失败：', e, file=sys.stderr)
        sys.exit(1)
