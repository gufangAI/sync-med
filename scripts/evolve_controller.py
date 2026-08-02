#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自进化控制器 —— 把 Loop 的「观察 → 判断 → 修正」装进系统

立此因(2026-08-02 创始人):「那 LOOP 是不是就可以实现我们的自己成长进化的系统了?」
                          「那如何在我们的系统中部署呢,你要想想」

答案:`/loop` 绑在 Claude 会话上,会话一关就停 —— 那是「驾驶员开自动巡航」,不是「无人车」。
真正装进系统的做法,是把 Loop 的四步做成 cron:

    现在的 cron:  执行 → 执行 → 执行            (跑一万次还是原样)
    加上本控制器:执行 → 观察 → 判断 → 修正 → 再执行   (越跑越好)

本控制器**不干活**,只做后三步:
  【观察】读各产线的真实指标(content_gen_runs / race_results / model_registry / 内容表)
  【判断】按阈值规则判定健康度
  【修正】① 能自动改的 → 直接写 D1(换冠军模型等,**配置在库里机器才改得动**)
          ② 改不了的   → 开 GitHub Issue 让人改(prompt / 代码 / 策略)

关键设计:**能被机器自动修正的,必须是数据不是代码。**
这正是 model_registry 存在的意义 —— 配置在库里,控制器就能调;配置在代码里,只有人能改。

安全边界(不可放松):
  · **绝不自动换冠军**:只在挑战者领先 ≥ SWITCH_MARGIN 且零合规违规时**写建议 + 开 Issue**,
    真正换将仍需人工放行(说好的那 10%)。
  · 只读+写少量受控字段,绝不删数据、绝不改 published 内容。
  · 任一子检查异常都只记"查询失败",绝不阻断其余检查。

用法(Actions):
  D1_API_TOKEN=... GITHUB_TOKEN=... python scripts/evolve_controller.py
本地只看不写:
  D1_API_TOKEN=... python scripts/evolve_controller.py --dry-run
