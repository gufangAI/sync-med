# coding: utf-8
"""指挥中心 —— 在 GitHub Issue 里发一条指令，这里执行完把真实结果回帖。

为什么把 Issue 当指挥群，而不是另建一个平台：
  · 手机上的 GitHub App 已经在推 fleet-watch 和日报的 Issue，创始人本来就在看。
  · 零新增基础设施 —— 不用申请机器人、不用维护常驻 webhook、不用另付钱。
  · 指令和结果留在同一条 Issue 里，天然是可追溯的工单。聊天记录会散，工单不会。

它解决的真实痛点：现在创始人只能在一个终端窗口跟 CTO 一问一答，CTO 派出去的
agent 他完全看不见。常用的查证类问题他本可以自己问，不必排队等人转述。

边界，说在前头免得当成万能：
  · 这里跑的是确定性动作（查状态 / 报数字 / 触发已有产线）。
  · 需要多步推理和判断的活（改代码、写方案、审内容）仍然要派 Claude agent。
  · 任何 AI 调用只许走内部免费池网关；GitHub Models 是计费源，红线禁用。
  · 只执行仓库 owner/member/collaborator 的指令 —— public 仓谁都能评论。

回帖必须带证据。这个系统一旦开始输出"看起来正常"这类话，它就变成了另一个
没人信的日志。所以每条回复都要么给数字、要么给 run id、要么明说拿不到。
"""
import io
import json
import os
import subprocess
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BODY = os.environ.get("BODY", "").strip()
REPO = os.environ.get("REPO", "gufangAI/sync-med")
ISSUE = os.environ.get("ISSUE", "")
CF_ACC = os.environ.get("CF_ACCOUNT_ID", "")
CF_TOK = os.environ.get("CF_API_TOKEN", "")
UA = "Mozilla/5.0 (compatible; SueAI-CommandCenter/1.0)"

HELP = """### 指挥中心 · 可用指令

| 指令 | 做什么 |
|---|---|
| `/帮助` | 显示这张表 |
| `/产线` | 所有 active workflow 的最近一次真实状态（绿勾不代表干了活，会标出从未跑过的） |
| `/rag 桂枝汤` | 查线上检索真实召回，给书名和相似度 |
| `/绑定` | 生产环境绑定实况：有没有违规的 AI binding、向量库指向哪个 |
| `/ocr` | OCR 产线最近一轮的真实产出页数 |

**这里只做确定性的查证与触发。** 需要判断和多步推理的活（改代码、写方案、审内容）
还是要派 agent —— 那是 Claude Code 的能力，Actions 里做不到。
"""


def post(md):
    """回帖。用 gh CLI，token 由 workflow 注入。"""
    p = "/tmp/_cc_reply.md"
    io.open(p, "w", encoding="utf-8").write(md)
    subprocess.run(["gh", "issue", "comment", ISSUE, "-R", REPO, "-F", p],
                   check=False)
    print(md[:2000])


def gh_json(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


# ── /产线 ────────────────────────────────────────────────────────
def cmd_pipelines():
    data = gh_json("repos/%s/actions/workflows" % REPO)
    if not data:
        return "拿不到 workflow 列表（gh api 调用失败）。"
    active = [w for w in (data.get("workflows") or []) if w.get("state") == "active"]
    rows, never, failed = [], [], []
    for w in active:
        fn = (w.get("path") or "").rsplit("/", 1)[-1]
        name = (w.get("name") or fn)[:24]
        runs = gh_json("repos/%s/actions/workflows/%s/runs?per_page=1" % (REPO, fn))
        rl = (runs or {}).get("workflow_runs") or []
        if not rl:
            never.append(name)
            continue
        r0 = rl[0]
        res = r0.get("conclusion") or r0.get("status") or "-"
        when = (r0.get("created_at") or "")[5:16].replace("T", " ")
        if res == "failure":
            failed.append("%s（%s）" % (name, when))
        rows.append("| %s | %s | %s | %s |" % (name, when, r0.get("event"), res))

    out = ["### 产线实况 · %d 条 active\n" % len(active)]
    if failed:
        out.append("**失败中：** " + "、".join(failed) + "\n")
    if never:
        # 建了从没跑过最容易被"文件在那儿"骗过去
        out.append("**建了从未跑过：** " + "、".join(never) + " —— 等于没建\n")
    out.append("| workflow | 最近 | 触发 | 结论 |")
    out.append("|---|---|---|---|")
    out += rows
    out.append("\n> 绿勾只说明 workflow 跑完了，不说明活干了。"
               "曾有一条连续 16 次 success 而生产端一页没产出，挂了 3.7 天。"
               "真实产出要看日志里的计数行。")
    return "\n".join(out)


# ── /rag ─────────────────────────────────────────────────────────
def cmd_rag(arg):
    q = (arg or "").strip()
    if not q:
        return "用法：`/rag 桂枝汤`"
    # 中文必须以 utf-8 字节发。曾因 shell 拼 JSON 把编码搞坏，服务端拿乱码去检索，
    # 还据此误报过一次"检索质量崩了"。
    body = json.dumps({"query": q, "topK": 5}).encode("utf-8")
    req = urllib.request.Request("https://gufangai.com/api/rag/search", data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA}, method="POST")
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    except Exception as e:
        return "查「%s」失败：`%s`" % (q, str(e)[:150])

    # 回显字段和发出去的不一致 = 传输把数据搞坏了，后面的结果全不作数
    if d.get("query") != q:
        return ("⚠ 查「%s」时服务端收到的是「%s」—— 请求编码被破坏，"
                "本次结果不作数。" % (q, d.get("query")))
    ch = d.get("chunks") or []
    if not ch:
        return "查「%s」召回 **0 条**。检索链路断了，或库里确实没有对应内容。" % q

    lines = ["### 检索「%s」· %d 条\n" % (q, len(ch)),
             "| 相似度 | 出处 | 片段 |", "|---|---|---|"]
    for c in ch:
        t = (c.get("text") or "").replace("\n", " ").replace("|", "｜")[:48]
        lines.append("| %.4f | %s | %s |"
                     % (c.get("score", 0), (c.get("book") or "?")[:26], t))
    return "\n".join(lines)


