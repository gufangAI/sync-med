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


# ── 绿勾零产出探针（2026-08-16 · 改善计划2.3）──────────────────────────
# conclusion=success 只证明"进程没崩"，不证明"有产出"。血证就在本仓：
# roundtable(百家论道) 自 08-09 起连续绿勾、每天 24 跑，content_gen_runs 里
# inserted 全 0（gateway 401），没有任何人被惊动 —— 和 pan-register 同一类病。
# 注册纪律（上线前实测出来的两条，违反即误报）：
#   1. 只准注册**验过活体**的探针：历史行数>0 且能指认生产者 workflow。
#      （差点注册 ocr_processing_log —— 该表有史以来 0 行，挂上=永远喊狼来了）
#   2. 探针 SQL 必须逐表核对时间列类型：intel_items.captured_at 是 TEXT，
#      content_gen_runs.started_at 是 epoch 整数 —— 整数与 datetime() 文本比较恒假。
# 产出=0 与 run 红绿**解耦**：绿勾、红叉、被 cancel、cron 被禁 —— 24h 零产出一律报。
OUTPUT_PROBES = [
    # (产线名, 生产者workflow, 24h产出计数SQL —— 返回一行一列 n)
    ('鹰眼情报采集', 'intel-radar.yml',
     "SELECT COUNT(*) AS n FROM intel_items WHERE captured_at >= datetime('now','-24 hours')"),
    ('百家论道内容工厂', 'roundtable.yml',
     "SELECT COALESCE(SUM(inserted),0) AS n FROM content_gen_runs "
     "WHERE started_at >= CAST(strftime('%s','now','-24 hours') AS INTEGER)"),
]


def _d1_query(sql):
    """D1 只读探针。凭据缺失/查询失败返回 None（哨兵自身不能因探针挂了而挂）。"""
    acc = os.environ.get('CF_ACCOUNT_ID'); db = os.environ.get('D1_DATABASE_ID')
    tok = os.environ.get('D1_API_TOKEN')
    if not (acc and db and tok):
        return None
    try:
        req = urllib.request.Request(
            f'https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query',
            data=json.dumps({'sql': sql}).encode(),
            headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return d['result'][0]['results'][0]['n'] if d.get('success') else None
    except Exception:
        return None


def check_output_probes():
    """返回产出=0 的 [(产线名, workflow)]；探针读不到(None)只打日志不报，宁漏不误。"""
    zero = []
    for name, wf, sql in OUTPUT_PROBES:
        n = _d1_query(sql)
        if n is None:
            print(f'  ⚪ 探针读不到: {name}（凭据缺失或查询失败，跳过不误报）')
        elif n == 0:
            zero.append((name, wf))
            print(f'  ⚫ 绿勾零产出: {name} ({wf}) · 24h 产出 = 0')
        else:
            print(f'  ✅ {name} 24h 产出 {n}')
    return zero


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



_CRON_CACHE = {}

def _cron_period_hours(fname):
    """
    返回该 workflow 的 cron 执行周期（小时）；没有未注释的 cron 返回 None。

    为什么要算周期而不是用一个固定阈值：第二版用统一的 48h 判停摆，
    结果把 report-weekly（100h 没跑）、report-monthly（126h）、weekly-hunter（85h）
    全报成了异常——它们本来就是每周/每月跑一次，几天不动完全正常。
    误报比不报更糟：人被假警报训练两次，真出事那天也会顺手划掉。

    只做粗判：看 cron 的"日"和"月"字段是不是固定值，够区分 时/日/周/月 四档。
    """
    if fname in _CRON_CACHE:
        return _CRON_CACHE[fname]
    period = None
    try:
        import base64, re
        d = gh(f'/repos/{REPO}/contents/.github/workflows/{fname}')
        body = base64.b64decode(d.get('content', '')).decode('utf-8', 'replace')
        for line in body.splitlines():
            s = line.strip()
            if s.startswith('#') or 'cron:' not in s:
                continue          # 注释掉的 cron = 人为停用，不算
            m = re.search(r"cron:\s*['\"]([^'\"]+)", s)
            if not m:
                continue
            f = m.group(1).split()
            if len(f) < 5:
                continue
            minute, hour, dom, month, dow = f[:5]
            if month != '*':          p = 24 * 365      # 每年
            elif dom != '*' and not dom.startswith('*'): p = 24 * 30   # 每月固定某日
            elif dow != '*':          p = 24 * 7        # 每周
            elif hour != '*' and not hour.startswith('*'): p = 24      # 每天固定点
            else:                     p = 6             # 每几小时或更密
            period = p if period is None else min(period, p)
    except Exception:
        period = None                # 读不到就当没 cron，宁可漏报不误报
    _CRON_CACHE[fname] = period
    return period


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
            # 只有真带 cron 的才算"停摆"——纯手动触发的 workflow 几百小时没跑完全正常。
            #
            # 第一版漏了这道判断，一上线就报了 27 条"疑似停摆"，里面混着大量
            # 本来就靠 workflow_dispatch 手动跑的诊断脚本（diag_* / *-smoke / ocr_compare 等）。
            # 误报比不报更糟：人只要被假警报训练两次，真出事那天也会顺手划掉。
            # GitHub 的 workflows API 不返回触发器信息，只能读 yml 正文判断。
            period = _cron_period_hours(fname)
            if period is None:
                continue                      # 纯手动触发，几百小时不跑是正常的
            # 阈值 = 该任务自己周期的 2.5 倍，且不低于 STALE_HOURS。
            # 周报允许 420h、日报允许 60h —— 各按各的节奏判，不搞一刀切。
            limit = max(STALE_HOURS, int(period * 2.5))
            if hours < limit:
                continue
            stale.append((w['name'], fname, hours, limit))

    print(f'扫描 {len(wfs)} 个 workflow · 最近一次失败 {len(failed)} 个 · 疑似停摆 {len(stale)} 个')
    zero_out = check_output_probes()
    for n, f, h, _ in failed:
        print(f'  ❌ {f:28} {n[:30]}  {h}h 前')

    if not failed and not zero_out:
        print('无失败产线、产出探针全活，不开 Issue')
        return

    title = f'🏭 产线失败/零产出 · 失败 {len(failed)} · 零产出 {len(zero_out)}'
    lines = ['> 兜底哨兵：盯的是 fleet-watch 覆盖范围之外、且自身没有告警出口的 workflow。', '',
             '## 🔴 最近一次运行失败', '']
    for n, f, h, url in failed:
        lines.append(f'- **{n}** (`{f}`) · {h} 小时前 · [查看运行]({url})')
    if zero_out:
        lines += ['', '## ⚫ 绿勾零产出（run 在跑/在绿，24 小时产出为 0）', '']
        for name, wf in zero_out:
            lines.append(f'- **{name}** (`{wf}`) · 24h 产出=0 —— 绿勾只证明进程没崩，先查产线自己的日志与下游表')
    if stale:
        lines += ['', '## 🟡 超出各自 cron 周期未运行（触发器可能断了）', '']
        for n, f, h, lim in stale:
            lines.append(f'- {n} (`{f}`) · 上次 {h} 小时前（该任务阈值 {lim}h）')
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
