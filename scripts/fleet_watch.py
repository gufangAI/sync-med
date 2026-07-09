#!/usr/bin/env python3
'\nfleet_watch.py \u2014 \u8230\u961f\u5de1\u67e5\u811a\u672c v2\uff08\u589e\u5f3a\u7248\uff09\n\u65b0\u589e: ocr_depth_check() \u2014 \u4ece run \u65e5\u5fd7\u5224\u65ad OCR \u771f\u5b9e\u4ea7\u51fa\uff0c\u8bc6\u522b\u7a7a\u8f6c/\u6599\u89c1\u5e95\u3002\n\n\u53ea\u8bfb GitHub Actions run \u72b6\u6001 + run \u65e5\u5fd7\uff0c\u4e0d\u89e6\u53d1\u4efb\u4f55 workflow\uff0c\u4e0d\u626b R2\uff0c\u4e0d\u78b0 secrets\u3002\n\u73af\u5883\u53d8\u91cf: GITHUB_TOKEN (Actions \u81ea\u52a8\u6ce8\u5165)\n\n\u65e5\u5fd7\u683c\u5f0f\u4f9d\u636e\uff08ocr.py \u5b9e\u6d4b\u8f93\u51fa\uff0c2026-06-24 \u786e\u8ba4\uff09:\n  \u542f\u52a8\u884c: "shard N/256 imgs A/B"         \u2192 A=\u8be5shard\u56fe\u6570, B=\u5168\u5e93\u603b\u56fe\u6570\n  \u6c47\u603b\u884c: "=== shard N OCR X new, Y/Z done ===" \u2192 X=\u672c\u6b21\u65b0\u4ea7\u51fa\u6761\u6570\n  \u7a7a\u8f6c\u5224\u5b9a: \u5355\u6b21 run \u6240\u6709\u53ef\u89c1 shard \u5747\u4e3a "OCR 0 new" \u2192 \u8be5 run \u96f6\u4ea7\u51fa\n  \u6599\u89c1\u5e95\u5224\u5b9a: \u8fde\u7eed N \u6b21 run \u5747\u96f6\u4ea7\u51fa \u2192 \u89e6\u53d1 alert\n'
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


WORKFLOWS = {
    "ocr.yml":         {"name": "OCR",          "alert_hours": 8},
    "sync.yml":        {"name": "sync",         "alert_hours": 24},
    "guji_sync.yml":   {"name": 'guji-sync(\u53e4\u7c4d\u8fc1\u79fb)', "alert_hours": 6},
    "clean-embed.yml": {"name": 'clean-embed(clean\u7d22\u5f15\u704c\u5e93)', "alert_hours": 10},
}


OCR_WORKFLOW = "ocr.yml"
OCR_DEPTH_RUNS = 3          
OCR_ZERO_ALERT_THRESHOLD = 3  
OCR_LOG_SAMPLE_JOBS = 8      


