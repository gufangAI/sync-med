#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案槽(variant)读写 —— 自我进化闭环的**接口层**

立此因(2026-08-03 创始人):
  「你要留接口,目前我们是经过你去改进系统,未来接入 Claude 的 API,就可以云端调用了!
    那才是真的全自动运行」
  + 当天追问「昨晚说的打通自我进化,成功了吗?」—— 查真实状态:**三张表一张没建、
    提示词仍写死在代码里、load_variant 零处**。只画了图没盖楼,这里是补盖。

为什么这一层是命门:
  提示词写死在 Python 常量里 → 每次改进都必须经过一个**能改代码、能 git push 的主体**(也就是我)。
  **闭环在结构上就离不开人。** 把方案搬进 D1,改进者就变成可插拔角色:
    今天 author='cto'(我写) → 将来 author='claude-api'(云端脚本写),**产线代码一行不用改**。

铁律:
  · 读不到 active 方案 → **回落源码里的默认常量**,库挂了产线照跑,绝不因为这层新增而停摆
  · **合规硬闸不进库**:BANNED 词表、认知档、确定性校验永远留在代码里。
    提示词可以进化,**红线不许进化**。
"""
import os, sys, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import d1, q          # noqa: E402  公共底座,不再抄一份


def load_variant(scope, slot, default_body):
    """取该产线该槽位当前生效的方案;取不到一律回落 default_body。

    返回 (body, variant_id)。variant_id 为 None 表示走的是源码默认值。
    """
    try:
        rows = d1(f"SELECT variant_id, body FROM evolve_variants "
                  f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='active' "
                  f"ORDER BY updated_at DESC LIMIT 1")
        if rows and (rows[0].get("body") or "").strip():
            return rows[0]["body"], rows[0]["variant_id"]
    except Exception as e:
        print(f"  [方案槽] 读库失败,回落源码默认值:{type(e).__name__} {str(e)[:70]}", flush=True)
    return default_body, None


def seed_variant(scope, slot, body, author="cto", note=""):
    """把源码里的默认提示词**登记成第一代方案**(active)。已有 active 就不动。"""
    try:
        cur = d1(f"SELECT variant_id FROM evolve_variants "
                 f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='active' LIMIT 1")
        if cur:
            return cur[0]["variant_id"], False
        vid = "v_" + uuid.uuid4().hex[:12]
        now = int(time.time())
        d1(f"INSERT INTO evolve_variants (variant_id,scope,slot,body,status,mode,author,note,created_at,updated_at) "
           f"VALUES ({q(vid)},{q(scope)},{q(slot)},{q(body)},'active','draft',{q(author)},{q(note)},{now},{now})")
        return vid, True
    except Exception as e:
        print(f"  [方案槽] 登记失败:{type(e).__name__} {str(e)[:70]}", flush=True)
        return None, False


def record_trial(variant_id, scope, *, passed=0, held=0, failed=0, sample_n=0,
                 feedback="", run_ref=""):
    """把一轮真跑的成绩与**失败反馈**写回 —— 这是 Improve/Debug 算子的燃料。

    fitness 目前 = 放行率。分开存 raw_score 与 fitness,是为了以后能表达
    "分数一般但有潜力"(照 GraphRAG 的 Program 把 score/reward/fitness 分开的做法)。
    """
    if not variant_id:
        return None
    try:
        n = max(sample_n or (passed + held + failed), 0)
        rate = (passed / n) if n else None
        tid = "t_" + uuid.uuid4().hex[:12]
        now = int(time.time())
        d1(f"INSERT INTO evolve_trials (trial_id,variant_id,scope,raw_score,fitness,sample_n,"
           f"passed,held,failed,feedback,run_ref,created_at) VALUES ("
           f"{q(tid)},{q(variant_id)},{q(scope)},{q(rate)},{q(rate)},{n},"
           f"{int(passed)},{int(held)},{int(failed)},{q(feedback[:1500])},{q(run_ref)},{now})")
        if rate is not None:
            d1(f"UPDATE evolve_variants SET fitness={q(rate)}, sample_n={n}, updated_at={now} "
               f"WHERE variant_id={q(variant_id)}")
        return tid
    except Exception as e:
        print(f"  [方案槽] 记轨迹失败:{type(e).__name__} {str(e)[:70]}", flush=True)
        return None


def settle_trials(scope):
    """结算适应度 —— **复核跑完之后**才调。

    【2026-08-04 纠错】`record_trial` 在生成阶段就写 fitness,那一刻拿到的只有**入库率**
      (生成出来的条目有没有成功写进库),实测是 0.83~1.00,**看不出好坏**。
      而同一批数据的**真实复核放行率**是:hf-v1 8%(8/103)、hf-v2 16%(39/238)。
      拿入库率当适应度,进化会朝「多产」优化而不是「更准」—— **方向正好反了**。
    正解:生成阶段只记过程数,**适应度等复核判完再结算**。
      fitness = 放行 / (放行 + 按住),draft(还没复核的)不计入分母。
    """
    tbl = "biocomp_entries" if scope == "biocomp" else "herb_compare"
    try:
        rows = d1(f"SELECT prompt_ver, "
                  f"SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) p, "
                  f"SUM(CASE WHEN status='held' THEN 1 ELSE 0 END) h "
                  f"FROM {tbl} WHERE prompt_ver IS NOT NULL GROUP BY prompt_ver")
    except Exception as e:
        print(f"  [结算] 取数失败:{type(e).__name__} {str(e)[:70]}", flush=True)
        return []

    out = []
    now = int(time.time())
    for r in rows:
        judged = (r["p"] or 0) + (r["h"] or 0)
        if judged < 20:          # 样本太少不许判定优劣 —— ±1 条就能翻几个百分点
            continue
        fit = (r["p"] or 0) / judged
        try:
            # 按 slot 归到当前 active 方案上(一个 prompt_ver 对应一代方案)
            d1(f"UPDATE evolve_variants SET fitness={q(round(fit,4))}, sample_n={judged}, "
               f"updated_at={now} WHERE scope={q(scope)} AND status='active'")
            out.append((r["prompt_ver"], round(fit, 4), judged))
        except Exception:
            pass
    for pv, f, n in out:
        print(f"  [结算] {scope} {pv}: 复核放行率 {f:.1%}(判过 {n} 条)", flush=True)
    return out
