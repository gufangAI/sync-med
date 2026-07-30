# -*- coding: utf-8 -*-
"""
产线失败哨兵 —— 兜住所有"有 cron 但没有告警出口"的 workflow。

立此因（2026-07-31 平台CTO 排查）：
sync-med 仓 65 个 workflow 里，**20 个有 cron 但正文里没有任何 Issue 出口**——
它们每天自动跑，失败了只在 Actions 页面留一个红叉，不会有任何人被惊动。
其中就有 `pan-register.yml`：CLAUDE.md 第 8 条那条血证的主角，
「每天成功、日志里写着 not-in-D1=3965，连喊 12 天零人响应」。

fleet-watch.yml 覆盖了其中 4 个（clean-embed / ocr / sync / ocr_ndl），
剩下 16 个是真空。与其给每个 workflow 单独加告警（16 处改动、以后新建的还会漏），
不如在外面兜一层：谁失败了、谁很久没跑了，一次全查出来。

设计取舍：
· **只读**，不触发、不重启、不禁用任何 workflow。自愈是 fleet-watch 的职责，
  两处都自愈会打架（execution-watchdog 第三条：watchdog 只拉死进程，绝不强杀慢任务）。
· 排除 fleet-watch 已经盯着的，避免同一件事开两个 Issue 互相刷屏。
· **只看最近一次 run**：连续失败才是问题，偶发失败下一轮就自愈了。
· 全绿不开 Issue —— 每天一条"一切正常"会让人很快学会忽略它，
  真出事那天也照样忽略。
"""
import os, json, sys, urllib.request, datetime

REPO     = os.environ.get('GITHUB_REPOSITORY', 'gufangAI/sync-med')
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')

# fleet-watch.yml 已经在盯的，这里跳过，免得同一件事两个 Issue 对着刷
COVERED_BY_FLEET = {'ocr.yml', 'ocr_ndl.yml', 'sync.yml', 'clean-embed.yml',
                    'guji_sync.yml', 'council.yml'}
# 哨兵自己不盯自己
SELF = {'workflow-sentry.yml', 'fleet-watch.yml', 'intake-sentry.yml', 'gateway-sentry.yml'}

STALE_HOURS = 48   # 有 cron 却 48 小时没跑过 = 触发器可能断了（execution-watchdog 第五条：停摆先查触发）


def gh(path, method='GET', payload=None):
    req = urllib.request.Request(
        f'https://api.github.com{path}',
        data=json.dumps(payload).encode() if payload else None,
        headers={'Authorization': f'Bearer {GH_TOKEN}',
                 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'},
        method=method)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read() or b'{}')


def main():
    wfs = gh(f'/repos/{REPO}/actions/workflows?per_page=100').get('workflows', [])
    now = datetime.datetime.now(datetime.timezone.utc)

    failed, stale = [], []
    for w in wfs:
        fname = (w.get('path') or '').split('/')[-1]
        if fname in COVERED_BY_FLEET or fname in SELF:
            continue
        if w.get('state') != 'active':
            continue

        runs = gh(f"/repos/{REPO}/actions/workflows/{w['id']}/runs"
                  f"?per_page=1&exclude_pull_requests=true").get('workflow_runs', [])
        if not runs:
            continue
        r = runs[0]
        if r.get('status') != 'completed':
            continue

        started = r.get('run_started_at') or r.get('created_at')
        hours = None
        if started:
            t = datetime.datetime.fromisoformat(started.replace('Z', '+00:00'))
            hours = int((now - t).total_seconds() // 3600)

        if r.get('conclusion') == 'failure':
            failed.append((w['name'], fname, hours, r.get('html_url')))
        elif hours is not None and hours >= STALE_HOURS and r.get('conclusion') == 'success':
            # 只有带 cron 的才算"停摆"——纯手动触发的 workflow 很久没跑是正常的
            stale.append((w['name'], fname, hours))

    print(f'扫描 {len(wfs)} 个 workflow · 最近一次失败 {len(failed)} 个 · 疑似停摆 {len(stale)} 个')
    for n, f, h, _ in failed:
        print(f'  ❌ {f:28} {n[:30]}  {h}h 前')

    if not failed:
        print('无失败产线，不开 Issue')
        return

    title = f'🏭 产线失败 · {len(failed)} 条'
    lines = ['> 兜底哨兵：盯的是 fleet-watch 覆盖范围之外、且自身没有告警出口的 workflow。', '',
             '## 🔴 最近一次运行失败', '']
    for n, f, h, url in failed:
        lines.append(f'- **{n}** (`{f}`) · {h} 小时前 · [查看运行]({url})')
    if stale:
        lines += ['', f'## 🟡 超 {STALE_HOURS}h 未运行（触发器可能断了）', '']
        for n, f, h in stale:
            lines.append(f'- {n} (`{f}`) · 上次 {h} 小时前')
        lines += ['', '停摆先查"是不是没触发/被 cancel"，别先改代码——'
                      '「之前几天好好的、代码越改越停」是改坏停摆反模式。']
    body = '\n'.join(lines)

    if not GH_TOKEN:
        print('无 GITHUB_TOKEN，仅打印：\n' + body); return

    issues = gh(f'/repos/{REPO}/issues?state=open&labels=pipeline&per_page=20')
    hit = next((i for i in issues if i['title'].startswith('🏭 产线失败')), None)
    if hit:
        gh(f"/repos/{REPO}/issues/{hit['number']}", 'PATCH', {'title': title, 'body': body})
        print(f"已更新 Issue #{hit['number']}")
    else:
        r = gh(f'/repos/{REPO}/issues', 'POST', {'title': title, 'body': body, 'labels': ['pipeline']})
        print(f"已开 Issue #{r['number']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('产线哨兵失败：', e, file=sys.stderr)
        sys.exit(1)
