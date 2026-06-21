#!/usr/bin/env python3
"""
fleet_watch.py — 舰队巡查脚本 v1
只读 GitHub Actions run 状态,输出 Markdown 报告,不触发任何 workflow,不扫 R2。
环境变量: GITHUB_TOKEN (Actions 自动注入)
"""
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

REPO = "gufangAI/sync-med"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 被盯的 workflow 文件名 -> 显示名 + 异常阈值(小时)
WORKFLOWS = {
    "ocr.yml":         {"name": "OCR",         "alert_hours": 8},
    "sync.yml":        {"name": "sync",         "alert_hours": 24},
    "guji_backup.yml": {"name": "guji_backup",  "alert_hours": 24},
}


def gh_api(path: str) -> dict | list:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {url} → {e.code}: {body}") from e


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_ago(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600


def check_workflow(file_name: str, cfg: dict, now: datetime) -> dict:
    path = f"repos/{REPO}/actions/workflows/{file_name}/runs?per_page=10&exclude_pull_requests=true"
    try:
        data = gh_api(path)
    except RuntimeError as e:
        return {
            "name": cfg["name"],
            "last_success_hours": None,
            "last_conclusion": "API_ERROR",
            "is_running": False,
            "alert": True,
            "note": str(e)[:120],
        }

    runs = data.get("workflow_runs", [])

    last_success_dt = None
    last_conclusion = "no_runs"
    is_running = False

    for run in runs:
        conclusion = run.get("conclusion")  # success / failure / cancelled / None(running)
        status = run.get("status")          # queued / in_progress / completed
        updated = parse_dt(run.get("updated_at"))

        if status in ("queued", "in_progress"):
            is_running = True

        if last_conclusion == "no_runs":
            last_conclusion = conclusion if conclusion else status

        if conclusion == "success" and last_success_dt is None:
            last_success_dt = updated

    last_success_hours = hours_ago(last_success_dt, now)
    alert_hours = cfg["alert_hours"]

    # 判断异常
    alert = False
    if last_success_hours is None:
        # 从未成功过
        alert = True
    elif last_success_hours > alert_hours:
        alert = True
    # 最近结论是 failure/cancelled 且之后没有 success (last_success_dt is None 或 last_success_dt 比最近的 failure 更早已被上面覆盖)
    # 简化: 只要最近一条 conclusion 是 failure/cancelled 且距上次成功 > 阈值 就 alert
    if last_conclusion in ("failure", "cancelled"):
        # 检查之后有没有 success
        has_newer_success = False
        if runs:
            latest_run_updated = parse_dt(runs[0].get("updated_at"))
            if last_success_dt and latest_run_updated and last_success_dt >= latest_run_updated:
                has_newer_success = True
        if not has_newer_success:
            alert = True

    return {
        "name": cfg["name"],
        "last_success_hours": last_success_hours,
        "last_conclusion": last_conclusion or "running",
        "is_running": is_running,
        "alert": alert,
        "note": "",
    }


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "从未成功"
    if h < 1:
        return f"{int(h*60)}分钟前"
    return f"{h:.1f}h 前"


def build_report(results: list[dict], now: datetime) -> str:
    ts = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M CST")
    alert_count = sum(1 for r in results if r["alert"])

    lines = []
    lines.append(f"## 🛡️ 舰队巡查快报 — {ts}\n")
    lines.append("| 线名 | 最后成功 | 最近结论 | 当前在跑 | 状态 |")
    lines.append("|------|---------|---------|---------|------|")

    for r in results:
        status_icon = "⚠️ 异常" if r["alert"] else "✅ 正常"
        running_icon = "🔄 是" if r["is_running"] else "否"
        last_ok = fmt_hours(r["last_success_hours"])
        conclusion = r["last_conclusion"]
        if conclusion == "success":
            conclusion = "✅ success"
        elif conclusion in ("failure",):
            conclusion = "❌ failure"
        elif conclusion in ("cancelled",):
            conclusion = "🚫 cancelled"
        note = f" ({r['note']})" if r["note"] else ""
        lines.append(f"| {r['name']} | {last_ok} | {conclusion}{note} | {running_icon} | {status_icon} |")

    lines.append("")
    if alert_count == 0:
        lines.append(f"**总结论: ✅ 全部 {len(results)} 条线正常。**")
    else:
        lines.append(f"**总结论: ⚠️ {alert_count}/{len(results)} 条线异常,请立即检查!**")

    lines.append("")
    lines.append("### 风险提示")
    for r in results:
        if r["alert"]:
            h = r["last_success_hours"]
            threshold = WORKFLOWS.get(
                next((k for k, v in WORKFLOWS.items() if v["name"] == r["name"]), ""),
                {}
            ).get("alert_hours", "?")
            if h is None:
                lines.append(f"- **{r['name']}**: 从未有成功记录,需立即排查。")
            else:
                lines.append(f"- **{r['name']}**: 上次成功已 {h:.1f}h 前(阈值 {threshold}h),最近结论={r['last_conclusion']}。")

    lines.append("")
    lines.append("### 下一步")
    if alert_count == 0:
        lines.append("- 无需操作,舰队正常运行中。")
    else:
        lines.append("- 打开对应 workflow 的 Actions 页面,查看失败 run 的日志。")
        lines.append("- 确认 R2 凭据 / OCR 模型 / sync 目标是否正常。")
        lines.append("- 修复后手动 `workflow_dispatch` 重跑该线验证。")

    lines.append("")
    lines.append(f"*by fleet_watch v1 · 只读 GitHub run 状态 · 不扫 R2*")

    return "\n".join(lines)


def main():
    if not TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    results = []
    for file_name, cfg in WORKFLOWS.items():
        print(f"Checking {cfg['name']} ({file_name})...", file=sys.stderr)
        r = check_workflow(file_name, cfg, now)
        results.append(r)
        print(f"  last_success={fmt_hours(r['last_success_hours'])} conclusion={r['last_conclusion']} alert={r['alert']}", file=sys.stderr)

    report = build_report(results, now)
    print(report)

    # 写出 alert 标志供 workflow 读取
    alert_count = sum(1 for r in results if r["alert"])
    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a", encoding="utf-8") as f:
        f.write(f"alert_count={alert_count}\n")
        f.write(f"report_body<<FLEET_REPORT_EOF\n{report}\nFLEET_REPORT_EOF\n")


if __name__ == "__main__":
    main()
