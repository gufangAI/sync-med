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
"""
import os, json, re, sys, urllib.request

SITE     = os.environ.get('GUYAOFANG_SITE', 'https://guyaofang-web.pages.dev')
REPO     = os.environ.get('GITHUB_REPOSITORY', 'gufangAI/sync-med')
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')

# 寻脉链头 —— 与 functions/api/_lib/local_ollama_router.js 的 callWithFallback 首参保持一致。
# 改那边的链头时，这里要同步改，否则哨兵会盯着一个已经没人用的供应商。
HEAD = os.environ.get('XUNMAI_HEAD', 'nvidia')
MIN_HEALTHY = int(os.environ.get('MIN_HEALTHY', '6'))   # 免费池可用家数下限


def gh(method, path, payload=None):
    req = urllib.request.Request(
        f'https://api.github.com{path}',
        data=json.dumps(payload).encode() if payload else None,
        headers={'Authorization': f'Bearer {GH_TOKEN}',
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'},
        method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b'{}')


def main():
    with urllib.request.urlopen(f'{SITE}/api/gateway/health', timeout=180) as r:
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

    if not head_down and not pool_low:
        print('链头健康、池子充足，不开 Issue')
        return

    title = ('🔌 网关链头挂了 · ' + HEAD) if head_down else f'🔌 免费池可用家数跌到 {len(healthy)}'
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
    lines += ['## 全部供应商', '', '| 供应商 | 状态 | 延迟 | 错误 |', '|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r['name']} | {'✅' if r['ok'] else '❌ ' + str(r['status'])} "
                     f"| {r['cost_ms']}ms | {r['error'][:60]} |")
    body = '\n'.join(lines)

    if not GH_TOKEN:
        print('无 GITHUB_TOKEN，仅打印：\n' + body); return

    issues = gh('GET', f'/repos/{REPO}/issues?state=open&labels=gateway&per_page=20')
    hit = next((i for i in issues if i['title'].startswith('🔌 ')), None)
    if hit:
        gh('PATCH', f"/repos/{REPO}/issues/{hit['number']}", {'title': title, 'body': body})
        print(f"已更新 Issue #{hit['number']}")
    else:
        r = gh('POST', f'/repos/{REPO}/issues', {'title': title, 'body': body, 'labels': ['gateway']})
        print(f"已开 Issue #{r['number']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('网关哨兵失败：', e, file=sys.stderr)
        sys.exit(1)
