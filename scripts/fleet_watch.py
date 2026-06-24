#!/usr/bin/env python3
"""
fleet_watch.py — 舰队巡查脚本 v2（增强版）
新增: ocr_depth_check() — 从 run 日志判断 OCR 真实产出，识别空转/料见底。

只读 GitHub Actions run 状态 + run 日志，不触发任何 workflow，不扫 R2，不碰 secrets。
环境变量: GITHUB_TOKEN (Actions 自动注入)

日志格式依据（ocr.py 实测输出，2026-06-24 确认）:
  启动行: "shard N/256 imgs A/B"         → A=该shard图数, B=全库总图数
  汇总行: "=== shard N OCR X new, Y/Z done ===" → X=本次新产出条数
  空转判定: 单次 run 所有可见 shard 均为 "OCR 0 new" → 该 run 零产出
  料见底判定: 连续 N 次 run 均零产出 → 触发 alert
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import zipfile
import io
from datetime import datetime, timezone, timedelta

REPO = "gufangAI/sync-med"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 被盯的 workflow 文件名 -> 显示名 + 异常阈值(小时)
WORKFLOWS = {
    "ocr.yml":         {"name": "OCR",         "alert_hours": 8},
    "sync.yml":        {"name": "sync",         "alert_hours": 24},
    "guji_backup.yml": {"name": "guji_backup",  "alert_hours": 24},
}

# OCR 深度检查参数
OCR_WORKFLOW = "ocr.yml"
OCR_DEPTH_RUNS = 3          # 回查最近 N 次 run（取已完成的 success run）
OCR_ZERO_ALERT_THRESHOLD = 3  # 连续几次零产出触发 alert（目前已有 5 次全零）
OCR_LOG_SAMPLE_JOBS = 8      # 每次 run 最多采样几个 job 的日志（节省 API 调用）


# ─── GitHub API 基础 ──────────────────────────────────────────────────────────

def gh_api(path: str, accept: str = "application/vnd.github+json") -> dict | list:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {url} → {e.code}: {body[:200]}") from e


def gh_api_raw(url: str) -> bytes:
    """直接 GET 一个完整 URL，返回 bytes（用于下载 log zip）。"""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} → {e.code}: {body[:200]}") from e


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_ago(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600


# ─── OCR 深度检查 ─────────────────────────────────────────────────────────────

def _fetch_run_log_text(run_id: int, max_jobs: int) -> str:
    """
    从 run 日志 ZIP 中提取纯文本，只取前 max_jobs 个 job 文件。
    GitHub 返回的是 zip，每个 job 一个 .txt 文件。
    只读不写，不落盘。
    """
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/logs"
    try:
        raw = gh_api_raw(url)
    except RuntimeError as e:
        print(f"  [ocr_depth] log fetch failed for run {run_id}: {e}", file=sys.stderr)
        return ""

    lines_collected = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = sorted(zf.namelist())
            for name in names[:max_jobs]:
                if name.endswith(".txt"):
                    with zf.open(name) as f:
                        lines_collected.append(f.read().decode("utf-8", errors="replace"))
    except zipfile.BadZipFile:
        print(f"  [ocr_depth] bad zip for run {run_id}", file=sys.stderr)
    return "\n".join(lines_collected)


# 日志正则：匹配 "=== shard N OCR X new, Y/Z done ==="
_SHARD_SUMMARY_RE = re.compile(
    r"=== shard \d+ OCR (\d+) new,\s*(\d+)/(\d+) done ==="
)
# 日志正则：匹配 "shard N/256 imgs A/B"
_SHARD_START_RE = re.compile(
    r"shard \d+/\d+ imgs (\d+)/(\d+)"
)


def _parse_ocr_metrics_from_log(log_text: str) -> dict:
    """
    从单次 run 的日志文本中提取 OCR 指标。
    返回:
        total_new      — 本次 run 产出的新 OCR 条数（所有可见 shard 之和）
        shard_count    — 出现汇总行的 shard 数
        zero_shards    — 产出=0 的 shard 数
        imgs_per_shard — 每 shard 图数（取第一个启动行）
        total_imgs     — 全库总图数（取第一个启动行）
        done_ratio     — 平均已完成率（Y/Z 均值），若无则 None
    """
    summaries = _SHARD_SUMMARY_RE.findall(log_text)
    starts = _SHARD_START_RE.findall(log_text)

    total_new = 0
    zero_shards = 0
    done_ratios = []

    for new_s, done_s, total_s in summaries:
        new = int(new_s)
        done = int(done_s)
        total = int(total_s)
        total_new += new
        if new == 0:
            zero_shards += 1
        if total > 0:
            done_ratios.append(done / total)

    shard_count = len(summaries)
    done_ratio = (sum(done_ratios) / len(done_ratios)) if done_ratios else None

    imgs_per_shard = None
    total_imgs = None
    if starts:
        imgs_per_shard = int(starts[0][0])
        total_imgs = int(starts[0][1])

    return {
        "total_new": total_new,
        "shard_count": shard_count,
        "zero_shards": zero_shards,
        "imgs_per_shard": imgs_per_shard,
        "total_imgs": total_imgs,
        "done_ratio": done_ratio,
    }


def ocr_depth_check(now: datetime) -> dict:
    """
    OCR 深度指标检查。
    逻辑:
      1. 取 ocr.yml 最近 OCR_DEPTH_RUNS 次 completed/success run
      2. 对每次 run 下载日志 ZIP，解析 shard 汇总行
      3. 统计 total_new；若为 0 → 该 run 判定"零产出"
      4. 若连续 >= OCR_ZERO_ALERT_THRESHOLD 次零产出 → alert

    判空转的核心特征（来自实测日志）:
      - 每个 shard 均输出 "=== shard N OCR 0 new, Y/Z done ==="
      - total_new = 0 for ALL sampled shards in that run
      - 连续多次 run 如此 → 料见底 / 下载线未投新料

    返回 dict:
        alert        — bool
        alert_msg    — 文字说明
        runs_checked — 检查了几次 run
        zero_streak  — 连续零产出次数
        per_run      — List[dict] 每次 run 的指标摘要
        total_imgs   — 全库总图数（最新 run 的值）
    """
    result = {
        "alert": False,
        "alert_msg": "",
        "runs_checked": 0,
        "zero_streak": 0,
        "per_run": [],
        "total_imgs": None,
    }

    try:
        path = f"repos/{REPO}/actions/workflows/{OCR_WORKFLOW}/runs?per_page=20&exclude_pull_requests=true"
        data = gh_api(path)
    except RuntimeError as e:
        result["alert"] = True
        result["alert_msg"] = f"ocr_depth: API error fetching runs: {e}"
        return result

    runs = data.get("workflow_runs", [])
    # 只取已完成的 run（不管 success/failure，我们要看日志）
    completed_runs = [
        r for r in runs
        if r.get("status") == "completed"
    ][:OCR_DEPTH_RUNS]

    if not completed_runs:
        result["alert_msg"] = "ocr_depth: 无已完成 run 可检查"
        return result

    zero_streak = 0
    per_run_data = []

    for run in completed_runs:
        run_id = run["databaseId"] if "databaseId" in run else run.get("id")
        run_created = run.get("created_at", "?")[:16]
        conclusion = run.get("conclusion", "?")

        print(f"  [ocr_depth] fetching log for run {run_id} ({run_created}, {conclusion})...", file=sys.stderr)
        log_text = _fetch_run_log_text(run_id, max_jobs=OCR_LOG_SAMPLE_JOBS)
        metrics = _parse_ocr_metrics_from_log(log_text)

        per_run_data.append({
            "run_id": run_id,
            "created_at": run_created,
            "conclusion": conclusion,
            **metrics,
        })

        if result["total_imgs"] is None and metrics["total_imgs"] is not None:
            result["total_imgs"] = metrics["total_imgs"]

        if metrics["shard_count"] == 0:
            # 日志采样不够，跳过（不计入 streak）
            print(f"  [ocr_depth] run {run_id}: no shard summary lines found in sampled jobs", file=sys.stderr)
            continue

        # 判断本次 run 是否零产出
        # 条件: 采样 shard 数 >= 2 且全部 total_new == 0
        is_zero = (metrics["shard_count"] >= 2 and metrics["total_new"] == 0)
        if is_zero:
            zero_streak += 1
        else:
            # 一旦出现非零产出，streak 终止（从最新向旧回溯，第一个非零即停）
            break

    result["runs_checked"] = len(per_run_data)
    result["zero_streak"] = zero_streak
    result["per_run"] = per_run_data

    if zero_streak >= OCR_ZERO_ALERT_THRESHOLD:
        result["alert"] = True
        result["alert_msg"] = (
            f"⚠️ OCR 空转/料见底: 最近 {zero_streak} 次 run 均零产出 "
            f"(阈值={OCR_ZERO_ALERT_THRESHOLD})。"
            f"全库 {result['total_imgs'] or '?'} 张图可能已全部处理完，"
            f"请检查下载线是否向 R2 投入新料。"
        )
    elif zero_streak > 0:
        result["alert_msg"] = (
            f"OCR 近 {zero_streak} 次零产出（未达阈值 {OCR_ZERO_ALERT_THRESHOLD}，持续观察）。"
        )
    else:
        result["alert_msg"] = f"OCR 近 {result['runs_checked']} 次 run 有实际产出，正常。"

    return result


def fmt_ocr_depth(depth: dict) -> list[str]:
    """把 ocr_depth_check 结果格式化为 Markdown 行，并入主报告。"""
    lines = []
    lines.append("")
    lines.append("### OCR 深度指标")

    if depth["alert"]:
        lines.append(f"> **{depth['alert_msg']}**")
    else:
        lines.append(f"> {depth['alert_msg']}")

    lines.append("")
    lines.append(f"全库总图数: `{depth['total_imgs'] or '未知'}` | "
                 f"回查 run 数: {depth['runs_checked']} | "
                 f"连续零产出: {depth['zero_streak']}")
    lines.append("")
    lines.append("| Run ID | 创建时间 | 结论 | 采样shard数 | 新产出条数 | 零产出shard | 已完成率 |")
    lines.append("|--------|---------|------|-----------|----------|------------|---------|")

    for r in depth["per_run"]:
        done_pct = f"{r['done_ratio']*100:.1f}%" if r["done_ratio"] is not None else "—"
        new_icon = "⚠️ 0" if r["total_new"] == 0 and r["shard_count"] >= 2 else str(r["total_new"])
        lines.append(
            f"| {r['run_id']} | {r['created_at']} | {r['conclusion']} | "
            f"{r['shard_count']} | {new_icon} | {r['zero_shards']} | {done_pct} |"
        )

    return lines


# ─── 原有 workflow 状态检查（v1 逻辑不变）────────────────────────────────────

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
        conclusion = run.get("conclusion")
        status = run.get("status")
        updated = parse_dt(run.get("updated_at"))

        if status in ("queued", "in_progress"):
            is_running = True

        if last_conclusion == "no_runs":
            last_conclusion = conclusion if conclusion else status

        if conclusion == "success" and last_success_dt is None:
            last_success_dt = updated

    last_success_hours = hours_ago(last_success_dt, now)
    alert_hours = cfg["alert_hours"]

    alert = False
    if last_success_hours is None:
        alert = True
    elif last_success_hours > alert_hours:
        alert = True

    if last_conclusion in ("failure", "cancelled"):
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


# ─── 报告组装 ─────────────────────────────────────────────────────────────────

def build_report(results: list[dict], ocr_depth: dict, now: datetime) -> str:
    ts = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M CST")

    # alert_count 合并 workflow 状态异常 + OCR 深度 alert
    wf_alert_count = sum(1 for r in results if r["alert"])
    ocr_depth_alert = 1 if ocr_depth.get("alert") else 0
    alert_count = wf_alert_count + ocr_depth_alert

    lines = []
    lines.append(f"## 🛡️ 舰队巡查快报 — {ts}\n")
    lines.append("| 线名 | 最后成功 | 最近结论 | 当前在跑 | 状态 |")
    lines.append("|------|---------|---------|---------|------|")

    for r in results:
        status_icon = "⚠️ 异常" if r["alert"] else "✅ 正常"
        # OCR 行加 OCR 深度 alert 标记
        if r["name"] == "OCR" and ocr_depth.get("alert"):
            status_icon = "⚠️ 异常(空转)"
        running_icon = "🔄 是" if r["is_running"] else "否"
        last_ok = fmt_hours(r["last_success_hours"])
        conclusion = r["last_conclusion"]
        if conclusion == "success":
            conclusion = "✅ success"
        elif conclusion == "failure":
            conclusion = "❌ failure"
        elif conclusion == "cancelled":
            conclusion = "🚫 cancelled"
        note = f" ({r['note']})" if r["note"] else ""
        lines.append(f"| {r['name']} | {last_ok} | {conclusion}{note} | {running_icon} | {status_icon} |")

    lines.append("")
    if alert_count == 0:
        lines.append(f"**总结论: ✅ 全部 {len(results)} 条线正常（含 OCR 深度检查）。**")
    else:
        lines.append(f"**总结论: ⚠️ {alert_count} 项异常（workflow 状态 {wf_alert_count} + OCR 空转 {ocr_depth_alert}），请立即检查!**")

    # 追加 OCR 深度指标区块
    lines.extend(fmt_ocr_depth(ocr_depth))

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
                lines.append(f"- **{r['name']}**: 从未有成功记录，需立即排查。")
            else:
                lines.append(f"- **{r['name']}**: 上次成功已 {h:.1f}h 前（阈值 {threshold}h），最近结论={r['last_conclusion']}。")

    if ocr_depth.get("alert"):
        lines.append(f"- **OCR 深度**: {ocr_depth['alert_msg']}")

    lines.append("")
    lines.append("### 下一步")
    if alert_count == 0:
        lines.append("- 无需操作，舰队正常运行中。")
    else:
        if wf_alert_count > 0:
            lines.append("- 打开对应 workflow 的 Actions 页面，查看失败 run 的日志。")
            lines.append("- 确认 R2 凭据 / OCR 模型 / sync 目标是否正常。")
            lines.append("- 修复后手动 `workflow_dispatch` 重跑该线验证。")
        if ocr_depth_alert:
            lines.append("- **OCR 空转**: 检查下载线是否有新书入 R2（`book/` 或 `gufang/` 桶）。")
            lines.append("- 若 R2 确实有新料但 OCR 未处理，检查 ocr.py 的 shard 分桶逻辑是否覆盖新书 prefix。")
            lines.append("- 若 R2 暂无新料，下载线恢复后 OCR 会自动恢复产出，可暂时降低 ocr.yml 触发频率节省 runner 分钟数。")

    lines.append("")
    lines.append(f"*by fleet_watch v2 · 只读 GitHub run 状态+日志 · 不扫 R2 · 不碰 secrets*")

    return "\n".join(lines)


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    results = []

    # 1. workflow 状态检查（v1 原有逻辑）
    for file_name, cfg in WORKFLOWS.items():
        print(f"Checking {cfg['name']} ({file_name})...", file=sys.stderr)
        r = check_workflow(file_name, cfg, now)
        results.append(r)
        print(f"  last_success={fmt_hours(r['last_success_hours'])} conclusion={r['last_conclusion']} alert={r['alert']}", file=sys.stderr)

    # 2. OCR 深度检查（v2 新增）
    print("Running OCR depth check...", file=sys.stderr)
    ocr_depth = ocr_depth_check(now)
    print(f"  zero_streak={ocr_depth['zero_streak']} alert={ocr_depth['alert']}", file=sys.stderr)
    if ocr_depth["alert_msg"]:
        print(f"  msg: {ocr_depth['alert_msg']}", file=sys.stderr)

    # 3. 组装报告
    report = build_report(results, ocr_depth, now)
    print(report)

    # 4. 写出 alert 标志供 workflow 读取
    wf_alert_count = sum(1 for r in results if r["alert"])
    ocr_depth_alert = 1 if ocr_depth.get("alert") else 0
    alert_count = wf_alert_count + ocr_depth_alert

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a", encoding="utf-8") as f:
        f.write(f"alert_count={alert_count}\n")
        f.write(f"ocr_zero_streak={ocr_depth['zero_streak']}\n")
        f.write(f"report_body<<FLEET_REPORT_EOF\n{report}\nFLEET_REPORT_EOF\n")


if __name__ == "__main__":
    main()
