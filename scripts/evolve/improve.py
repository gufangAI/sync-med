#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Improve 算子 —— 自我进化闭环的**心脏**:让系统自己生出下一代方案

立此因(2026-08-04 创始人):
  「要抓紧完成这个闭环,这是最值钱的部分,我们就真的要开始融钱了」
  「我们这几天也在讨论这个问题,也说了是要触发你去审核改进!**不可以只是说,必须实现**」

此前闭环缺的正是这一环。原来的链条是:
    产线跑 → 复核判 → 记 fitness → **然后没了**
  能量到最后一步就散掉:没有任何东西会因为"这一代只有 4.6% 放行率"而改变。
  真正的闭环必须让**成绩反过来改写方案**:
    产线跑 → 复核判 → 结算 fitness → **诊断失败原因 → 生成下一代 → A/B 抢流量 → 裁决换冠军**
                                        ↑___________________________________________|

三层各司其职(创始人 2026-08-03 钦定的命名):
  · info4AI      → `evolve_candidates`:外面出了什么新技术/新模型(进化的**来源**)
  · QA-Evolution → `evolve_trials`:改进者本身有没有变强(进化的**度量**)
  · sueAI        → 本脚本:真的去改(进化的**动作**)

改进者是**可插拔角色**(创始人:「你要留接口,未来接入 Claude 的 API 就是真的全自动」):
  --improver freepool    默认。走内部免费池网关,零按量计费。
  --improver claude-api  接口已焊好,**默认不启用**;需要同时给 ANTHROPIC_API_KEY
                         才会走,且会在日志里明写"这是按量计费源"。
                         启不启用是创始人的决定,不是脚本自己拍板。

铁律:
  · **红线不许进化**:合规约束(禁剂量/禁疗效承诺/宁可留空不许编造)在代码里做确定性校验,
    父代有的红线,子代必须一条不少 —— 少一条直接丢弃这次改进,不写库。
  · 改进者只改提示词,**碰不到**产线代码、D1 数据、已上线内容。
  · 新方案一律 status='trial',靠 A/B 自己去挣成绩;赢了才由 arbitrate() 提拔。