"""
import os, sys, json, time, argparse, urllib.request

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "b7362ed77d212bab298a9ae8736c9868")
D1_DB      = os.environ.get("D1_DATABASE_ID", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GH_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GH_REPO    = os.environ.get("GITHUB_REPOSITORY", "gufangAI/sync-med")

# ── 判据阈值(改这里就改了系统的"健康标准")─────────────────────
TH = {
    "content_min_per_day": 20,      # 内容工厂:每天至少新增这么多条,低于=产线偏慢
    "compliance_max_rate": 0.05,    # 合规拦截率上限 5%,超了说明提示词漂了
    "fail_max_rate":       0.20,    # 生成失败率上限 20%,超了说明模型或网关有问题
    "switch_margin":       5.0,     # 挑战者领先冠军多少分才建议换将
    "race_stale_hours":    72,      # 赛马超过这么久没跑 = 评估环停摆
    "radar_stale_hours":   36,      # 情报雷达超过这么久没落库 = 发现环停摆
}


def d1(sql):
    if not D1_TOKEN:
        sys.exit("缺 D1_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"sql": sql}).encode("utf-8"),
        headers={"Authorization": "Bearer " + D1_TOKEN, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=90).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:200])
    return (j.get("result") or [{}])[0].get("results") or []


def scalar(sql, default=0):
    try:
        r = d1(sql)
        return list(r[0].values())[0] if r else default
    except Exception:
        return default


# ══ 【观察】读真实指标 ══════════════════════════════════════════
def observe():
    now = int(time.time())
    day = now - 86400
    o = {"at": now, "err": []}

    def safe(key, fn, default=None):
        try:
            o[key] = fn()
        except Exception as e:
            o[key] = default
            o["err"].append(f"{key}: {type(e).__name__} {str(e)[:80]}")

    # 内容工厂
    safe("herb_total",    lambda: scalar("SELECT COUNT(*) FROM herb_compare"))
    safe("bio_total",     lambda: scalar("SELECT COUNT(*) FROM biocomp_entries"))
    safe("content_24h",   lambda: scalar(f"SELECT COUNT(*) FROM biocomp_entries WHERE created_at>{day}")
                                 + scalar(f"SELECT COUNT(*) FROM herb_compare WHERE created_at>{day}"))
    safe("runs_24h",      lambda: d1(f"SELECT target,SUM(requested) req,SUM(inserted) ins,"
                                     f"SUM(compliance_reject) rej,SUM(failed) fail "
                                     f"FROM content_gen_runs WHERE started_at>{day} GROUP BY target"), [])
    # 模型池
    safe("champion",      lambda: d1("SELECT model_key,provider,score FROM model_registry "
                                     "WHERE task='chat' AND role='champion' LIMIT 1"), [])
    safe("board",         lambda: d1("SELECT model_key,provider,role,score FROM model_registry "
                                     "WHERE task='chat' AND enabled=1 AND score IS NOT NULL "
                                     "ORDER BY score DESC LIMIT 5"), [])
    safe("batch_models",  lambda: scalar("SELECT COUNT(*) FROM model_registry WHERE task='batch'"))
    safe("last_race",     lambda: scalar("SELECT MAX(created_at) FROM race_results"))
    # 发现环
    safe("candidates",    lambda: scalar("SELECT COUNT(*) FROM model_candidates"))
    safe("last_discover", lambda: scalar("SELECT MAX(discovered_at) FROM model_candidates"))
    return o


# ══ 【判断】按阈值出结论 ═══════════════════════════════════════
def judge(o):
    now = o["at"]
    issues, oks = [], []

    c24 = o.get("content_24h") or 0
    (oks if c24 >= TH["content_min_per_day"] else issues).append(
        f"内容工厂 24h 新增 {c24} 条(阈值 ≥{TH['content_min_per_day']})")

    for r in (o.get("runs_24h") or []):
        req = r.get("req") or 0
        if req <= 0:
            continue
        rej_rate = (r.get("rej") or 0) / req
        fail_rate = (r.get("fail") or 0) / req
        tgt = r.get("target")
        if rej_rate > TH["compliance_max_rate"]:
            issues.append(f"⚠️ {tgt} 合规拦截率 {rej_rate:.0%} 超阈值 {TH['compliance_max_rate']:.0%}"
                          f" —— 提示词可能漂了,需要人工看 prompt")
        if fail_rate > TH["fail_max_rate"]:
            issues.append(f"⚠️ {tgt} 生成失败率 {fail_rate:.0%} 超阈值 {TH['fail_max_rate']:.0%}"
                          f" —— 模型或网关异常")

    lr = o.get("last_race") or 0
    if lr and (now - lr) > TH["race_stale_hours"] * 3600:
        issues.append(f"⚠️ 评估环停摆:赛马已 {int((now-lr)/3600)} 小时没跑(阈值 {TH['race_stale_hours']}h)")
    elif lr:
        oks.append(f"评估环活着:赛马 {int((now-lr)/3600)} 小时前跑过")

    ld = o.get("last_discover") or 0
    if ld and (now - ld) > TH["radar_stale_hours"] * 3600:
        issues.append(f"⚠️ 发现环停摆:候选池已 {int((now-ld)/3600)} 小时没进新货")
    elif ld:
        oks.append(f"发现环活着:候选池 {int((now-ld)/3600)} 小时前有新货,库存 {o.get('candidates')} 个")

    return issues, oks


# ══ 【修正】能自动改的改,改不了的开 Issue ═══════════════════════
def suggest_switch(o):
    """挑战者显著强于冠军 → 出换将建议。**不自动执行**,人工放行闸。"""
    champ = (o.get("champion") or [None])[0]
    board = o.get("board") or []
    if not champ or not board:
        return None
    top = board[0]
    if top["model_key"] == champ["model_key"]:
        return None
    gap = (top.get("score") or 0) - (champ.get("score") or 0)
    if gap < TH["switch_margin"]:
        return None
    return {
        "from": champ["model_key"], "from_score": champ.get("score"),
        "to": top["model_key"], "to_score": top.get("score"), "gap": round(gap, 2),
        "sql": (f"UPDATE model_registry SET role='contender' WHERE model_key='{champ['model_key']}';\n"
                f"UPDATE model_registry SET role='champion'  WHERE model_key='{top['model_key']}';"),
    }


def open_issue(title, body):
    if not GH_TOKEN:
        print("  (无 GITHUB_TOKEN,跳过开 Issue)", flush=True)
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/issues", method="POST",
        data=json.dumps({"title": title, "body": body, "labels": ["self-evolve"]}).encode("utf-8"),
        headers={"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "evolve-controller"})
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=45).read())
        return j.get("html_url")
    except Exception as e:
        print(f"  开 Issue 失败: {str(e)[:120]}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只观察判断,不开 Issue")
    args = ap.parse_args()

    print("═" * 66)
    print("自进化控制器 · 观察 → 判断 → 修正")
    print("═" * 66)

    o = observe()
    print(f"\n【观察】")
    print(f"  内容:本草 {o.get('herb_total')} · 生物计算 {o.get('bio_total')} · 24h 新增 {o.get('content_24h')}")
    print(f"  模型:chat 冠军 {(o.get('champion') or [{}])[0].get('model_key','?')} · batch 池 {o.get('batch_models')} 个")
    print(f"  候选:{o.get('candidates')} 个")
    if o["err"]:
        print(f"  ⚠️ 查询失败项:{o['err']}")

    issues, oks = judge(o)
    print(f"\n【判断】正常 {len(oks)} 项 · 异常 {len(issues)} 项")
    for s in oks:
        print(f"  ✓ {s}")
    for s in issues:
        print(f"  {s}")

    sw = suggest_switch(o)
    print(f"\n【修正】")
    body_parts = []
    if sw:
        print(f"  🔔 建议换将:{sw['from']}({sw['from_score']}) → {sw['to']}({sw['to_score']}),领先 {sw['gap']} 分")
        print(f"     ★ 不自动执行 —— 人工放行闸。批准后跑:")
        print("     " + sw["sql"].replace("\n", "\n     "))
        body_parts.append(f"### 🔔 建议换将\n\n"
                          f"`{sw['from']}`({sw['from_score']}) → `{sw['to']}`({sw['to_score']}),领先 **{sw['gap']}** 分\n\n"
                          f"**不自动执行**(人工放行闸)。批准后执行:\n```sql\n{sw['sql']}\n```")
    else:
        print("  维持现状:冠军仍是最优或领先不足阈值")

    if issues:
        body_parts.append("### ⚠️ 需要人工处理\n\n" + "\n".join("- " + s for s in issues))
    if body_parts:
        body_parts.append(f"\n---\n\n**观察快照**\n```json\n"
                          f"{json.dumps({k: v for k, v in o.items() if k != 'err'}, ensure_ascii=False, indent=1)}\n```")
        if args.dry_run:
            print("\n  (--dry-run,不开 Issue)")
        else:
            url = open_issue(f"🧬 自进化控制器 · {time.strftime('%Y-%m-%d %H:%M')} · {len(issues)} 项待处理",
                             "\n\n".join(body_parts))
            if url:
                print(f"  已开 Issue: {url}")
    else:
        print("  无需人工介入,不开 Issue(避免噪声)")

    print("\n" + "═" * 66)


if __name__ == "__main__":
    main()
