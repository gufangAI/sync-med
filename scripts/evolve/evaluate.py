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
    """跑一道题:生成 → 复核。**全在内存,不落任何生产表。**"""
    try:
        if scope == "herb":
            obj, model = HF.gen_herb(item[0], item[1], sys_prompt=body)
        else:
            obj, model = HF.gen_bio(item[0], item[1], sys_prompt=body)
    except Exception as e:
        return ("fail", f"生成失败 {type(e).__name__}", None)
    if HF.violates(obj):
        return ("hold", "命中合规词表", model)
    try:
        row = dict(obj)
        row["id"] = "eval"
        verdict, why, _ = RV.review_one(row, model, target=scope)
        return (verdict, why, model)
    except Exception as e:
        return ("fail", f"复核失败 {type(e).__name__}", model)


def evaluate(variant, scope, eval_id, items):
    body = variant["body"]
    res = {"pass": 0, "hold": 0, "fail": 0, "why": []}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(run_one, scope, it, body): it for it in items}
        for f in as_completed(futs):
            v, why, _ = f.result()
            if v == "pass":
                res["pass"] += 1
            elif v == "hold":
                res["hold"] += 1
                res["why"].append(why[:60])
            else:
                res["fail"] += 1
                res["why"].append(why[:60])
    judged = res["pass"] + res["hold"]
    score = (res["pass"] / judged) if judged else None
    tid = "t_" + uuid.uuid4().hex[:12]
    now = int(time.time())
    d1(f"INSERT INTO evolve_trials (trial_id,variant_id,scope,raw_score,fitness,sample_n,"
       f"passed,held,failed,feedback,run_ref,created_at,evaluation_dataset,mode) VALUES ("
       f"{q(tid)},{q(variant['variant_id'])},{q(scope)},{q(score)},{q(score)},{judged},"
       f"{res['pass']},{res['hold']},{res['fail']},{q(' | '.join(res['why'][:8]))},"
       f"{q(os.environ.get('GITHUB_RUN_ID',''))},{now},{q(eval_id)},'offline_eval')")
    # variants.fitness 只由回归集分数写 —— 生产流量不再参与裁决
    if score is not None:
        d1(f"UPDATE evolve_variants SET fitness={q(round(score,4))}, sample_n={judged}, "
           f"evaluation_dataset={q(eval_id)}, updated_at={now} "
           f"WHERE variant_id={q(variant['variant_id'])}")
    return score, res


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
            s = "—" if score is None else f"{score:.1%}"
            print(f"  [{v['status']:6s}] {v['variant_id']} 得分 {s} "
                  f"(过{res['pass']} 按{res['hold']} 废{res['fail']})", flush=True)
            if res["why"]:
                print(f"          扣分:{res['why'][0]}", flush=True)
            out[scope]["variants"].append({
                "variant_id": v["variant_id"], "status": v["status"],
                "score": score, **{k: res[k] for k in ("pass", "hold", "fail")},
            })

    if a.report:
        json.dump(out, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n=== 评测完成。裁决交给 improve.py 的 arbitrate(它只认同一张卷子的分数)===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
