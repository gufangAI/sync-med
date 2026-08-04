#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Evaluate 算子 —— **固定回归集**离线评测,取代拿生产流量赌

立此因(2026-08-04 两位大佬评审,都判我不合格):
  GPT:「**绝不能让线上引擎边运行边自由修改自己**。运行面只使用当前已批准的 Champion;
        候选走离线评测 / 影子流量 / A-B,合格才晋升,且要留 promotion_record 与 rollback_version。」
  Claude:「方案槽的进化裁决基准是**回归集**……**没有固定基准的 A/B 是掷骰子**。」

我原来干的正是掷骰子:挑战者直接吃 30% 生产流量,而生产每轮的**题材是随机的**。
  实测翻车:挑战者一度显示 5.9%「领先」冠军 3.5 个点,样本从 17 涨到 57 后变成 1.8%「落后」。
  那不是方案变差了,是**两边根本没跑同一批题目**。拿不同卷子的分数比高下,必然掷骰子。

这里改成:**同一批固定题材、同一个复核器、只换提示词**。
  两边跑同一张卷子,差值才是方案本身的差值。

铁律:
  · **一条生产数据都不写**。生成与复核全在内存里跑完,只把分数写进 evolve_trials。
    (评测污染生产内容是最容易犯又最难查的错 —— 从设计上堵死,不靠小心。)
  · 只走内部免费池网关,零按量计费。
  · 分数必须绑 eval_id:换了卷子的分数**不许**跨卷比较。
