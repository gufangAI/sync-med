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

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB      = os.environ.get("D1_DATABASE_ID", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GH_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GH_REPO    = os.environ.get("GITHUB_REPOSITORY", "hosonzuo8848/sync-med")

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
    # 第4/5步要用:当前策略 + 历史经验
    safe("policy",        lambda: d1("SELECT policy_key,scope,dimension,value,auto_managed FROM evolve_policy"), [])
    safe("memory",        lambda: d1("SELECT scope,dimension,key_name,trials,success,rejected,failed,score "
                                     "FROM evolve_memory ORDER BY score DESC"), [])
    # 第1步「目标」+ 停止条件(2026-08-02 联网核对 Loop 行业标准后补)
    safe("goals",         lambda: d1("SELECT goal_key,scope,metric,target_value,current_value,unit,"
                                     "max_rounds,rounds_used,no_progress_rounds,no_progress_limit,"
                                     "last_value,status FROM evolve_goals WHERE status='running'"), [])
    return o


# ══ 【第1步 目标 + 停止条件】═════════════════════════════════════
# 行业结论(2026 Loop Engineering):
#   · 好目标必须**可判定终止** —— "Make the tests pass" 好,"improve the code" 坏。
#   · 每个 loop 必须焊三道硬停:最大轮次 / 无进展检测 / 重试上限。
#   我们此前一道都没有 —— cron 无脑跑,题材枯竭了还空转烧配额,这是真隐患。
def track_goals(o, dry):
    """回填当前值、判定达标或触发硬停。返回 (达标列表, 停摆列表, 进行中列表)。"""
    now = o["at"]
    reached, halted, running = [], [], []
    cur_map = {
        "content_biocomp": o.get("bio_total") or 0,
        "content_herb":    o.get("herb_total") or 0,
        "model_chat":      ((o.get("champion") or [{}])[0].get("score") or 0),
    }
    for g in (o.get("goals") or []):
        cur = float(cur_map.get(g["scope"], 0) or 0)
        tgt = float(g["target_value"])
        last = g.get("last_value")
        rounds = (g.get("rounds_used") or 0) + 1
        # ② 无进展检测:这轮数值没涨 → 计数 +1
        nop = (g.get("no_progress_rounds") or 0)
        nop = 0 if (last is None or cur > float(last)) else nop + 1

        status, reason = "running", None
        if cur >= tgt:
            status, reason = "reached", f"已达标 {cur}/{tgt}{g.get('unit') or ''}"
            reached.append((g["goal_key"], reason))
        elif g.get("max_rounds") and rounds >= int(g["max_rounds"]):
            status, reason = "halted_max_rounds", f"到轮次上限 {rounds}/{g['max_rounds']},停"
            halted.append((g["goal_key"], reason))
        elif nop >= int(g.get("no_progress_limit") or 3):
            status, reason = "halted_no_progress", f"连续 {nop} 轮无进展(停在 {cur}/{tgt}),停并报警"
            halted.append((g["goal_key"], reason))
        else:
            pct = (cur / tgt * 100) if tgt else 0
            running.append((g["goal_key"], f"{cur:g}/{tgt:g}{g.get('unit') or ''} ({pct:.0f}%)"
                                           + (f" · 已 {nop} 轮无进展" if nop else "")))
        if not dry:
            try:
                d1(f"UPDATE evolve_goals SET current_value={cur}, last_value={cur}, "
                   f"rounds_used={rounds}, no_progress_rounds={nop}, status='{status}', "
                   f"halt_reason={'NULL' if not reason else chr(39)+reason.replace(chr(39),'')+chr(39)}, "
                   f"updated_at={now} WHERE goal_key='{g['goal_key']}'")
            except Exception as e:
                print(f"  目标回填失败 {g['goal_key']}: {str(e)[:90]}")
    return reached, halted, running


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


# ══ 【第5步 记忆与沉淀】把每轮表现写进 evolve_memory ════════════
# 立此因(2026-08-02 Loop Engineering 六步图自评):
#   此前控制器只有「观察→判断→报警」,**没有第4步反思改进、第5步只沉淀数据没沉淀经验**。
#   每轮内容工厂都从零开始,不知道上一轮哪个策略好用 —— 那不叫进化,叫重复。
def consolidate(o, dry):
    """把 24h 内各产线的表现按「策略维度」累计进记忆表。"""
    now = o["at"]
    wrote = 0
    for r in (o.get("runs_24h") or []):
        tgt = r.get("target") or ""
        scope = "content_biocomp" if "biocomp" in tgt else ("content_herb" if "herb" in tgt else tgt)
        req  = r.get("req") or 0
        ins  = r.get("ins") or 0
        rej  = r.get("rej") or 0
        fail = r.get("fail") or 0
        if req <= 0:
            continue
        # 当前策略取值 = 这段经验挂在哪个 key 上
        pol = {p["dimension"]: p["value"] for p in (o.get("policy") or []) if p.get("scope") == scope}
        for dim, val in pol.items():
            if dim not in ("topic_source", "batch_size"):
                continue
            # 综合评分:成功率 - 违规重罚 - 失败惩罚(与赛马同一套价值观:合规零容忍)
            score = round((ins / req) * 100 - (rej / req) * 60 - (fail / req) * 30, 2)
            key = f"{scope}|{dim}|{val}".replace("'", "")
            sql = (
                "INSERT INTO evolve_memory (mem_id,scope,dimension,key_name,trials,success,rejected,"
                "failed,score,last_used_at,updated_at) VALUES "
                f"('{key}','{scope}','{dim}','{val}',{req},{ins},{rej},{fail},{score},{now},{now}) "
                "ON CONFLICT(scope,dimension,key_name) DO UPDATE SET "
                "trials=trials+excluded.trials, success=success+excluded.success, "
                "rejected=rejected+excluded.rejected, failed=failed+excluded.failed, "
                "score=(COALESCE(score,0)*0.7 + excluded.score*0.3), "   # 滑动平均:新样本占三成
                "last_used_at=excluded.last_used_at, updated_at=excluded.updated_at")
            if dry:
                print(f"  (dry) 沉淀 {scope}.{dim}={val} → 评分 {score}")
            else:
                try:
                    d1(sql); wrote += 1
                except Exception as e:
                    print(f"  沉淀失败 {key}: {str(e)[:90]}")
    return wrote


# ══ 【第4步 反思与改进】按记忆改策略(有边界的自动改)═══════════
# 边界:只改 auto_managed=1 的策略;每轮最多改 1 项(避免多变量同时动导致归因不清);
#       改动必须有 ≥2 次试验样本支撑;每次改都写审计并保留 prev_value 供一键回滚。
def improve(o, dry):
    acts = []
    mem = o.get("memory") or []
    pol = {p["policy_key"]: p for p in (o.get("policy") or [])}
    if not mem:
        return acts

    # 按 (scope,dimension) 分组,挑评分最高的取值
    best = {}
    for m in mem:
        k = (m["scope"], m["dimension"])
        if m.get("trials", 0) < 2:      # 样本太少不作数,防噪声
            continue
        if k not in best or (m.get("score") or -999) > (best[k].get("score") or -999):
            best[k] = m

    for (scope, dim), m in best.items():
        pk = f"{scope}.{dim}"
        cur = pol.get(pk)
        if not cur or not cur.get("auto_managed"):
            continue
        if cur["value"] == m["key_name"]:
            continue                     # 现行策略已是最优,不动
        # 只有明显更优才改(差 ≥8 分),避免来回抖
        cur_mem = next((x for x in mem if x["scope"] == scope and x["dimension"] == dim
                        and x["key_name"] == cur["value"]), None)
        cur_score = (cur_mem or {}).get("score")
        if cur_score is not None and (m.get("score") or 0) - cur_score < 8:
            continue
        act = {"policy_key": pk, "from": cur["value"], "to": m["key_name"],
               "from_score": cur_score, "to_score": m.get("score"), "trials": m.get("trials")}
        acts.append(act)
        if not dry:
            try:
                d1(f"UPDATE evolve_policy SET prev_value=value, value='{m['key_name']}', "
                   f"reason='控制器据 evolve_memory 自动改进(评分 {m.get('score')} vs {cur_score},样本 {m.get('trials')})', "
                   f"changed_by='controller', updated_at={o['at']} WHERE policy_key='{pk}'")
                d1("INSERT INTO evolve_actions (action_id,scope,action,detail,evidence,auto_applied,created_at) "
                   f"VALUES ('{pk}-{o['at']}','{scope}','policy_change',"
                   f"'{json.dumps(act, ensure_ascii=False)}','evolve_memory',1,{o['at']})")
            except Exception as e:
                print(f"  改进写库失败 {pk}: {str(e)[:90]}")
        break        # ★ 每轮只改一项:多变量同时动会让下一轮无法归因
    return acts


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


# 常驻 Issue 的标题前缀。同一条产线永远复用这一个 Issue,不再每轮新开。
STANDING_TITLE = "🧬 自进化控制器 · 待处理"


def open_issue(title, body):
    """维护**一个**常驻 Issue,而不是每轮开一个新的。

    立此因(2026-08-27 全仓体检):本脚本挂在 cron '40 */6 * * *' 上 = 一天四轮,
    每轮 POST 一个新 Issue。实测:仓里 91 个自进化控制器 Issue 全部 open、
    全部 0 条回复。出口不是没有,是**太多等于没有**。

    实现在 scripts/gh_issue.py —— 那是规范实现,不在这里再手抄一份。
    仓里这段逻辑已经手抄了四遍(workflow_sentry / gateway_sentry / intake_sentry /
    team_report_audit),而且四份都漏了同一件事:只 PATCH 正文、从不发评论,
    **而编辑正文不产生任何通知**。gh_issue.upsert 默认 notify=True 补上那一半。
    """
    from gh_issue import upsert
    return upsert(GH_REPO, "self-evolve", STANDING_TITLE.split(" · ")[0],
                  title, body)


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

    # 第1步:目标追踪 + 三道硬停(Loop 五要素最后两项)
    reached, halted, running = track_goals(o, args.dry_run)
    print(f"\n【目标与停止条件】")
    for k, s in running:
        print(f"  ▶ {k:<26} {s}")
    for k, s in reached:
        print(f"  🎯 {k:<26} {s}")
    for k, s in halted:
        print(f"  🛑 {k:<26} {s}")
    if not (running or reached or halted):
        print("  (无进行中目标)")

    # 第5步:记忆与沉淀
    n = consolidate(o, args.dry_run)
    print(f"\n【记忆与沉淀】写入 {n} 条策略经验(记忆库现有 {len(o.get('memory') or [])} 条)")

    # 第4步:反思与改进(每轮最多改 1 项,有样本与差值门槛)
    acts = improve(o, args.dry_run)
    print(f"\n【反思与改进】")
    if acts:
        for a in acts:
            print(f"  🔧 策略调整 {a['policy_key']}:{a['from']} → {a['to']}"
                  f"(评分 {a['from_score']} → {a['to_score']},样本 {a['trials']} 次)")
            if args.dry_run:
                print("     (dry-run,未写库)")
    else:
        print("  维持现行策略(样本不足 / 现行已最优 / 差值未达门槛)")

    sw = suggest_switch(o)
    print(f"\n【修正】")
    body_parts = []
    if acts and not args.dry_run:
        body_parts.append("### 🔧 本轮自动调整的策略\n\n" +
                          "\n".join(f"- `{a['policy_key']}`:`{a['from']}` → `{a['to']}`"
                                    f"(评分 {a['from_score']} → {a['to_score']},样本 {a['trials']} 次)"
                                    for a in acts) +
                          "\n\n回滚:`UPDATE evolve_policy SET value=prev_value, changed_by='human' WHERE policy_key='...'`")
    if sw:
        print(f"  🔔 建议换将:{sw['from']}({sw['from_score']}) → {sw['to']}({sw['to_score']}),领先 {sw['gap']} 分")
        print(f"     ★ 不自动执行 —— 人工放行闸。批准后跑:")
        print("     " + sw["sql"].replace("\n", "\n     "))
        body_parts.append(f"### 🔔 建议换将\n\n"
                          f"`{sw['from']}`({sw['from_score']}) → `{sw['to']}`({sw['to_score']}),领先 **{sw['gap']}** 分\n\n"
                          f"**不自动执行**(人工放行闸)。批准后执行:\n```sql\n{sw['sql']}\n```")
    else:
        print("  维持现状:冠军仍是最优或领先不足阈值")

    # 目标达标 / 硬停 都必须上报 —— 这两件事最需要人知道
    if reached:
        body_parts.append("### 🎯 目标已达标(请决定下一阶段目标或收工)\n\n" +
                          "\n".join(f"- `{k}`:{s}" for k, s in reached))
    if halted:
        body_parts.append("### 🛑 触发硬停(loop 已停,需人工介入)\n\n" +
                          "\n".join(f"- `{k}`:{s}" for k, s in halted) +
                          "\n\n恢复:`UPDATE evolve_goals SET status='running', no_progress_rounds=0 "
                          "WHERE goal_key='...'`(先查清为什么停,别直接重启)")
    if issues:
        body_parts.append("### ⚠️ 需要人工处理\n\n" + "\n".join("- " + s for s in issues))
    if body_parts:
        body_parts.append(f"\n---\n\n**观察快照**\n```json\n"
                          f"{json.dumps({k: v for k, v in o.items() if k != 'err'}, ensure_ascii=False, indent=1)}\n```")
        if args.dry_run:
            print("\n  (--dry-run,不开 Issue)")
        else:
            # 标题带最新时间与待处理数(一眼看新鲜度),但**前缀固定** ——
            # open_issue 靠这个前缀找到同一个常驻 Issue,不再每轮新开一个。
            url = open_issue(
                f"{STANDING_TITLE} · {len(issues)} 项 · 更新于 {time.strftime('%Y-%m-%d %H:%M')}",
                "\n\n".join(body_parts))
            if url:
                print(f"  已开 Issue: {url}")
    else:
        print("  无需人工介入,不开 Issue(避免噪声)")

    print("\n" + "═" * 66)


if __name__ == "__main__":
    main()
