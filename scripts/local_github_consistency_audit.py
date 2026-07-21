#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_github_consistency_audit.py — 本地-GitHub 一致性审计 v1

立此脚本因(2026-07-21/22 夜审计发现,创始人定性为"系统性失忆问题"):
  tools/rag/bulk_ingest_vectorize.py 配套写了 .github/workflows/bulk_ingest_rag.yml,
  但这个 workflow 从来没有 push 到 GitHub、从来没有真正跑过(git status 显示整个 .github/
  从未 git add) —— 182.8 万向量的生产索引实际是靠人工本地手动跑脚本喂出来的,长期被误以为
  "有自动化管线在跑",本地进度日志早就跟真实索引状态对不上号。这不是孤例,本脚本把"抓这类
  本地/GitHub 状态对不上号"做成常态化巡查,不是查一次就完。

三个维度(每次都查,不遗漏):
  维度1 · 本地 workflow 文件 vs 远端真实存在
    1A(仅本机深度模式,需要 --local-repo 指到真实工作区):
       扫本地 git 工作区里 .github/workflows/*.yml 的 git 追踪状态,揪出"写了但从未 git add /
       从未 push"的草稿——这正是 RAG 案例的真实形状,云端 runner 天然看不到(它只能看到已提交的
       内容),所以必须本机跑或未来接自建 runner 才能查全。
    1B(云端模式也能查,checkout 即可看到已提交内容):
       ①扫已提交的仓库树,找"长得像 workflow 定义(同时含 on: 和 jobs:)但没放在
       .github/workflows/ 下"的走失 yaml;②扫 docs/README/脚本注释里"提到某 workflow 文件名"
       的文字,和真实注册的 workflow 列表交叉核对,揪出"文档说有、仓库里其实没有"的情况。
  维度2 · 远端 workflow 存在 vs 从未真正运行过("幽灵 workflow")
    对两个仓库全部真实注册的 workflow,查 run 历史,分类:
      GHOST_ZERO_RUNS        — 存在但一次都没跑过(runs=0)
      DISABLED_BUT_SCHEDULED — 配着 schedule/cron,但当前处于 disabled 状态,cron 不可能触发
      SCHEDULE_NEVER_FIRED   — 配着 schedule/cron,已存在够久(>=14天),但历史 run 里从来没有一次
                                是被 schedule 触发的(全是手动 workflow_dispatch)
      DISPATCH_ONLY_BY_DESIGN— 压根没配 schedule,纯手动工具/诊断脚本,健康、非异常
      HEALTHY                — 其余情况
  维度3 · 本地/仓库声称的进度状态 vs D1/Vectorize 生产真实状态
    查已知几个"进度/断点/checkpoint"文件声称的进度,和 Cloudflare D1 表行数 / Vectorize
    向量数的真实值做硬数字对比,标出差距。云端模式只能报"真实值是多少";本机模式能额外读到
    本地 checkpoint 文件,给出"声称 vs 真实"的差值。

诚实边界(写脚本时就要认清,别自己骗自己):
  一个跑在 GitHub 云端 ubuntu-latest 上的 workflow,checkout 到的只是"已提交"的仓库状态,
  天然看不到本机磁盘上从未 git add 过的文件。维度1A / 维度3 的"本地 checkpoint 文件"这部分,
  只有在本机(或未来注册的 self-hosted runner)上跑本脚本才能查到——云端模式会清楚地在报告里
  写明"本次为云端模式,未做本机深度扫描",不假装什么都查到了。

只读:不改任何 workflow 状态、不删任何文件、不碰 secrets 内容,只查 GitHub API + Cloudflare
只读 API(Vectorize info / D1 SELECT)。

用法:
  云端(GitHub Actions 默认): python local_github_consistency_audit.py --mode cloud
  本机深度模式:
    python local_github_consistency_audit.py --mode local \
        --local-repo hosonzuo8848/guyaofang=F:\\0book\\guyaofang-web \
        --local-repo gufangAI/sync-med=F:\\0book\\<sync-med本地路径,如有>
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import subprocess
from datetime import datetime, timezone

# ─── 被审计的仓库 ──────────────────────────────────────────────────────────────
REPOS = [
    {"slug": "gufangAI/sync-med", "token_env": "GITHUB_TOKEN", "label": "sync-med(下载/后端管线)"},
    {"slug": "hosonzuo8848/guyaofang", "token_env": "GUYAOFANG_TOKEN", "label": "guyaofang-web(前端+RAG)"},
]

# 维度3 · 已知"进度声称 vs 生产真实"检查点(v1 先覆盖 RAG 灌库这条线——今晚案发现场;
# 后续发现新的 checkpoint 类文件,照这个字典的形状继续加,不需要改检查逻辑本身)
CF_VECTORIZE_INDEXES = ["tcm-rag-768", "tcm-rag-clean-768", "tcm-rag-clean-1024", "tcm-rag-xf"]
CF_D1_TABLES_TO_COUNT = ["persona_panels", "book_fingerprints"]

SCHEDULE_NEVER_FIRED_MIN_DAYS = 14  # workflow 存在够久才有资格判定"该来的 schedule 一直没来"


# ─── GitHub API 基础(风格同 fleet_watch.py:urllib,只读)──────────────────────

def gh_api(path: str, token: str, method: str = "GET") -> dict | list:
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def gh_api_safe(path: str, token: str) -> tuple[dict | list | None, str | None]:
    """包一层:失败不炸,返回 (data, error_str)。跨仓库 token 权限不够时用这个走降级。"""
    try:
        return gh_api(path, token), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return None, f"HTTP {e.code}: {body[:150]}"
    except Exception as e:
        return None, str(e)[:150]


def parse_dt(s: str | None):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def days_ago(dt, now) -> float | None:
    if dt is None:
        return None
    return (now - dt).total_seconds() / 86400.0


# ─── 维度2 · 幽灵 workflow 检测 ────────────────────────────────────────────────

def fetch_workflow_file_text(slug: str, path: str, token: str) -> str | None:
    data, err = gh_api_safe(f"repos/{slug}/contents/{path}", token)
    if err or not isinstance(data, dict) or "content" not in data:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None


def workflow_has_schedule(text: str | None) -> bool:
    if not text:
        return False
    # YAML 里 cron 行通常是列表项 "- cron: '...'",前面带 "- " 不能漏匹配
    return bool(re.search(r"^\s*-?\s*cron\s*:\s*['\"]", text, re.M))


def audit_ghost_workflows(repo: dict, now: datetime) -> dict:
    slug, token_env = repo["slug"], repo["token_env"]
    token = os.environ.get(token_env, "")
    result = {"repo": slug, "ok": False, "error": None, "workflows": []}

    if not token:
        result["error"] = f"缺 token(环境变量 {token_env} 未设置或为空),跳过该仓库"
        return result

    data, err = gh_api_safe(f"repos/{slug}/actions/workflows?per_page=100", token)
    if err:
        result["error"] = f"读 workflows 列表失败: {err}"
        return result

    for wf in data.get("workflows", []):
        path = wf.get("path", "")
        if not path.startswith(".github/workflows/"):
            continue  # 跳过 dynamic/github-code-scanning/codeql 这类非文件 workflow

        wf_id = wf["id"]
        name = wf.get("name", path)
        state = wf.get("state", "?")

        runs_data, runs_err = gh_api_safe(
            f"repos/{slug}/actions/workflows/{wf_id}/runs?per_page=100&exclude_pull_requests=true", token)
        if runs_err:
            result["workflows"].append({
                "name": name, "path": path, "state": state,
                "category": "API_ERROR", "detail": runs_err,
            })
            continue

        total = runs_data.get("total_count", 0)
        runs = runs_data.get("workflow_runs", [])

        text = fetch_workflow_file_text(slug, path, token)
        has_schedule = workflow_has_schedule(text)
        ever_scheduled = any(r.get("event") == "schedule" for r in runs)

        oldest_seen = None
        for r in runs:
            dt = parse_dt(r.get("created_at"))
            if dt and (oldest_seen is None or dt < oldest_seen):
                oldest_seen = dt
        age_days = days_ago(oldest_seen, now)

        if total == 0:
            category = "GHOST_ZERO_RUNS"
        elif has_schedule and state != "active":
            category = "DISABLED_BUT_SCHEDULED"
        elif has_schedule and not ever_scheduled and age_days is not None and age_days >= SCHEDULE_NEVER_FIRED_MIN_DAYS:
            category = "SCHEDULE_NEVER_FIRED"
        elif has_schedule and not ever_scheduled:
            category = "SCHEDULE_TOO_NEW_TO_JUDGE"
        elif not has_schedule:
            category = "DISPATCH_ONLY_BY_DESIGN"
        else:
            category = "HEALTHY"

        result["workflows"].append({
            "name": name, "path": path, "state": state, "total_runs": total,
            "has_schedule": has_schedule, "ever_scheduled_run": ever_scheduled,
            "age_days_of_sample": round(age_days, 1) if age_days is not None else None,
            "category": category,
        })

    result["ok"] = True
    return result


# ─── 维度1B · 云端也能查的:走失 yaml + 文档提及但不存在的 workflow ─────────────

_SKIP_TREE_PREFIXES = ("node_modules/", "dist/", ".git/", "package-lock.json")


def audit_misplaced_and_phantom_mentions(repo: dict) -> dict:
    slug, token_env = repo["slug"], repo["token_env"]
    token = os.environ.get(token_env, "")
    result = {"repo": slug, "ok": False, "error": None,
              "misplaced_workflow_yaml": [], "phantom_mentions": []}
    if not token:
        result["error"] = f"缺 token({token_env}),跳过"
        return result

    branch_data, err = gh_api_safe(f"repos/{slug}", token)
    if err:
        result["error"] = f"读仓库信息失败: {err}"
        return result
    default_branch = branch_data.get("default_branch", "main")

    tree_data, err = gh_api_safe(f"repos/{slug}/git/trees/{default_branch}?recursive=1", token)
    if err:
        result["error"] = f"读仓库树失败: {err}"
        return result
    if tree_data.get("truncated"):
        result["error"] = "(注: 仓库树太大被 GitHub 截断,本次扫描不完整)"

    tree = [f for f in tree_data.get("tree", []) if f.get("type") == "blob"]

    # 真实注册的 workflow 文件名集合(维度2 已经拿到,这里独立再拿一次 path 列表保持函数独立可测)
    wf_data, _ = gh_api_safe(f"repos/{slug}/actions/workflows?per_page=100", token)
    real_paths = set()
    if wf_data:
        real_paths = {w["path"] for w in wf_data.get("workflows", []) if w.get("path", "").startswith(".github/workflows/")}
    real_basenames = {p.rsplit("/", 1)[-1] for p in real_paths}

    # ① 走失的 workflow 形状 yaml(在仓库里但不在 .github/workflows/ 下)
    yaml_files = [f["path"] for f in tree if f["path"].endswith((".yml", ".yaml"))
                  and not any(f["path"].startswith(p) for p in _SKIP_TREE_PREFIXES)]
    outside = [p for p in yaml_files if not p.startswith(".github/workflows/")]
    for p in outside:
        text = fetch_workflow_file_text(slug, p, token)
        if text and re.search(r"^\s*on\s*:", text, re.M) and re.search(r"^\s*jobs\s*:", text, re.M):
            result["misplaced_workflow_yaml"].append(p)

    # ② 文档/脚本注释里提到的 workflow 文件名,和真实存在的做交叉核对
    # 用 GitHub Code Search 一次查询定位候选文件(而不是逐个 tree 文件拉内容——
    # v0 在 sync-med 上逐文件拉取就跑了 2 分钟以上超时,大仓库 guyaofang-web 只会更糟)。
    mention_re = re.compile(r"(?:\.github/workflows/|gh workflow run\s+)([\w\-]+\.ya?ml)")
    seen_files = set()
    for query_phrase in (".github/workflows/", "gh workflow run"):
        q = urllib.parse.quote(f'"{query_phrase}" repo:{slug}')
        hits, err = gh_api_safe(f"search/code?q={q}&per_page=30", token)
        if err or not hits:
            continue
        for item in hits.get("items", []):
            p = item.get("path", "")
            if not p or p in seen_files or p.startswith(".github/workflows/"):
                continue
            if any(p.startswith(sp) for sp in _SKIP_TREE_PREFIXES):
                continue
            seen_files.add(p)

    for p in seen_files:
        text = fetch_workflow_file_text(slug, p, token)
        if not text:
            continue
        for m in mention_re.finditer(text):
            fname = m.group(1)
            if fname not in real_basenames:
                result["phantom_mentions"].append({"mentioned_in": p, "workflow_name": fname})

    result["ok"] = True
    return result


# ─── 维度1A · 本机深度模式:git 未追踪的 workflow 草稿 ─────────────────────────

def _run_git(cwd: str, args: list[str]) -> tuple[str, int]:
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), -1


def audit_local_untracked_workflows(repo_slug: str, local_path: str, real_wf_basenames: set[str]) -> dict:
    result = {"repo": repo_slug, "local_path": local_path, "ok": False, "error": None,
              "untracked_workflow_yaml": [], "total_untracked_files": 0,
              "local_ahead_commits": None, "days_since_local_head": None,
              "days_since_origin_main_head": None}

    if not os.path.isdir(os.path.join(local_path, ".git")):
        result["error"] = f"{local_path} 不是 git 工作区,跳过本机深度扫描"
        return result

    # 本地 workflow 目录里实际存在哪些 .yml
    wf_dir = os.path.join(local_path, ".github", "workflows")
    on_disk = []
    if os.path.isdir(wf_dir):
        on_disk = [f for f in os.listdir(wf_dir) if f.endswith((".yml", ".yaml"))]

    tracked_out, _ = _run_git(local_path, ["ls-files", ".github/workflows/"])
    tracked_basenames = {line.rsplit("/", 1)[-1] for line in tracked_out.splitlines() if line.strip()}

    for fname in on_disk:
        if fname not in tracked_basenames:
            # 本地真实存在、本地 git 里都没追踪(更别提 push)——这正是 RAG 案例的形状
            severity = "从未 git add(未追踪)"
            also_missing_remote = fname not in real_wf_basenames
            result["untracked_workflow_yaml"].append({
                "file": fname,
                "git_status": severity,
                "also_absent_from_remote_api": also_missing_remote,
            })

    untracked_out, _ = _run_git(local_path, ["status", "--porcelain"])
    result["total_untracked_files"] = len([l for l in untracked_out.splitlines()
                                            if l.strip() and l.startswith("??")])

    ahead_out, rc = _run_git(local_path, ["rev-list", "--count", "origin/main..HEAD"])
    if rc == 0 and ahead_out.strip().isdigit():
        result["local_ahead_commits"] = int(ahead_out.strip())

    now_ts = datetime.now(timezone.utc)
    local_head_out, rc1 = _run_git(local_path, ["log", "-1", "--format=%ct"])
    origin_head_out, rc2 = _run_git(local_path, ["log", "-1", "--format=%ct", "origin/main"])
    if rc1 == 0 and local_head_out.strip().isdigit():
        dt = datetime.fromtimestamp(int(local_head_out.strip()), tz=timezone.utc)
        result["days_since_local_head"] = round((now_ts - dt).total_seconds() / 86400.0, 1)
    if rc2 == 0 and origin_head_out.strip().isdigit():
        dt = datetime.fromtimestamp(int(origin_head_out.strip()), tz=timezone.utc)
        result["days_since_origin_main_head"] = round((now_ts - dt).total_seconds() / 86400.0, 1)

    result["ok"] = True
    return result


# ─── 维度3 · Cloudflare 真实生产状态(Vectorize / D1) ───────────────────────────

def _cf_auth_headers() -> dict:
    token = os.environ.get("CF_API_TOKEN_SCOPED", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {
        "X-Auth-Email": os.environ.get("CF_GLOBAL_EMAIL", ""),
        "X-Auth-Key": os.environ.get("CF_GLOBAL_API_KEY", ""),
    }


def cf_vectorize_info(index_name: str, account_id: str) -> dict:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/vectorize/v2/indexes/{index_name}/info"
    req = urllib.request.Request(url, headers={**_cf_auth_headers(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read())
        if j.get("success") and j.get("result"):
            return {"ok": True, "vector_count": j["result"].get("vectorCount"),
                    "processed_upto": j["result"].get("processedUpToDatetime")}
        return {"ok": False, "error": str(j.get("errors", ""))[:150]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def cf_d1_table_counts(tables: list[str], account_id: str, db_id: str, token: str) -> dict:
    if not (account_id and db_id and token):
        return {"ok": False, "error": "缺 CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN"}
    sql = "SELECT " + ", ".join(f"(SELECT COUNT(*) FROM {t}) AS {t}_count" for t in tables)
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query"
    req = urllib.request.Request(url, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps({"sql": sql}).encode("utf-8"))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read())
        if j.get("success"):
            row = (j.get("result") or [{}])[0].get("results", [{}])[0]
            return {"ok": True, "counts": row}
        return {"ok": False, "error": str(j.get("errors", ""))[:150]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def audit_production_reality(now: datetime) -> dict:
    account_id = os.environ.get("CF_ACCOUNT_ID", "")
    result = {"vectorize": {}, "d1": {}, "ok": bool(account_id)}
    if not account_id:
        result["error"] = "缺 CF_ACCOUNT_ID,跳过维度3云端查询"
        return result

    for idx in CF_VECTORIZE_INDEXES:
        result["vectorize"][idx] = cf_vectorize_info(idx, account_id)

    result["d1"] = cf_d1_table_counts(
        CF_D1_TABLES_TO_COUNT, account_id,
        os.environ.get("D1_DATABASE_ID", ""), os.environ.get("D1_API_TOKEN", ""))
    return result


LOCAL_CHECKPOINT_FILES = {
    # repo_slug -> [(relative_path_from_repo_root, "claimed 值怎么算", 对应真实指标 key)]
    "hosonzuo8848/guyaofang": [
        # 注: _vec_done.txt 是"书"计数、vector_count 是"块/向量"计数,单位不同不能直接相减,
        # 不放进这张自动做差表(避免"10 本 vs 182万" 这种单位不一致的假 gap);
        # 真实故事在 _vec_ingest_log.txt(单位是"块",和 vector_count 同单位)里已经能看出来。
        {"path": "tools/rag/_vec_work/_vec_ingest_log.txt", "kind": "last_summary_line",
         "compares_to": "vectorize.tcm-rag-768.vector_count",
         "note": "vectorize 灌库运行日志(最后一条汇总行声称的累计块数)"},
        {"path": "tools/rag/_persona_work/_persona_done.txt", "kind": "lines",
         "compares_to": "d1.persona_panels+book_fingerprints",
         "note": "personas 灌库已完成清单(每行一个 panel/fingerprint)"},
    ],
}


def _lookup_reality_value(reality: dict, dotted_key: str):
    """支持 'vectorize.tcm-rag-768.vector_count' 或 'd1.persona_panels+book_fingerprints' 这种简单点路径。"""
    if dotted_key.startswith("vectorize."):
        _, idx, field = dotted_key.split(".", 2)
        return reality.get("vectorize", {}).get(idx, {}).get(field)
    if dotted_key.startswith("d1."):
        spec = dotted_key.split(".", 1)[1]
        counts = reality.get("d1", {}).get("counts", {}) if reality.get("d1", {}).get("ok") else {}
        parts = spec.split("+")
        vals = [counts.get(f"{p}_count") for p in parts]
        if all(v is not None for v in vals):
            return sum(vals)
        return None
    return None


def audit_local_checkpoints_vs_reality(repo_slug: str, local_path: str, reality: dict) -> dict:
    result = {"repo": repo_slug, "checks": []}
    for spec in LOCAL_CHECKPOINT_FILES.get(repo_slug, []):
        fpath = os.path.join(local_path, spec["path"].replace("/", os.sep))
        entry = {"file": spec["path"], "note": spec["note"], "claimed": None,
                 "exists": os.path.isfile(fpath), "compares_to": spec["compares_to"]}
        if entry["exists"]:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                if spec["kind"] == "lines":
                    entry["claimed"] = len([l for l in lines if l.strip()])
                elif spec["kind"] == "last_summary_line":
                    non_empty = [l.strip() for l in lines if l.strip()]
                    entry["claimed_raw_last_line"] = non_empty[-1] if non_empty else None
                    m = re.search(r"累计\s*(\d+)\s*块", non_empty[-1]) if non_empty else None
                    entry["claimed"] = int(m.group(1)) if m else None
            except Exception as e:
                entry["error"] = str(e)[:150]
        entry["real_value"] = _lookup_reality_value(reality, spec["compares_to"])
        if entry["claimed"] is not None and entry["real_value"] is not None:
            entry["gap"] = entry["real_value"] - entry["claimed"]
        result["checks"].append(entry)
    return result


# ─── 报告组装 ─────────────────────────────────────────────────────────────────

CATEGORY_LABEL = {
    "GHOST_ZERO_RUNS": "🚨 幽灵(从未跑过一次)",
    "DISABLED_BUT_SCHEDULED": "⚠️ 已禁用但仍配着 schedule",
    "SCHEDULE_NEVER_FIRED": "⚠️ 配了 schedule 但从未被 schedule 触发过",
    "SCHEDULE_TOO_NEW_TO_JUDGE": "🕐 配了 schedule,创建不足14天,暂不判定",
    "DISPATCH_ONLY_BY_DESIGN": "手动工具(设计如此,健康)",
    "HEALTHY": "✅ 正常",
    "API_ERROR": "❓ 查询失败",
}


def build_report(mode: str, ghost_results: list[dict], phantom_results: list[dict],
                  local_results: list[dict], reality: dict, local_checkpoint_results: list[dict],
                  now: datetime) -> tuple[str, int]:
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append(f"## 🔍 本地-GitHub 一致性审计 — {ts}\n")
    lines.append(f"运行模式: **{mode}**"
                  + ("(云端 ubuntu-latest,天然看不到本机磁盘上从未 git add 过的文件——"
                     "维度1A/维度3的本机 checkpoint 对比本轮未执行,见下方说明)" if mode == "cloud" else
                     "(本机深度模式,含本地 git 工作区扫描)"))
    lines.append("")

    alert_count = 0

    # ── 维度2:幽灵 workflow ──
    lines.append("### 维度2 · 远端 workflow 存在 vs 从未真正运行过")
    for gr in ghost_results:
        lines.append(f"\n**{gr['repo']}**")
        if gr.get("error"):
            lines.append(f"- 跳过: {gr['error']}")
            continue
        flagged = [w for w in gr["workflows"] if w["category"] not in
                   ("HEALTHY", "DISPATCH_ONLY_BY_DESIGN", "SCHEDULE_TOO_NEW_TO_JUDGE")]
        healthy_dispatch = [w for w in gr["workflows"] if w["category"] == "DISPATCH_ONLY_BY_DESIGN"]
        lines.append(f"- 共 {len(gr['workflows'])} 个真实注册 workflow;"
                      f"手动工具(健康){len(healthy_dispatch)} 个;需要关注 {len(flagged)} 个。")
        if flagged:
            lines.append("")
            lines.append("| workflow | 状态 | 累计 run 数 | 分类 |")
            lines.append("|---|---|---|---|")
            for w in flagged:
                lines.append(f"| `{w['path']}` | {w['state']} | {w.get('total_runs','?')} "
                              f"| {CATEGORY_LABEL.get(w['category'], w['category'])} |")
                if w["category"] in ("GHOST_ZERO_RUNS", "DISABLED_BUT_SCHEDULED", "SCHEDULE_NEVER_FIRED"):
                    alert_count += 1
        else:
            lines.append("  (本轮未发现幽灵 / 该关注的 workflow)")

    # ── 维度1B:走失 yaml + 文档幻影提及 ──
    lines.append("\n### 维度1(云端可查部分)· 走失的 workflow 草稿 / 文档提及但不存在")
    for pr in phantom_results:
        lines.append(f"\n**{pr['repo']}**")
        if pr.get("error"):
            lines.append(f"- {pr['error']}")
        if pr.get("misplaced_workflow_yaml"):
            alert_count += len(pr["misplaced_workflow_yaml"])
            lines.append(f"- ⚠️ 发现 {len(pr['misplaced_workflow_yaml'])} 个"
                          f"「长得像 workflow、但没放在 .github/workflows/ 下」的文件:")
            for p in pr["misplaced_workflow_yaml"]:
                lines.append(f"  - `{p}`")
        if pr.get("phantom_mentions"):
            alert_count += len(pr["phantom_mentions"])
            lines.append(f"- ⚠️ 文档/脚本提到但真实不存在的 workflow 文件名:")
            for m in pr["phantom_mentions"]:
                lines.append(f"  - `{m['workflow_name']}`(提及于 `{m['mentioned_in']}`)")
        if not pr.get("misplaced_workflow_yaml") and not pr.get("phantom_mentions") and pr.get("ok"):
            lines.append("  (本轮未发现)")

    # ── 维度1A:本机深度扫描(仅 local 模式) ──
    if local_results:
        lines.append("\n### 维度1(本机深度部分)· 从未 git add 过的 workflow 草稿")
        for lr in local_results:
            lines.append(f"\n**{lr['repo']}**(本地路径 `{lr['local_path']}`)")
            if lr.get("error"):
                lines.append(f"- {lr['error']}")
                continue
            lines.append(f"- 本地未追踪文件总数: {lr['total_untracked_files']}"
                          f" | 本地领先 origin/main 未推送提交数: {lr.get('local_ahead_commits','?')}"
                          f" | 本地 HEAD 距今: {lr.get('days_since_local_head','?')} 天"
                          f" | origin/main HEAD 距今: {lr.get('days_since_origin_main_head','?')} 天")
            if lr["untracked_workflow_yaml"]:
                alert_count += len(lr["untracked_workflow_yaml"])
                lines.append(f"- 🚨 发现 {len(lr['untracked_workflow_yaml'])} 个从未 git add 的 workflow 文件:")
                for u in lr["untracked_workflow_yaml"]:
                    remote_note = "且远端 API 确认也不存在" if u["also_absent_from_remote_api"] else ""
                    lines.append(f"  - `{u['file']}` — {u['git_status']}{remote_note}")
            else:
                lines.append("  (.github/workflows/ 下未发现未追踪文件)")

    # ── 维度3:生产真实状态 ──
    lines.append("\n### 维度3 · 生产真实状态(Cloudflare Vectorize / D1)")
    if reality.get("ok"):
        lines.append("\n**Vectorize 向量数(真实,来自 CF API):**\n")
        lines.append("| 索引 | 向量数 | 最后处理时间 |")
        lines.append("|---|---|---|")
        for idx, info in reality.get("vectorize", {}).items():
            if info.get("ok"):
                lines.append(f"| `{idx}` | {info.get('vector_count','?')} | {info.get('processed_upto','?')} |")
            else:
                lines.append(f"| `{idx}` | 查询失败 | {info.get('error','')} |")
        d1 = reality.get("d1", {})
        if d1.get("ok"):
            counts = d1.get("counts", {})
            lines.append(f"\n**D1 真实行数**: " + " · ".join(f"{k}={v}" for k, v in counts.items()))
        else:
            lines.append(f"\n- D1 查询跳过/失败: {d1.get('error','')}")
    else:
        lines.append(f"- 跳过: {reality.get('error','')}")

    if local_checkpoint_results:
        lines.append("\n**本地 checkpoint 文件声称值 vs 真实值(本机模式才有):**\n")
        for lcr in local_checkpoint_results:
            lines.append(f"\n**{lcr['repo']}**")
            lines.append("")
            lines.append("| checkpoint 文件 | 声称值 | 真实值 | 差距 | 说明 |")
            lines.append("|---|---|---|---|---|")
            for c in lcr["checks"]:
                claimed = c.get("claimed")
                if claimed is None:
                    claimed = "文件不存在" if not c["exists"] else "无法解析"
                real_v = c.get("real_value", "未知")
                gap = c.get("gap", "—")
                if isinstance(gap, int) and gap != 0:
                    alert_count += 1
                    gap = f"**{gap:+d}**"
                lines.append(f"| `{c['file']}` | {claimed} | {real_v} | {gap} | {c['note']} |")

    lines.append("")
    lines.append("### 总结论")
    if alert_count == 0:
        lines.append("**✅ 本轮未发现新的一致性异常。**")
    else:
        lines.append(f"**⚠️ 本轮发现 {alert_count} 项需要关注的本地-GitHub 不一致。**")

    lines.append("")
    lines.append("*by local_github_consistency_audit.py v1 · 只读 GitHub API + Cloudflare 只读 API · "
                  "不改任何 workflow 状态 · 不碰 secrets 内容*")

    return "\n".join(lines), alert_count


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def parse_local_repo_args(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            continue
        slug, path = item.split("=", 1)
        out[slug.strip()] = path.strip()
    return out


def main():
    # 本机 Windows 终端默认 GBK 编码,报告里的 emoji(🔍 等)会直接把 print 炸掉;
    # 云端 ubuntu-latest 本来就是 UTF-8,这行对它是无害的 no-op。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["cloud", "local"], default="cloud")
    ap.add_argument("--local-repo", action="append", default=[],
                     help="格式 owner/repo=本地路径,可重复传多个")
    ap.add_argument("--json", default=None, help="额外把结构化原始结果写一份到这个路径(供留痕/artifact)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    local_repo_paths = parse_local_repo_args(args.local_repo)

    print(f"[audit] mode={args.mode} local_repo_paths={local_repo_paths}", file=sys.stderr)

    ghost_results = []
    phantom_results = []
    for repo in REPOS:
        print(f"[audit] 维度2 ghost workflow 扫描: {repo['slug']}", file=sys.stderr)
        ghost_results.append(audit_ghost_workflows(repo, now))
        print(f"[audit] 维度1B 走失yaml/文档幻影 扫描: {repo['slug']}", file=sys.stderr)
        phantom_results.append(audit_misplaced_and_phantom_mentions(repo))

    local_results = []
    local_checkpoint_results = []
    if args.mode == "local" and local_repo_paths:
        real_basenames_by_repo = {}
        for gr in ghost_results:
            real_basenames_by_repo[gr["repo"]] = {w["path"].rsplit("/", 1)[-1] for w in gr.get("workflows", [])}
        for slug, path in local_repo_paths.items():
            print(f"[audit] 维度1A 本机未追踪 workflow 扫描: {slug} @ {path}", file=sys.stderr)
            local_results.append(audit_local_untracked_workflows(slug, path, real_basenames_by_repo.get(slug, set())))

    print("[audit] 维度3 生产真实状态查询(Vectorize/D1)", file=sys.stderr)
    reality = audit_production_reality(now)

    if args.mode == "local" and local_repo_paths:
        for slug, path in local_repo_paths.items():
            if slug in LOCAL_CHECKPOINT_FILES:
                local_checkpoint_results.append(audit_local_checkpoints_vs_reality(slug, path, reality))

    report, alert_count = build_report(args.mode, ghost_results, phantom_results,
                                        local_results, reality, local_checkpoint_results, now)
    print(report)

    if args.json:
        raw = {
            "generated_at": now.isoformat(),
            "mode": args.mode,
            "alert_count": alert_count,
            "ghost_workflows": ghost_results,
            "misplaced_and_phantom": phantom_results,
            "local_untracked": local_results,
            "production_reality": reality,
            "local_checkpoints": local_checkpoint_results,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, default=str)
        print(f"[audit] 结构化原始结果已写: {args.json}", file=sys.stderr)

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"alert_count={alert_count}\n")
            f.write(f"report_body<<AUDIT_REPORT_EOF\n{report}\nAUDIT_REPORT_EOF\n")


if __name__ == "__main__":
    main()