D1_VS_PAN_MISSING_ALERT_THRESHOLD = 50   
D1_VS_PAN_WILD_ALERT_THRESHOLD = 20      




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
    '\u76f4\u63a5 GET \u4e00\u4e2a\u5b8c\u6574 URL\uff0c\u8fd4\u56de bytes\uff08\u7528\u4e8e\u4e0b\u8f7d log zip\uff09\u3002'
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    '\u963b\u6b62 urllib \u81ea\u52a8\u8ddf\u968f\u91cd\u5b9a\u5411\uff0c\u8ba9\u8c03\u7528\u65b9\u62ff\u5230 302 \u7684 Location\u3002'
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _fetch_job_log_text(job_id: int) -> str:
    '\n    \u83b7\u53d6\u5355\u4e2a job \u7684\u65e5\u5fd7\u6587\u672c\uff08\u7eaf\u6587\u672c\uff0c\u975e ZIP\uff09\u3002\n\n    GitHub /actions/jobs/{id}/logs \u8fd4\u56de 302 \u2192 Azure blob \u9884\u7b7e\u540d URL\u3002\n    \u76f4\u63a5\u8ddf\u968f\u91cd\u5b9a\u5411\u4f1a\u628a Authorization \u8f6c\u53d1\u7ed9 blob\uff0c\u5bfc\u81f4 Azure \u8fd4\u56de 400/403\u3002\n    \u6b63\u786e\u505a\u6cd5:\n      1. \u7528 _NoRedirect opener \u62e6\u622a 302\uff0c\u62ff\u5230 Location URL\u3002\n      2. \u4e0d\u5e26 Authorization \u91cd\u65b0 GET Location URL\uff08\u5df2\u542b SAS \u7b7e\u540d\uff09\u3002\n    '
    api_url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            blob_url = e.headers.get("Location", "")
            if not blob_url:
                raise RuntimeError(f"job {job_id} log: redirect but no Location header") from e
            
            blob_req = urllib.request.Request(blob_url)
            try:
                with urllib.request.urlopen(blob_req, timeout=60) as blob_resp:
                    return blob_resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e2:
                raise RuntimeError(f"job {job_id} blob fetch → {e2.code}: {e2.read()[:200]}") from e2
        raise RuntimeError(f"job {job_id} log API → {e.code}: {e.read()[:200]}") from e


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_ago(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 3600




def _fetch_run_log_text(run_id: int, max_jobs: int) -> str:
    '\n    \u53ea\u4e0b\u524d max_jobs \u4e2a shard job \u7684\u5355\u72ec\u65e5\u5fd7(jobs API)\uff0c\u4e0d\u4e0b\u6574\u4e2a run \u7684\u5927 zip\u3002\n    \u5168 run zip \u542b 256 \u4e2a job\u3001\u4e0b\u8f7d\u8981\u5341\u51e0\u5206\u949f\u3001\u4f1a\u62d6\u57ae\u5de1\u67e5(\u5b9e\u6d4b\u88ab concurrency cancel)\uff1b\n    \u5355 job log \u662f\u7eaf\u6587\u672c\u3001\u79d2\u7ea7\u3002\u53ea\u8bfb\u4e0d\u5199\uff0c\u4e0d\u843d\u76d8\u3002\n\n    \u6ce8\u610f: per_page=30 \u53d6\u524d 30 \u4e2a jobs\uff08prep + run(0)..run(28)\uff09\uff1b\n    OCR workflow jobs \u521b\u5efa\u987a\u5e8f: prep \u6392\u7b2c 0\uff0crun(0)..run(255) \u63a5\u7eed\u3002\n    jobs[:max_jobs] \u53d6\u524d max_jobs \u4e2a\u542b\u5c11\u91cf shard job\uff0c\u8db3\u591f\u5224\u65ad\u5168\u96f6\u3002\n    '
    try:
        jobs_data = gh_api(f"repos/{REPO}/actions/runs/{run_id}/jobs?per_page=30")
    except RuntimeError as e:
        print(f"  [ocr_depth] jobs list failed for run {run_id}: {e}", file=sys.stderr)
        return ""
    all_jobs = jobs_data.get("jobs", [])
    
    shard_jobs = [j for j in all_jobs if re.search(r"\(\d+\)", j.get("name", ""))]
    sampled = shard_jobs[:max_jobs] if shard_jobs else all_jobs[:max_jobs]
    print(f"  [ocr_depth] run {run_id}: total_jobs_in_page={len(all_jobs)}, "
          f"shard_jobs_found={len(shard_jobs)}, sampling={len(sampled)}", file=sys.stderr)
    texts = []
    for job in sampled:
        jid = job.get("id")
        jname = job.get("name", "?")
        if not jid:
            continue
        try:
            text = _fetch_job_log_text(jid)
            texts.append(text)
            
            found = bool(_SHARD_SUMMARY_RE.search(text))
            print(f"  [ocr_depth]   job {jid} ({jname}): {len(text)} chars, summary_found={found}", file=sys.stderr)
        except RuntimeError as e:
            print(f"  [ocr_depth]   job {jid} ({jname}) log failed: {e}", file=sys.stderr)
    return "\n".join(texts)



_SHARD_SUMMARY_RE = re.compile(
    r"=== shard \d+ OCR (\d+) new,\s*(\d+)/(\d+) done ==="
)

_SHARD_START_RE = re.compile(
    r"shard \d+/\d+ imgs (\d+)/(\d+)"
)


def _parse_ocr_metrics_from_log(log_text: str) -> dict:
    '\n    \u4ece\u5355\u6b21 run \u7684\u65e5\u5fd7\u6587\u672c\u4e2d\u63d0\u53d6 OCR \u6307\u6807\u3002\n    \u8fd4\u56de:\n        total_new      \u2014 \u672c\u6b21 run \u4ea7\u51fa\u7684\u65b0 OCR \u6761\u6570\uff08\u6240\u6709\u53ef\u89c1 shard \u4e4b\u548c\uff09\n        shard_count    \u2014 \u51fa\u73b0\u6c47\u603b\u884c\u7684 shard \u6570\n        zero_shards    \u2014 \u4ea7\u51fa=0 \u7684 shard \u6570\n        imgs_per_shard \u2014 \u6bcf shard \u56fe\u6570\uff08\u53d6\u7b2c\u4e00\u4e2a\u542f\u52a8\u884c\uff09\n        total_imgs     \u2014 \u5168\u5e93\u603b\u56fe\u6570\uff08\u53d6\u7b2c\u4e00\u4e2a\u542f\u52a8\u884c\uff09\n        done_ratio     \u2014 \u5e73\u5747\u5df2\u5b8c\u6210\u7387\uff08Y/Z \u5747\u503c\uff09\uff0c\u82e5\u65e0\u5219 None\n    '
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
    '\n    OCR \u6df1\u5ea6\u6307\u6807\u68c0\u67e5\u3002\n    \u903b\u8f91:\n      1. \u53d6 ocr.yml \u6700\u8fd1 OCR_DEPTH_RUNS \u6b21 completed/success run\n      2. \u5bf9\u6bcf\u6b21 run \u4e0b\u8f7d\u65e5\u5fd7 ZIP\uff0c\u89e3\u6790 shard \u6c47\u603b\u884c\n      3. \u7edf\u8ba1 total_new\uff1b\u82e5\u4e3a 0 \u2192 \u8be5 run \u5224\u5b9a"\u96f6\u4ea7\u51fa"\n      4. \u82e5\u8fde\u7eed >= OCR_ZERO_ALERT_THRESHOLD \u6b21\u96f6\u4ea7\u51fa \u2192 alert\n\n    \u5224\u7a7a\u8f6c\u7684\u6838\u5fc3\u7279\u5f81\uff08\u6765\u81ea\u5b9e\u6d4b\u65e5\u5fd7\uff09:\n      - \u6bcf\u4e2a shard \u5747\u8f93\u51fa "=== shard N OCR 0 new, Y/Z done ==="\n      - total_new = 0 for ALL sampled shards in that run\n      - \u8fde\u7eed\u591a\u6b21 run \u5982\u6b64 \u2192 \u6599\u89c1\u5e95 / \u4e0b\u8f7d\u7ebf\u672a\u6295\u65b0\u6599\n\n    \u8fd4\u56de dict:\n        alert        \u2014 bool\n        alert_msg    \u2014 \u6587\u5b57\u8bf4\u660e\n        runs_checked \u2014 \u68c0\u67e5\u4e86\u51e0\u6b21 run\n        zero_streak  \u2014 \u8fde\u7eed\u96f6\u4ea7\u51fa\u6b21\u6570\n        per_run      \u2014 List[dict] \u6bcf\u6b21 run \u7684\u6307\u6807\u6458\u8981\n        total_imgs   \u2014 \u5168\u5e93\u603b\u56fe\u6570\uff08\u6700\u65b0 run \u7684\u503c\uff09\n    '
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
    
    completed_runs = [
        r for r in runs
        if r.get("status") == "completed"
    ][:OCR_DEPTH_RUNS]

    if not completed_runs:
        result["alert_msg"] = 'ocr_depth: \u65e0\u5df2\u5b8c\u6210 run \u53ef\u68c0\u67e5'
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
            
            print(f"  [ocr_depth] run {run_id}: no shard summary lines found in sampled jobs", file=sys.stderr)
            continue

        
        
        is_zero = (metrics["shard_count"] >= 2 and metrics["total_new"] == 0)
        if is_zero:
            zero_streak += 1
        else:
            
            break

    result["runs_checked"] = len(per_run_data)
    result["zero_streak"] = zero_streak
    result["per_run"] = per_run_data

    if zero_streak >= OCR_ZERO_ALERT_THRESHOLD:
        result["alert"] = True
        result["alert_msg"] = (
            f"⚠️ OCR \u7a7a\u8f6c/\u6599\u89c1\u5e95: \u6700\u8fd1 {zero_streak} \u6b21 run \u5747\u96f6\u4ea7\u51fa "
            f"(\u9608\u503c={OCR_ZERO_ALERT_THRESHOLD})。"
            f"\u5168\u5e93 {result['total_imgs'] or '?'} \u5f20\u56fe\u53ef\u80fd\u5df2\u5168\u90e8\u5904\u7406\u5b8c，"
            f"\u8bf7\u68c0\u67e5\u4e0b\u8f7d\u7ebf\u662f\u5426\u5411 R2 \u6295\u5165\u65b0\u6599。"
        )
    elif zero_streak > 0:
        result["alert_msg"] = (
            f"OCR \u8fd1 {zero_streak} \u6b21\u96f6\u4ea7\u51fa（\u672a\u8fbe\u9608\u503c {OCR_ZERO_ALERT_THRESHOLD}，\u6301\u7eed\u89c2\u5bdf）。"
        )
    else:
        result["alert_msg"] = f"OCR \u8fd1 {result['runs_checked']} \u6b21 run \u6709\u5b9e\u9645\u4ea7\u51fa，\u6b63\u5e38。"

    return result


def fmt_ocr_depth(depth: dict) -> list[str]:
    '\u628a ocr_depth_check \u7ed3\u679c\u683c\u5f0f\u5316\u4e3a Markdown \u884c\uff0c\u5e76\u5165\u4e3b\u62a5\u544a\u3002'
    lines = []
    lines.append("")
    lines.append('### OCR \u6df1\u5ea6\u6307\u6807')

    if depth["alert"]:
        lines.append(f"> **{depth['alert_msg']}**")
    else:
        lines.append(f"> {depth['alert_msg']}")

    lines.append("")
    lines.append(f"\u5168\u5e93\u603b\u56fe\u6570: `{depth['total_imgs'] or '未知'}` | "
                 f"\u56de\u67e5 run \u6570: {depth['runs_checked']} | "
                 f"\u8fde\u7eed\u96f6\u4ea7\u51fa: {depth['zero_streak']}")
    lines.append("")
    lines.append('| Run ID | \u521b\u5efa\u65f6\u95f4 | \u7ed3\u8bba | \u91c7\u6837shard\u6570 | \u65b0\u4ea7\u51fa\u6761\u6570 | \u96f6\u4ea7\u51fashard | \u5df2\u5b8c\u6210\u7387 |')
    lines.append("|--------|---------|------|-----------|----------|------------|---------|")

    for r in depth["per_run"]:
        done_pct = f"{r['done_ratio']*100:.1f}%" if r["done_ratio"] is not None else "—"
        new_icon = "⚠️ 0" if r["total_new"] == 0 and r["shard_count"] >= 2 else str(r["total_new"])
        lines.append(
            f"| {r['run_id']} | {r['created_at']} | {r['conclusion']} | "
            f"{r['shard_count']} | {new_icon} | {r['zero_shards']} | {done_pct} |"
        )

    return lines




def gh_api_put(path: str) -> None:
    '\u7a7a body \u7684 PUT\uff08\u7528\u4e8e enable workflow\uff09\u3002'
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, method="PUT", headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def self_heal_disabled(file_name: str, cfg: dict) -> str | None:
    '\n    2026-07-01 \u65b0\u589e(\u6cbb\u6839):\u53d1\u73b0\u88ab\u76d1\u63a7\u7684 workflow \u5904\u4e8e disabled \u72b6\u6001(\u624b\u52a8\u6216\u81ea\u52a8\u7981\u7528)\n    \u2192 \u7acb\u5373\u81ea\u52a8\u91cd\u65b0\u542f\u7528\uff0c\u4e0d\u7b49\u4eba\u6765\u770b Issue\u3001\u4e0d\u4f9d\u8d56\u4efb\u4f55\u5916\u90e8\u901a\u77e5/\u5524\u9192\u670d\u52a1\u3002\n    \u8fd9\u662f\u7eaf GitHub Actions \u5185\u81ea\u6108\uff0c\u6bcf\u5c0f\u65f6\u5de1\u67e5\u4e00\u6b21\u81ea\u52a8\u89e6\u53d1\uff0c\u6700\u574f\u60c5\u51b5 1 \u5c0f\u65f6\u5185\u81ea\u6108\u3002\n    \u8fd4\u56de:\u81ea\u6108\u52a8\u4f5c\u8bf4\u660e(str)\u6216 None(\u65e0\u9700\u81ea\u6108)\u3002\n    '
    try:
        wf = gh_api(f"repos/{REPO}/actions/workflows/{file_name}")
    except RuntimeError as e:
        return None  

    state = wf.get("state", "")
    if state == "active":
        return None

    try:
        gh_api_put(f"repos/{REPO}/actions/workflows/{file_name}/enable")
        return f"🔧 \u81ea\u6108: {cfg['name']} \u539f\u72b6\u6001={state}，\u5df2\u81ea\u52a8\u91cd\u65b0\u542f\u7528"
    except Exception as e:
        return f"⚠️ \u81ea\u6108\u5931\u8d25: {cfg['name']} \u539f\u72b6\u6001={state}，\u91cd\u65b0\u542f\u7528\u51fa\u9519: {str(e)[:100]}"


def check_workflow(file_name: str, cfg: dict, now: datetime) -> dict:
    heal_note = self_heal_disabled(file_name, cfg)

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
            "note": (heal_note + "; " if heal_note else "") + str(e)[:120],
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
        "note": heal_note or "",
    }


