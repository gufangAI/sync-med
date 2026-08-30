# -*- coding: utf-8 -*-
"""
内测申请哨兵 —— 有人想付钱时，确保有人知道。

立此因（2026-07-31 平台CTO）：
/pricing 的三个转化入口（申请内测 / 申请创始席位 / 联系企业合作）提交后，
functions/api/feedback/submit.js 只往 D1 写一行就结束了 —— 没有邮件、
没有 webhook、没有任何人会被惊动。CLAUDE.md 第 8 条铁律写着
「凡是只写进日志的产线，一律视为没人看」（血证：pan-register 每天成功、
日志里写着 not-in-D1=3965，连喊 12 天零人响应）。

留资躺在数据库里没人看，和没有留资功能是一回事——区别只在于，
前者会让我们以为自己有。当天刚把导航入口补上（此前 7 处 /pricing 链接
全在阅读器内部，只逛首页的访客一个都走不到），门开了就得配上铃。

它盯什么：user_feedback 表里 status='inbox' 且未 handled 的申请类
反馈（founder / founder_pro / enterprise / pro），按积压时长分级。
超过 48 小时未处理的单独标红 —— 想付钱的人等两天还没人回，这单基本就凉了。

它出问题时谁会看见：workflow 失败会出现在 Actions 页；有积压时写/更新
当天 Issue 并打 intake 标签，创始人手机能收到。零积压时不开 Issue，
避免每天一条噪音把真信号淹掉。
"""
import os, json, sys, urllib.request, datetime

ACCOUNT = os.environ['CF_ACCOUNT_ID']
TOKEN   = os.environ['D1_API_TOKEN']
DB_ID   = os.environ.get('GUYAOFANG_DB_ID', '2db89d3b-e988-4577-a9e3-fb7c563af72f')

REPO     = os.environ.get('GITHUB_REPOSITORY', 'hosonzuo8848/sync-med')
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')

# 申请类反馈 —— 这些是钱，和 bug 报告 / 功能建议不是一回事，必须分开盯
INTAKE_TYPES = ('founder', 'founder_pro', 'enterprise', 'pro')


def d1(sql):
    req = urllib.request.Request(
        f'https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/{DB_ID}/query',
        data=json.dumps({'sql': sql}).encode(),
        headers={'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.loads(r.read())
    if not j.get('success'):
        raise RuntimeError(f"D1 查询失败: {json.dumps(j.get('errors'), ensure_ascii=False)[:200]}")
    return j['result'][0]['results']


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
    types = ','.join(f"'{t}'" for t in INTAKE_TYPES)
    rows = d1(f"""
        SELECT feedback_id, email, feedback_type, title, content,
               created_at,
               CAST((strftime('%s','now') - created_at) / 3600 AS INTEGER) AS hours_waiting
        FROM user_feedback
        WHERE feedback_type IN ({types})
          AND (status IS NULL OR status = 'inbox')
          AND handled_at IS NULL
        ORDER BY created_at ASC
    """)

    # 自测数据不算真实申请。2026-07-24 我自己写过一条验证后端接住新类型的
    # 假申请，差点在排查时被当成"有人等了 7 天没人理"报上去——过滤掉，
    # 别让哨兵为自己的测试数据拉警报。
    rows = [r for r in rows if '自测' not in str(r.get('title') or '')
                            and 'CTO' not in str(r.get('title') or '')]

    print(f'待处理申请：{len(rows)} 条')
    for r in rows:
        print(f"  #{r['feedback_id']} [{r['feedback_type']}] 等待 {r['hours_waiting']}h")

    if not rows:
        print('零积压，不开 Issue（避免每日噪音淹掉真信号）')
        return

    stale = [r for r in rows if (r.get('hours_waiting') or 0) >= 48]
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    title = f'💰 内测申请待处理 · {len(rows)} 条' + (f'（{len(stale)} 条超 48h）' if stale else '')

    lines = [f'> 巡查时间：{today} UTC · 数据源：guyaofang-db `user_feedback`', '']
    if stale:
        lines += ['## 🔴 超 48 小时未回', '',
                  '想付钱的人等两天还没人回，这单基本就凉了。', '']
        for r in stale:
            em = r.get('email') or '（未留邮箱）'
            lines.append(f"- **#{r['feedback_id']}** `{r['feedback_type']}` · 等待 **{r['hours_waiting']}h** · {em}")
            lines.append(f"  - {str(r.get('title') or '')[:80]}")
        lines.append('')

    fresh = [r for r in rows if (r.get('hours_waiting') or 0) < 48]
    if fresh:
        lines += ['## 🟡 新进', '']
        for r in fresh:
            em = r.get('email') or '（未留邮箱）'
            lines.append(f"- **#{r['feedback_id']}** `{r['feedback_type']}` · {r['hours_waiting']}h · {em}")
            lines.append(f"  - {str(r.get('title') or '')[:80]}")
        lines.append('')

    lines += ['---', '',
              '处理完请把该行 `status` 置为非 `inbox` 或填上 `handled_at`，本哨兵即不再报。',
              '',
              '入口：网站导航「創始內測」→ /pricing → 三张卡的申请按钮 → `/api/feedback/submit`。']
    body = '\n'.join(lines)

    if not GH_TOKEN:
        print('无 GITHUB_TOKEN，仅打印：\n' + body)
        return

    # 幂等：同名 Issue 已开着就更新，不每天堆一条新的
    issues = gh('GET', f'/repos/{REPO}/issues?state=open&labels=intake&per_page=20')
    hit = next((i for i in issues if i['title'].startswith('💰 内测申请待处理')), None)
    if hit:
        gh('PATCH', f"/repos/{REPO}/issues/{hit['number']}", {'title': title, 'body': body})
        print(f"已更新 Issue #{hit['number']}")
    else:
        r = gh('POST', f'/repos/{REPO}/issues',
               {'title': title, 'body': body, 'labels': ['intake']})
        print(f"已开 Issue #{r['number']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('哨兵失败：', e, file=sys.stderr)
        sys.exit(1)