"""
import os, sys, json, time, uuid, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
CF = os.path.join(HERE, "..", "content_factory")
sys.path.insert(0, CF)
from _ai import d1, q                                          # noqa: E402
import herb_factory as HF                                      # noqa: E402
import reviewer as RV                                          # noqa: E402

WORKERS = int(os.environ.get("EVAL_WORKERS", "4"))


def ensure_tables():
    d1("""CREATE TABLE IF NOT EXISTS evolve_evalsets (
            eval_id TEXT PRIMARY KEY, scope TEXT, slot TEXT,
            items_json TEXT, n_items INTEGER, note TEXT, created_at INTEGER)""")
    # 控制面字段 —— GPT 的规格。ALTER 失败 = 列已存在,忽略。
    for col, typ in (("evaluation_dataset", "TEXT"), ("promotion_record", "TEXT"),
                     ("rollback_version", "TEXT"), ("evo_scope", "TEXT")):
        try:
            d1(f"ALTER TABLE evolve_variants ADD COLUMN {col} {typ}")
            print(f"  [表] evolve_variants 加列 {col}", flush=True)
        except Exception:
            pass
    for col, typ in (("evaluation_dataset", "TEXT"), ("mode", "TEXT")):
        try:
            d1(f"ALTER TABLE evolve_trials ADD COLUMN {col} {typ}")
            print(f"  [表] evolve_trials 加列 {col}", flush=True)
        except Exception:
            pass


def seed_evalset(scope, n=12):
    """**冻结一张卷子**。已有就复用 —— 卷子一旦变了,历史分数就不可比,这是回归集的命。"""
    ensure_tables()
    cur = d1(f"SELECT eval_id, items_json, n_items FROM evolve_evalsets "
             f"WHERE scope={q(scope)} ORDER BY created_at DESC LIMIT 1")
    if cur:
        return cur[0]["eval_id"], json.loads(cur[0]["items_json"])

    items = (HF.HERB_FALLBACK if scope == "herb" else HF.BIO_FALLBACK)[:n]
    items = [list(x) for x in items]
    eid = "ev_" + uuid.uuid4().hex[:10]
    d1(f"INSERT INTO evolve_evalsets (eval_id,scope,slot,items_json,n_items,note,created_at) VALUES ("
       f"{q(eid)},{q(scope)},{q('SYS_HERB' if scope=='herb' else 'SYS_BIO')},"
       f"{q(json.dumps(items, ensure_ascii=False))},{len(items)},"
       f"{q('首版回归集:固定题材,换卷即不可比')},{int(time.time())})")
    print(f"  [回归集] 新建 {eid},{len(items)} 道题(已冻结)", flush=True)
    return eid, items


def run_one(scope, item, body):
    """跑一道题:生成 → **确定性判分**。全在内存,不落任何生产表。

    【2026-08-04 换判据】原来这里调 reviewer.review_one() —— 那是**模型判**,
      于是分数 = f(考生模型, 考生随机, **考官模型**, **考官随机**, 题目),
      而我只想量提示词。实测同一方案两轮 25.0% → 8.3%,摆幅 16.7 点。
    现在换成 gate.score():**零调用、零随机、同一份输出永远得同一个分**。
      而且它给**连续分**不是过/不过 —— 治的是第二个病(四个候选并列 0.0%,
      就算噪声全消也永远排不出序)。
    """
    try:
        if scope == "herb":
            obj, model = HF.gen_herb(item[0], item[1], sys_prompt=body)
        else:
            obj, model = HF.gen_bio(item[0], item[1], sys_prompt=body)
    except Exception as e:
        return {"qualified": False, "score": 0.0, "reasons": [f"生成失败 {type(e).__name__}"],
                "model": None, "gen_fail": True}
    if HF.violates(obj):
        return {"qualified": False, "score": 0.0, "reasons": ["命中合规词表"], "model": model}
    r = gate_score(scope, obj)
    r["model"] = model
    return r


def evaluate(variant, scope, eval_id, items):
    """跑完整张卷子。**分数 = 过闸条目的连续分均值**,不是放行率。"""
    body = variant["body"]
    scores, quals, fails, reasons = [], 0, 0, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(run_one, scope, it, body) for it in items]
        for f in as_completed(futs):
            r = f.result()
            if r.get("gen_fail"):
                fails += 1
            if r.get("qualified"):
                quals += 1
                scores.append(r["score"])
            else:
                scores.append(0.0)          # 失格计 0 分,但**仍进均值** —— 失格率也是水平的一部分
                reasons.extend(r.get("reasons") or [])
    n = len(items)
    mean = (sum(scores) / n) if n else None
    tid = "t_" + uuid.uuid4().hex[:12]
    now = int(time.time())
    # raw_score = 过闸率(旧口径,留痕可对比);fitness = 连续分均值(新主判据)
    qual_rate = (quals / n) if n else None
    d1(f"INSERT INTO evolve_trials (trial_id,variant_id,scope,raw_score,fitness,sample_n,"
       f"passed,held,failed,feedback,run_ref,created_at,evaluation_dataset,mode) VALUES ("
       f"{q(tid)},{q(variant['variant_id'])},{q(scope)},{q(qual_rate)},{q(mean)},{n},"
       f"{quals},{n - quals - fails},{fails},"
       f"{q(' | '.join(dict.fromkeys(reasons))[:1400])},"
       f"{q(os.environ.get('GITHUB_RUN_ID',''))},{now},{q(eval_id + '#' + SCORER_VERSION)},"
       f"'offline_deterministic')")
    if mean is not None:
        d1(f"UPDATE evolve_variants SET fitness={q(round(mean,4))}, sample_n={n}, "
           f"evaluation_dataset={q(eval_id + '#' + SCORER_VERSION)}, updated_at={now} "
           f"WHERE variant_id={q(variant['variant_id'])}")
    return mean, {"qual": quals, "n": n, "fail": fails,
                  "why": list(dict.fromkeys(reasons))[:6]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="all", help="herb / biocomp / all")
    ap.add_argument("--items", type=int, default=12)
    ap.add_argument("--report", default="")
    a = ap.parse_args()
    scopes = ["herb", "biocomp"] if a.scope == "all" else [a.scope]

    out = {}
    for scope in scopes:
        slot = "SYS_HERB" if scope == "herb" else "SYS_BIO"
        eval_id, items = seed_evalset(scope, a.items)
        rows = d1(f"SELECT variant_id,body,status FROM evolve_variants "
                  f"WHERE scope={q(scope)} AND slot={q(slot)} AND status IN ('active','trial')")
        if not rows:
            print(f"  [{scope}] 无可评方案", flush=True)
            continue
        print(f"\n=== {scope} · 回归集 {eval_id}({len(items)} 题)· 待评 {len(rows)} 个方案 ===", flush=True)
        out[scope] = {"eval_id": eval_id, "n_items": len(items), "variants": []}
        for v in rows:
            score, res = evaluate(v, scope, eval_id, items)
            s = "—" if score is None else f"{score:.4f}"
            print(f"  [{v['status']:6s}] {v['variant_id']} 连续分 {s} "
                  f"(过闸 {res['qual']}/{res['n']} 生成废 {res['fail']})", flush=True)
            for w in res["why"][:2]:
                print(f"          失格:{w}", flush=True)
            out[scope]["variants"].append({
                "variant_id": v["variant_id"], "status": v["status"],
                "score": score, "qualified": res["qual"], "n": res["n"], "fail": res["fail"],
            })

    if a.report:
        json.dump(out, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n=== 评测完成。裁决交给 improve.py 的 arbitrate(它只认同一张卷子的分数)===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
