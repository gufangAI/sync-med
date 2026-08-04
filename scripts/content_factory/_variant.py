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
import os, sys, time, uuid, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import d1, q          # noqa: E402  公共底座,不再抄一份

# 裁决阈值 —— 三个数决定"什么时候敢换冠军",写在一处别散落。
MIN_N  = int(os.environ.get("EVOLVE_MIN_N", "25"))     # 样本不够一律不判优劣(±1 条就能翻几个点)
MARGIN = float(os.environ.get("EVOLVE_MARGIN", "0.03"))  # 必须**显著**赢才换,防噪声换冠军
# 空字符串要当成"没设"(Actions 里未填的 input 传进来就是空串,float("") 会崩)
# 【2026-08-04 两位大佬评审后改为 0】原来默认 0.30,即挑战者直接吃 30% **生产流量**。
#   GPT:「绝不能让线上引擎边运行边自由修改自己」;Claude:「没有固定基准的 A/B 是掷骰子」。
#   实测也证明了:挑战者一度显示领先 3.5 个点,样本涨到 57 后变成落后 —— 因为两边
#   **跑的根本不是同一批题材**。现在裁决改由固定回归集(scripts/evolve/evaluate.py)供分,
#   生产线只跑已批准的冠军。要临时灰度就显式传 EVOLVE_EXPLORE,默认零。
EXPLORE = float(os.environ.get("EVOLVE_EXPLORE", "").strip() or "0")  # 运行面默认只跑冠军


def load_variant(scope, slot, default_body, explore=None):
    """取该产线该槽位**本轮要用**的方案;取不到一律回落 default_body。

    返回 (body, variant_id)。variant_id 为 None 表示走的是源码默认值。

    **A/B 分流(2026-08-04 补·闭环的关键一环)**:
      原来只读 active —— 那么新方案永远拿不到样本,`fitness` 永远是 NULL,
      裁决永远无从下手。**闭环在这里是断的:能生孩子,但孩子永远不许上场。**
      现在按 `explore` 比例把流量分给 status='trial' 的挑战者,
      让它自己去挣成绩;挣够 MIN_N 条再由 arbitrate() 判生死。
    """
    ex = EXPLORE if explore is None else explore
    try:
        if ex > 0 and random.random() < ex:
            ch = d1(f"SELECT variant_id, body FROM evolve_variants "
                    f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='trial' "
                    f"ORDER BY COALESCE(sample_n,0) ASC, updated_at DESC LIMIT 1")
            if ch and (ch[0].get("body") or "").strip():
                print(f"  [方案槽] 本轮走**挑战者** {ch[0]['variant_id']}(探索流量 {ex:.0%})", flush=True)
                return ch[0]["body"], ch[0]["variant_id"]
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
        # 【2026-08-04 当场抓到·会毁掉闭环的 bug】这里原来还有一句
        #     UPDATE evolve_variants SET fitness=入库率 ...
        #   —— 把**生成阶段的入库率**写进了变体的 fitness。
        #   实测后果:挑战者 v_5024541a2dea 显示 fitness=54.8%(n=31),
        #   而它名下真实数据是 17 条、复核只放行 1 条(**5.9%**);在位冠军的 3.3%
        #   却是货真价实的放行率。裁决拿**入库率**去比**放行率**,
        #   下一轮就会把一个更差的方案当冠军提拔上去 —— 进化方向直接反了。
        #   现在:record_trial 只记过程数(留痕、给 Improve 算子当燃料),
        #   **variants.fitness 只许 settle_trials 写**(唯一真源 = 复核放行率)。
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
        pv = str(r["prompt_ver"] or "")
        try:
            # 【2026-08-04 纠错·闭环断点二】原来这里是
            #     UPDATE ... WHERE scope=? AND status='active'
            #   —— 不管 prompt_ver 是谁的成绩,**一律写到 active 头上**。
            #   于是 A/B 跑出来的两组数字会互相覆盖,挑战者的成绩记在冠军名下,
            #   **裁决拿到的永远是同一个数,自动换冠军在结构上不可能成立。**
            # 现在按 variant_id 归位:产线写的 prompt_ver 就是它本轮用的 variant_id
            #   (老数据里是 "hf-v2-..." 这种字符串,归到 active,保持向后兼容)。
            if pv.startswith("v_"):
                d1(f"UPDATE evolve_variants SET fitness={q(round(fit,4))}, sample_n={judged}, "
                   f"updated_at={now} WHERE variant_id={q(pv)}")
            else:
                d1(f"UPDATE evolve_variants SET fitness={q(round(fit,4))}, sample_n={judged}, "
                   f"updated_at={now} WHERE scope={q(scope)} AND status='active'")
            out.append((pv, round(fit, 4), judged))
        except Exception:
            pass
    for pv, f, n in out:
        print(f"  [结算] {scope} {pv}: 复核放行率 {f:.1%}(判过 {n} 条)", flush=True)
    return out