def fmt_hours(h: float | None) -> str:
    if h is None:
        return '\u4ece\u672a\u6210\u529f'
    if h < 1:
        return f"{int(h*60)}\u5206\u949f\u524d"
    return f"{h:.1f}h \u524d"




def build_report(results: list[dict], ocr_depth: dict, now: datetime) -> str:
    ts = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M CST")

    
    wf_alert_count = sum(1 for r in results if r["alert"])
    ocr_depth_alert = 1 if ocr_depth.get("alert") else 0
    alert_count = wf_alert_count + ocr_depth_alert

    lines = []
    lines.append(f"## 🛡️ \u8230\u961f\u5de1\u67e5\u5feb\u62a5 — {ts}\n")
    lines.append('| \u7ebf\u540d | \u6700\u540e\u6210\u529f | \u6700\u8fd1\u7ed3\u8bba | \u5f53\u524d\u5728\u8dd1 | \u72b6\u6001 |')
    lines.append("|------|---------|---------|---------|------|")

    for r in results:
        status_icon = '\u26a0\ufe0f \u5f02\u5e38' if r["alert"] else '\u2705 \u6b63\u5e38'
        
        if r["name"] == "OCR" and ocr_depth.get("alert"):
            status_icon = '\u26a0\ufe0f \u5f02\u5e38(\u7a7a\u8f6c)'
        running_icon = '\U0001f504 \u662f' if r["is_running"] else '\u5426'
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
        lines.append(f"**\u603b\u7ed3\u8bba: ✅ \u5168\u90e8 {len(results)} \u6761\u7ebf\u6b63\u5e38（\u542b OCR \u6df1\u5ea6\u68c0\u67e5）。**")
    else:
        lines.append(f"**\u603b\u7ed3\u8bba: ⚠️ {alert_count} \u9879\u5f02\u5e38（workflow \u72b6\u6001 {wf_alert_count} + OCR \u7a7a\u8f6c {ocr_depth_alert}），\u8bf7\u7acb\u5373\u68c0\u67e5!**")

    
    lines.extend(fmt_ocr_depth(ocr_depth))

    lines.append("")
    lines.append('### \u98ce\u9669\u63d0\u793a')
    for r in results:
        if r["alert"]:
            h = r["last_success_hours"]
            threshold = WORKFLOWS.get(
                next((k for k, v in WORKFLOWS.items() if v["name"] == r["name"]), ""),
                {}
            ).get("alert_hours", "?")
            if h is None:
                lines.append(f"- **{r['name']}**: \u4ece\u672a\u6709\u6210\u529f\u8bb0\u5f55，\u9700\u7acb\u5373\u6392\u67e5。")
            else:
                lines.append(f"- **{r['name']}**: \u4e0a\u6b21\u6210\u529f\u5df2 {h:.1f}h \u524d（\u9608\u503c {threshold}h），\u6700\u8fd1\u7ed3\u8bba={r['last_conclusion']}。")

    if ocr_depth.get("alert"):
        lines.append(f"- **OCR \u6df1\u5ea6**: {ocr_depth['alert_msg']}")

    lines.append("")
    lines.append('### \u4e0b\u4e00\u6b65')
    if alert_count == 0:
        lines.append('- \u65e0\u9700\u64cd\u4f5c\uff0c\u8230\u961f\u6b63\u5e38\u8fd0\u884c\u4e2d\u3002')
    else:
        if wf_alert_count > 0:
            lines.append('- \u6253\u5f00\u5bf9\u5e94 workflow \u7684 Actions \u9875\u9762\uff0c\u67e5\u770b\u5931\u8d25 run \u7684\u65e5\u5fd7\u3002')
            lines.append('- \u786e\u8ba4 R2 \u51ed\u636e / OCR \u6a21\u578b / sync \u76ee\u6807\u662f\u5426\u6b63\u5e38\u3002')
            lines.append('- \u4fee\u590d\u540e\u624b\u52a8 `workflow_dispatch` \u91cd\u8dd1\u8be5\u7ebf\u9a8c\u8bc1\u3002')
        if ocr_depth_alert:
            lines.append('- **OCR \u7a7a\u8f6c**: \u68c0\u67e5\u4e0b\u8f7d\u7ebf\u662f\u5426\u6709\u65b0\u4e66\u5165 R2\uff08`book/` \u6216 `gufang/` \u6876\uff09\u3002')
            lines.append('- \u82e5 R2 \u786e\u5b9e\u6709\u65b0\u6599\u4f46 OCR \u672a\u5904\u7406\uff0c\u68c0\u67e5 ocr.py \u7684 shard \u5206\u6876\u903b\u8f91\u662f\u5426\u8986\u76d6\u65b0\u4e66 prefix\u3002')
            lines.append('- \u82e5 R2 \u6682\u65e0\u65b0\u6599\uff0c\u4e0b\u8f7d\u7ebf\u6062\u590d\u540e OCR \u4f1a\u81ea\u52a8\u6062\u590d\u4ea7\u51fa\uff0c\u53ef\u6682\u65f6\u964d\u4f4e ocr.yml \u89e6\u53d1\u9891\u7387\u8282\u7701 runner \u5206\u949f\u6570\u3002')

    lines.append("")
    lines.append(f"*by fleet_watch v2 · \u53ea\u8bfb GitHub run \u72b6\u6001+\u65e5\u5fd7 · \u4e0d\u626b R2 · \u4e0d\u78b0 secrets*")

    return "\n".join(lines)