# ── /绑定 ────────────────────────────────────────────────────────
def cmd_bindings():
    if not (CF_ACC and CF_TOK):
        return "拿不到 CF 凭据（secrets 未配 CF_ACCOUNT_ID / CF_API_TOKEN_SCOPED）。"
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/pages/projects/guyaofang-web"
           % CF_ACC)
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + CF_TOK})
        p = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())["result"]
    except Exception as e:
        return "CF API 调用失败：`%s`" % str(e)[:150]

    prod = (p.get("deployment_configs") or {}).get("production") or {}
    ai = prod.get("ai_bindings")
    out = ["### 生产绑定实况\n"]
    # 红线：AI binding 一旦出现就是违规，与有没有免费额度无关
    out.append("- **CF Workers AI binding**：%s"
               % ("🔴 `%s` —— 违反红线，需立即拆除" % json.dumps(ai, ensure_ascii=False)
                  if ai else "✅ 无"))
    for name, meta in (prod.get("vectorize_bindings") or {}).items():
        idx = (meta or {}).get("index_name")
        warn = "  ⚠ 脏库，对任意查询过闸恒为 0" if (idx == "tcm-rag-768" and name == "VEC_TCM") else ""
        out.append("- Vectorize `%s` → `%s`%s" % (name, idx, warn))
    for k, label in (("d1_databases", "D1"), ("r2_buckets", "R2"),
                     ("kv_namespaces", "KV"), ("env_vars", "环境变量")):
        out.append("- %s：%d 项" % (label, len(prod.get(k) or {})))
    out.append("\n最近部署：`%s`"
               % (p.get("latest_deployment") or {}).get("created_on", "?")[:19])
    return "\n".join(out)


# ── /ocr ─────────────────────────────────────────────────────────
def cmd_ocr():
    runs = gh_json("repos/%s/actions/workflows/ocr_ndl.yml/runs?per_page=3" % REPO)
    rl = (runs or {}).get("workflow_runs") or []
    if not rl:
        return "拿不到 ocr_ndl 的 run 记录。"
    out = ["### OCR 主力线（ocr_ndl）最近 3 轮\n",
           "| 时间 | 触发 | 结论 | run |", "|---|---|---|---|"]
    for r in rl:
        out.append("| %s | %s | %s | [%s](%s) |"
                   % ((r.get("created_at") or "")[5:16].replace("T", " "),
                      r.get("event"), r.get("conclusion") or r.get("status"),
                      r.get("id"), r.get("html_url")))
    out.append("\n> 真实产出页数要点开 run 日志看 `done=` 汇总行。"
               "这里只报状态 —— 状态绿不等于产出不为零。")
    return "\n".join(out)


def main():
    if not BODY.startswith("/"):
        return
    parts = BODY.split(None, 1)
    cmd = parts[0].lstrip("/").lower()
    arg = parts[1] if len(parts) > 1 else ""

    table = {
        "帮助": lambda: HELP, "help": lambda: HELP, "?": lambda: HELP,
        "产线": cmd_pipelines, "pipelines": cmd_pipelines,
        "rag": lambda: cmd_rag(arg), "检索": lambda: cmd_rag(arg),
        "绑定": cmd_bindings, "bindings": cmd_bindings,
        "ocr": cmd_ocr,
    }
    fn = table.get(cmd)
    if not fn:
        post("未知指令 `%s`。\n\n%s" % (cmd, HELP))
        return
    try:
        post(fn())
    except Exception as e:
        # 失败也要回帖。静默失败等于这个系统不存在。
        post("执行 `/%s` 时出错：\n```\n%s\n```" % (cmd, str(e)[:600]))


main()