"""
import os, sys, json, time, uuid, argparse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q, ask, _unfence                       # noqa: E402
from _variant import arbitrate, MIN_N                      # noqa: E402

SCOPES = {
    "biocomp": ("SYS_BIO",  "biocomp_entries", "entry_id", "name_cn"),
    "herb":    ("SYS_HERB", "herb_compare",    "herb_id",  "name_cn"),
}

# 父代里出现过就必须保留的红线关键词。**不是穷举合规要求**,
#   是"这条约束存在过就不许在进化中消失"的锚点 —— 模型最爱在重写时把这些"精简"掉。
RED_MARKERS = ["剂量", "疗效", "留空", "编造", "出处", "医嘱", "临床", "禁"]

SYS_IMPROVER = (
    "你是一条内容产线的**提示词优化师**。给你:当前提示词、这一代的真实成绩、"
    "以及**被复核按住的真实原因分布和样例**。你的任务是改写提示词,让下一代的放行率更高。\n"
    "硬约束(违反即作废):\n"
    "  ① 当前提示词里所有**合规红线**(禁剂量/禁疗效承诺/禁医嘱口吻/体外不许写成临床/"
    "宁可留空不许编造)一条都不许删、不许弱化 —— 那些不是效果问题,是法律问题;\n"
    "  ② 只改提示词本身,不许输出解释、不许加前后缀、不许用```包裹;\n"
    "  ③ 针对**真实失败原因**下刀,不要泛泛地写「请更准确」这种废话;\n"
    "  ④ 长度与原提示词相当,不许大幅删减。\n"
    "直接输出改写后的完整提示词全文,一个字的多余话都不要。"
)


def diagnose(scope, sample_n=10):
    """**确定性诊断** —— 从真实被按住的条目里统计失败原因,不让模型自己猜。

    这是整个闭环里最容易偷懒的一步:让模型"反思一下哪里不好"最省事,
      但那是它在编;真正有信息量的是产线自己留下的 review_note。
    """
    _, tbl, idc, namec = SCOPES[scope]
    try:
        rows = d1(f"SELECT {namec} nm, review_note rn FROM {tbl} "
                  f"WHERE status='held' AND review_note IS NOT NULL "
                  f"ORDER BY updated_at DESC LIMIT 400")
    except Exception as e:
        print(f"  [诊断] 取数失败:{type(e).__name__} {str(e)[:80]}", flush=True)
        return "", []
    buckets, samples = {}, []
    for r in rows:
        note = (r.get("rn") or "").strip()
        if not note:
            continue
        key = note.split("|")[-1].strip()[:28] or "其他"
        buckets[key] = buckets.get(key, 0) + 1
        if len(samples) < sample_n:
            samples.append(f"「{r.get('nm')}」→ {note[:90]}")
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:8]
    total = sum(buckets.values()) or 1
    lines = [f"  · {k}  {v} 条({v/total:.0%})" for k, v in top]
    return "\n".join(lines), samples


def ask_claude_api(system, user, model=None, max_tokens=3000):
    """Claude API 改进者 —— 接口层。**按量计费源,默认不走。**

    创始人要的「未来接入 Claude 的 API 就可以云端调用」就是这个口子:
      产线代码一行不改,只把 --improver 切过来 + 给 secret,改进者就从免费池换成 Claude。
    钥匙握在创始人手里(要显式给 ANTHROPIC_API_KEY),脚本自己**不会**偷偷启用。
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未提供 ANTHROPIC_API_KEY —— Claude 改进者未启用(这是按量计费源,需创始人显式开启)")
    body = json.dumps({
        "model": model or os.environ.get("ANTHROPIC_MODEL", "claude-opus-5"),
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", method="POST", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    j = json.loads(urllib.request.urlopen(req, timeout=180).read())
    return "".join(b.get("text", "") for b in (j.get("content") or [])), (j.get("model") or "claude")


def guard(parent_body, child_body):
    """**红线闸** —— 父代有的合规约束,子代必须一条不少。确定性判据,不问模型。

    模型重写提示词时最常见的破坏就是"顺手精简":把「禁开剂量」这类它认为啰嗦的约束删掉。
    效果差一点可以靠 A/B 淘汰,**红线掉了是要出事的**,所以这道闸只能是硬编码。
    """
    if not child_body or len(child_body) < 80:
        return "产出为空或过短"
    if len(child_body) < len(parent_body) * 0.6:
        return f"比父代短太多({len(child_body)} vs {len(parent_body)}),疑似被截断/精简掉了约束"
    if len(child_body) > len(parent_body) * 2.5:
        return "比父代长太多,疑似把解释性文字也吐进来了"
    lost = [m for m in RED_MARKERS if m in parent_body and m not in child_body]
    if lost:
        return "丢失红线关键词:" + "/".join(lost)
    if child_body.strip() == parent_body.strip():
        return "与父代完全相同,没有改进"
    return None


def sync_candidates(limit=40):
    """info4AI 那一环:把雷达情报灌进 `evolve_candidates`。

    实测(2026-08-04):`evolve_candidates` **0 行** —— 雷达一直在跑、
      `intel_items` 有 100 条、`model_candidates` 有 63 条,但**没有一条流进进化侧**。
      三层架构里 info4AI 这一层是断开的,系统看得见外面的世界,却用不上。
    """
    n_in = 0
    try:
        # 列名以 pragma_table_info 实测为准:是 relevance_score / importance,
        #   **没有 score**(我第一版按习惯写的 `score` 直接 400 —— 同一个毛病今天第二次)。
        rows = d1("SELECT title, url, relevance_score, importance FROM intel_items "
                  "WHERE title IS NOT NULL "
                  "ORDER BY COALESCE(relevance_score,0) DESC, COALESCE(importance,0) DESC "
                  "LIMIT %d" % int(limit))
    except Exception as e:
        print(f"  [候选] intel_items 取数失败:{str(e)[:80]}", flush=True)
        rows = []
    now = int(time.time())
    vals = []
    for r in rows:
        t = (r.get("title") or "").strip()[:180]
        if not t:
            continue
        cid = "c_" + uuid.uuid4().hex[:12]
        sc = r.get("relevance_score") or r.get("importance") or 0
        vals.append(f"({q(cid)},'intel',{q(t)},{q((r.get('url') or '')[:300])},"
                    f"{q(str(now))},{q(sc)},'new',{now},{now})")
    if vals:
        try:
            d1("INSERT OR IGNORE INTO evolve_candidates "
               "(cand_id,kind,title,url,radar_run,score,status,created_at,updated_at) VALUES "
               + ",".join(vals))
            n_in = len(vals)
        except Exception as e:
            print(f"  [候选] 写入失败:{str(e)[:110]}", flush=True)
    tot = d1("SELECT COUNT(*) c FROM evolve_candidates")[0]["c"]
    print(f"  [候选] 本轮灌入 {n_in} 条 → 库内累计 {tot} 条", flush=True)
    return n_in


def improve_one(scope, improver="freepool", dry=False):
    slot, tbl, _, _ = SCOPES[scope]
    cur = d1(f"SELECT variant_id,body,fitness,sample_n FROM evolve_variants "
             f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='active' LIMIT 1")
    if not cur:
        print(f"  [{scope}] 无在位方案,跳过(先让产线跑一轮 seed_variant)", flush=True)
        return None
    par = cur[0]
    fit, n = par.get("fitness"), par.get("sample_n") or 0
    if n < MIN_N:
        print(f"  [{scope}] 在位方案样本 {n} < {MIN_N},还没资格谈改进(先攒样本)", flush=True)
        return None

    # 已有挑战者在场就别再生 —— 一次只养一个,否则流量摊薄谁也攒不够样本
    live = d1(f"SELECT variant_id,sample_n FROM evolve_variants "
              f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='trial'")
    if live:
        print(f"  [{scope}] 已有挑战者 {live[0]['variant_id']}(样本 {live[0].get('sample_n') or 0}),"
              f"等它跑够再生下一代", flush=True)
        return None

    reasons, samples = diagnose(scope)
    if not reasons:
        print(f"  [{scope}] 没有可用的失败反馈,不瞎改", flush=True)
        return None

    user = (f"【当前提示词】\n{par['body']}\n\n"
            f"【这一代的真实成绩】复核放行率 {fit:.1%}(判过 {n} 条)—— 也就是说约 "
            f"{(1-(fit or 0)):.0%} 的产出被按住了。\n\n"
            f"【被按住的真实原因分布】\n{reasons}\n\n"
            f"【真实失败样例】\n" + "\n".join("  " + s for s in samples) +
            "\n\n请针对上面这些**真实**的失败原因改写提示词。")

    def _gen(extra=""):
        if improver == "claude-api":
            print("  [改进者] Claude API(**按量计费源** —— 由创始人显式开启)", flush=True)
            t, m = ask_claude_api(SYS_IMPROVER, user + extra)
        else:
            t, m = ask(SYS_IMPROVER, user + extra, max_tokens=3000)
        return _unfence(t or "").strip(), m

    child, model = _gen()
    bad = guard(par["body"], child)
    if bad:
        # 【2026-08-04 首轮实测】biocomp 这一路被拦在「比父代长太多」——
        #   模型把改写理由也一并吐了出来。一次都不重试的话,**这条产线永远进化不了**,
        #   红线闸从"防出事"变成"永久堵死",那不是我们要的。
        #   给一次带反馈的重试(把它自己的毛病告诉它),仍不合格才丢弃。
        print(f"  [{scope}] ⚠ 首轮被闸拦下({bad}),带反馈重试一次", flush=True)
        child, model = _gen(f"\n\n【上一次你的产出被自动闸拦下了】原因:{bad}。"
                            f"请**只输出提示词全文本身**,不要任何说明、理由、前后缀,"
                            f"长度与原提示词相当。")
        bad = guard(par["body"], child)
    if bad:
        print(f"  [{scope}] ✗ 红线闸拦下这次改进(重试后仍不合格):{bad}", flush=True)
        return None
    if dry:
        print(f"  [{scope}] (dry-run)将写入子代,长度 {len(child)},改进者 {model}", flush=True)
        return None

    vid = "v_" + uuid.uuid4().hex[:12]
    now = int(time.time())
    d1(f"INSERT INTO evolve_variants (variant_id,scope,slot,body,status,mode,author,parent_id,"
       f"note,created_at,updated_at) VALUES ({q(vid)},{q(scope)},{q(slot)},{q(child)},'trial',"
       f"'improve',{q('claude-api' if improver=='claude-api' else 'freepool:'+str(model)[:40])},"
       f"{q(par['variant_id'])},{q('针对 %s 条失败反馈生成;父代 %.1f%%' % (n, (fit or 0)*100))},{now},{now})")
    print(f"  [{scope}] ✓ 生出挑战者 {vid}(父代 {par['variant_id']} {fit:.1%})—— 下一轮产线开始给它分流量", flush=True)
    return vid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="all", help="biocomp / herb / all")
    ap.add_argument("--improver", default=os.environ.get("EVOLVE_IMPROVER", "freepool"),
                    choices=["freepool", "claude-api"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    scopes = list(SCOPES) if a.scope == "all" else [a.scope]
    print(f"=== Improve 算子 · 改进者={a.improver} · 产线={','.join(scopes)} ===", flush=True)

    out = {"improver": a.improver, "born": [], "arbitrate": [], "candidates": 0}
    out["candidates"] = sync_candidates()

    for s in scopes:
        print(f"\n--- {s} ---", flush=True)
        # 先裁决(把上一轮跑够样本的挑战者判掉),再谈生新的 —— 顺序不能反,
        #   否则刚提拔的冠军会立刻被当成"该改进的老方案"又生一个孩子。
        for act, vid, why in arbitrate(s, SCOPES[s][0]):
            out["arbitrate"].append({"scope": s, "action": act, "variant": vid, "why": why})
        vid = improve_one(s, improver=a.improver, dry=a.dry_run)
        if vid:
            out["born"].append({"scope": s, "variant": vid})

    print("\n=== 本轮闭环动作 ===", flush=True)
    print(f"  情报灌入候选池:{out['candidates']} 条", flush=True)
    print(f"  裁决动作:{len(out['arbitrate'])} 次 {out['arbitrate']}", flush=True)
    print(f"  新生挑战者:{len(out['born'])} 个 {out['born']}", flush=True)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