def d1_vs_pan_reconcile():
    '\u5bf9\u8d26 D1 catalog vs 123 \u5df2\u5907\u4efd\u6e05\u5355,\u5206\u7ea7\u8f93\u51fa\u5dee\u5f02\u3002\n\n    \u2605 \u53ea\u8bfb D1 + \u53ea\u8bfb R2 \u4e0a\u7684 PAN_DONE_KEY(sync prep \u6b65\u9aa4\u7ef4\u62a4\u7684"123\u5df2\u6709\u5217\u8868"\u7f13\u5b58),\n      \u7edd\u4e0d\u8c03\u7528 123 API\u3001\u7edd\u4e0d\u6539 D1\u3001\u7edd\u4e0d\u5199 R2\u3002\n    \u2605 \u5206\u7ea7:\n      - \u7f3a\u5931(D1 \u6709 123 \u65e0):\u4f4e\u98ce\u9669,\u62a5\u544a\u6570\u91cf,\u53ef\u4ee5\u8ba9 sync \u4e0b\u6b21\u81ea\u7136\u8865\u4e0a\u3002\n      - \u91ce\u751f(123 \u6709 D1 \u65e0):\u6709\u6b67\u4e49,\u53ea\u62a5\u8b66\u4e0d\u52a8\u624b(\u53ef\u80fd\u662f\u547d\u540d\u4e0d\u4e00\u81f4\u3001\u6d4b\u8bd5\u6570\u636e\u3001\u8bef\u64cd\u4f5c)\u3002\n\n    \u73af\u5883\u53d8\u91cf(\u5168\u90e8\u53ef\u9009,\u4efb\u4e00\u7f3a\u5931\u5c31\u8df3\u8fc7\u6574\u4e2a\u6a21\u5757,\u4e0d\u5f71\u54cd\u5176\u5b83\u5de1\u67e5):\n      CF_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN(D1 \u67e5\u8be2)\n      S_EP / S_AK / S_SK / S_BUCKET / PAN_DONE_KEY(R2 \u8bfb\u7f13\u5b58,\u590d\u7528 sync.py \u5df2\u6709)\n\n    \u8fd4\u56de:{"ok":bool, "d1_total":int, "pan_zip_have":int, "missing":int, "wild":int, "alert":bool, "alert_msg":str}\n    '
    result = {"ok": False, "d1_total": 0, "pan_zip_have": 0, "missing": 0, "wild": 0,
              "alert": False, "alert_msg": "", "skip_reason": ""}

    
    d1_env = ["CF_ACCOUNT_ID", "D1_DATABASE_ID", "D1_API_TOKEN"]
    r2_env = ["S_EP", "S_AK", "S_SK", "S_BUCKET"]
    missing_env = [e for e in (d1_env + r2_env) if not os.environ.get(e)]
    if missing_env:
        result["skip_reason"] = f"\u7f3a\u73af\u5883\u53d8\u91cf: {','.join(missing_env)}"
        return result

    try:
        
        acc = os.environ["CF_ACCOUNT_ID"]; db = os.environ["D1_DATABASE_ID"]; tok = os.environ["D1_API_TOKEN"]
        d1_url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
        sql = ("SELECT book_id, book_title FROM books_assets_v2 "
               "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
        req = urllib.request.Request(d1_url, method="POST",
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
            data=json.dumps({"sql": sql}).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=120) as resp:
            j = json.loads(resp.read())
        if not j.get("success"):
            result["skip_reason"] = f"D1 \u67e5\u8be2\u5931\u8d25: {str(j.get('errors',''))[:100]}"
            return result
        rows = (j.get("result") or [{}])[0].get("results") or []
        
        d1_titles = {row.get("book_title"): row.get("book_id") for row in rows if row.get("book_title")}
        result["d1_total"] = len(d1_titles)

        
        pan_done_key = os.environ.get("PAN_DONE_KEY", "_cc/pan_done.json")
        try:
            import boto3
        except ImportError:
            result["skip_reason"] = 'boto3 \u672a\u5b89\u88c5(pip install boto3)'
            return result
        s3 = boto3.client("s3", endpoint_url=os.environ["S_EP"],
            aws_access_key_id=os.environ["S_AK"], aws_secret_access_key=os.environ["S_SK"],
            region_name="auto")
        try:
            pan_done_raw = s3.get_object(Bucket=os.environ["S_BUCKET"], Key=pan_done_key)["Body"].read()
            pan_done = json.loads(pan_done_raw.decode("utf-8"))
        except Exception as e:
            result["skip_reason"] = f"R2 \u8bfb {pan_done_key} \u5931\u8d25({str(e)[:80]}) → \u8bf4\u660e sync.prep \u8fd8\u6ca1\u8dd1\u8fc7·\u8df3\u8fc7\u5bf9\u8d26"
            return result
        
        pan_zip_have = set(pan_done.get("zip", []))
        result["pan_zip_have"] = len(pan_zip_have)

        
        expected = {title + ".zip" for title in d1_titles.keys()}
        missing = expected - pan_zip_have    
        wild = pan_zip_have - expected        
        result["missing"] = len(missing)
        result["wild"] = len(wild)

        
        alert_parts = []
        if result["missing"] >= D1_VS_PAN_MISSING_ALERT_THRESHOLD:
            alert_parts.append(f"\u7f3a\u5931{result['missing']}\u672c(\u8d85\u9608\u503c{D1_VS_PAN_MISSING_ALERT_THRESHOLD})→sync\u53ef\u80fd\u672a\u8dd1\u901a")
        if result["wild"] >= D1_VS_PAN_WILD_ALERT_THRESHOLD:
            alert_parts.append(f"\u91ce\u751f{result['wild']}\u4e2a(\u8d85\u9608\u503c{D1_VS_PAN_WILD_ALERT_THRESHOLD})→\u8bf7\u4eba\u5de5\u6838\u5bf9(\u6709\u6b67\u4e49·\u52ff\u5220)")
        if alert_parts:
            result["alert"] = True
            result["alert_msg"] = " / ".join(alert_parts)

        result["ok"] = True
        return result
    except Exception as e:
        result["skip_reason"] = f"\u5bf9\u8d26\u5f02\u5e38({str(e)[:100]}) → \u672c\u6b21\u8df3\u8fc7"
        return result


def fmt_d1_vs_pan(r):
    '\u5bf9\u8d26\u7ed3\u679c\u683c\u5f0f\u5316\u6210\u62a5\u544a markdown \u6bb5\u3002'
    lines = ["", '### \u8f6c\u5b58 D1\u2194123 \u5bf9\u8d26']
    if not r.get("ok"):
        lines.append(f"- \u8df3\u8fc7: {r.get('skip_reason','未知')}")
        return lines
    lines.append(f"- D1 \u5e94\u6709: {r['d1_total']} \u672c · 123 \u5df2\u6709(zip): {r['pan_zip_have']} \u4e2a")
    lines.append(f"- **\u7f3a\u5931**(D1 \u6709 123 \u65e0): {r['missing']} \u672c → sync \u4e0b\u6b21\u4f1a\u81ea\u7136\u8865")
    lines.append(f"- **\u91ce\u751f**(123 \u6709 D1 \u65e0): {r['wild']} \u4e2a → \u6709\u6b67\u4e49、\u53ea\u62a5\u544a、\u7edd\u4e0d\u81ea\u52a8\u5220")
    if r.get("alert"):
        lines.append(f"- ⚠️ \u544a\u8b66: {r['alert_msg']}")
    return lines




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

    
    print("Running OCR depth check...", file=sys.stderr)
    ocr_depth = ocr_depth_check(now)
    print(f"  zero_streak={ocr_depth['zero_streak']} alert={ocr_depth['alert']}", file=sys.stderr)
    if ocr_depth["alert_msg"]:
        print(f"  msg: {ocr_depth['alert_msg']}", file=sys.stderr)

    
    print("Running D1 vs 123 reconcile...", file=sys.stderr)
    d1pan = d1_vs_pan_reconcile()
    if d1pan.get("ok"):
        print(f"  d1_total={d1pan['d1_total']} pan_zip={d1pan['pan_zip_have']} missing={d1pan['missing']} wild={d1pan['wild']} alert={d1pan['alert']}", file=sys.stderr)
    else:
        print(f"  skipped: {d1pan.get('skip_reason','')}", file=sys.stderr)

    
    report = build_report(results, ocr_depth, now)
    
    report += "\n" + "\n".join(fmt_d1_vs_pan(d1pan))
    print(report)

    
    wf_alert_count = sum(1 for r in results if r["alert"])
    ocr_depth_alert = 1 if ocr_depth.get("alert") else 0
    d1pan_alert = 1 if d1pan.get("alert") else 0
    alert_count = wf_alert_count + ocr_depth_alert + d1pan_alert

    with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a", encoding="utf-8") as f:
        f.write(f"alert_count={alert_count}\n")
        f.write(f"ocr_zero_streak={ocr_depth['zero_streak']}\n")
        f.write(f"d1pan_missing={d1pan.get('missing',0)}\n")
        f.write(f"d1pan_wild={d1pan.get('wild',0)}\n")
        f.write(f"report_body<<FLEET_REPORT_EOF\n{report}\nFLEET_REPORT_EOF\n")


if __name__ == "__main__":
    main()