def arbitrate(scope, slot, min_n=None, margin=None):
    """**裁决环** —— 挑战者够样本了就自动换冠军 / 自动杀 / 自动回滚。

    立此因(2026-08-04 创始人:「抓紧完成闭环的全自动进化系统」「不可以只是说,必须实现」):
      此前控制器里写死「**绝不自动换冠军**,只出建议开 Issue,人工放行」——
      那不是闭环,那是**把最后一步永远留给人**。人不点,系统就永远停在第一代。
      改成自动裁决,但**不是裸奔**,三道闸焊死:
        ① 样本闸:judged < MIN_N 一律不判(防 ±1 条翻盘的噪声换冠军);
        ② 显著闸:必须赢过冠军 MARGIN 以上才换(平手不换,减少抖动);
        ③ 回滚闸:换上去之后掉下来,自动退回父代 —— 这条才是敢自动的底气,
           **错了能自己退回去,比"永远不敢动"强**。
    合规红线不在这条链上(红线在代码里,提示词进化不到那儿),所以自动换是安全的。
    返回本次动作列表 [(动作, variant_id, 说明)]。
    """
    mn = MIN_N if min_n is None else min_n
    mg = MARGIN if margin is None else margin
    # 【2026-08-04 改回归集之后必须配套改闸】固定回归集只有一二十道题,
    #   写死 MIN_N=25 的话裁决**永远不会触发**;而题少时 1 道题就能翻好几个点。
    #   所以两个闸都随题量自适应:
    #     样本闸 = min(MIN_N, 半张卷子) —— 至少答了一半才算数
    #     显著闸 = max(MARGIN, 1.5 题的分值) —— 必须赢过"一道半题",不是赢在噪声上
    def _gates(n):
        n = max(int(n or 0), 1)
        return min(mn, max(6, n // 2)), max(mg, 1.5 / n)
    acts, now = [], int(time.time())
    try:
        rows = d1(f"SELECT variant_id,status,fitness,sample_n,parent_id,note FROM evolve_variants "
                  f"WHERE scope={q(scope)} AND slot={q(slot)} AND status IN ('active','trial')")
    except Exception as e:
        print(f"  [裁决] 取数失败:{type(e).__name__} {str(e)[:70]}", flush=True)
        return acts

    champ = next((r for r in rows if r["status"] == "active"), None)
    # 闸值按在位冠军答的题量算(两边同卷,题量一致)
    _mn, _mg = _gates((next((r for r in rows if r["status"] == "active"), {}) or {}).get("sample_n"))
    chals = [r for r in rows if r["status"] == "trial"
             and r.get("fitness") is not None and (r.get("sample_n") or 0) >= _mn]
    if not champ:
        print("  [裁决] 无在位冠军,跳过", flush=True)
        return acts

    cf = champ.get("fitness")
    # ③ 回滚闸:在位冠军是被提拔上来的(有父代),现在跑输父代 → 退回去
    if champ.get("parent_id") and cf is not None and (champ.get("sample_n") or 0) >= mn:
        par = d1(f"SELECT variant_id,fitness FROM evolve_variants WHERE variant_id={q(champ['parent_id'])}")
        pf = par[0].get("fitness") if par else None
        if pf is not None and cf + _mg < pf:
            d1(f"UPDATE evolve_variants SET status='rolled_back', updated_at={now} "
               f"WHERE variant_id={q(champ['variant_id'])}")
            d1(f"UPDATE evolve_variants SET status='active', updated_at={now} "
               f"WHERE variant_id={q(champ['parent_id'])}")
            acts.append(("回滚", champ["variant_id"],
                         f"上任后 {cf:.1%} < 父代 {pf:.1%},自动退回 {champ['parent_id']}"))
            print(f"  [裁决] ⏪ 回滚 {champ['variant_id']}({cf:.1%})→ 父代 {champ['parent_id']}({pf:.1%})", flush=True)
            return acts

    # 防呆一:两边分数必须来自**同一张回归集卷子**。
    #   拿不同卷子的分数比高下 = 掷骰子(2026-08-04 实测翻车过一次:挑战者"领先"3.5 个点,
    #   换成同一批题材后其实是落后)。评测集不同 → 不裁决。
    ch_ev = {r.get("evaluation_dataset") for r in rows if r.get("evaluation_dataset")}
    if len(ch_ev) > 1:
        print(f"  [裁决] 各方案的回归集不一致 {ch_ev},本轮不裁决(不许跨卷比分)", flush=True)
        return acts

    # 防呆二:两边的 fitness 必须**同源**。
    #   在位冠军还没被结算过就不许裁决 —— 拿一个空值或旧口径去比,
    #   等于让裁决在瞎猜(今天已经被入库率骗过一次,这道防呆不能省)。
    if cf is None:
        print("  [裁决] 在位冠军尚无结算过的放行率,本轮不裁决(避免拿不同口径的数字比)", flush=True)
        return acts
    for ch in sorted(chals, key=lambda r: -(r["fitness"] or 0)):
        f, n = ch["fitness"], ch["sample_n"]
        if f >= cf + _mg:                                   # ② 显著赢 → 换冠军
            d1(f"UPDATE evolve_variants SET status='retired', updated_at={now} "
               f"WHERE variant_id={q(champ['variant_id'])}")
            d1(f"UPDATE evolve_variants SET status='active', updated_at={now} "
               f"WHERE variant_id={q(ch['variant_id'])}")
            acts.append(("换冠军", ch["variant_id"], f"{f:.1%}(n={n})胜过在位 {cf:.1%}"))
            print(f"  [裁决] 🏆 换冠军 {ch['variant_id']} {f:.1%}(n={n})← 原 {champ['variant_id']}", flush=True)
            break
        if f + _mg < cf:                                    # 显著输 → 杀掉,别再吃流量
            d1(f"UPDATE evolve_variants SET status='killed', updated_at={now} "
               f"WHERE variant_id={q(ch['variant_id'])}")
            acts.append(("淘汰", ch["variant_id"], f"{f:.1%}(n={n})显著输给在位 {cf:.1%}"))
            print(f"  [裁决] ✗ 淘汰 {ch['variant_id']} {f:.1%}(n={n})", flush=True)
        elif n >= _mn * 2:
            # ④ **让位闸(2026-08-04 实测补)** —— 打平的挑战者必须退场,否则闭环卡死。
            #   当场撞上:本草挑战者 1.8%(n=57)vs 冠军 2.4%,既不够格上位、也没差到该杀,
            #   于是它永远挂在 trial 上;而 improve_one 看到"已有挑战者在跑"就不肯生下一代
            #   —— **循环停在这里,再也不会有第三代**。
            #   规则:样本给到两倍闸值还没赢,就算它这一代的机会用完了,退场让下一代上。
            #   保留成绩不删数据,status='lost' 仍可回溯这一支为什么没成。
            d1(f"UPDATE evolve_variants SET status='lost', updated_at={now} "
               f"WHERE variant_id={q(ch['variant_id'])}")
            acts.append(("让位", ch["variant_id"],
                         f"{f:.1%}(n={n})与在位 {cf:.1%} 打平,机会用完退场,让下一代上"))
            print(f"  [裁决] ↩ 让位 {ch['variant_id']} {f:.1%}(n={n})打平在位 {cf:.1%},退场", flush=True)
    if not acts:
        print(f"  [裁决] {scope}/{slot} 本轮无动作(挑战者 {len(chals)} 个够样本,"
              f"闸值 样本>={_mn} 显著>={_mg:.1%})", flush=True)
    return acts
