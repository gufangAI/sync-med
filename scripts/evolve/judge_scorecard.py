#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
judge_scorecard —— 量在位冠军判定力的「判对率 + 一致性」体检(整合队列#18)

立此因(2026-08-09):星网首次点火那轮,open-webui 被冠军弱关联判成 adopt(把「用户
  友好」硬接到「前台阅读器」),靠星网多认知共判才纠回 watch。这暴露 adopt 判定力弱
  (radar_race 总分 0.5833 里 adopt 类才 0.3333),但 radar_race 只给一个总分,看不出
  「冠军到底哪几题判错、判得稳不稳」。

学 deepeval「judge 建考场量准确率」的思维(不搬它 141M 本体,红线+零常驻),用我们
  已有的确定性判分(radar_set.score_one)+ 冻结考卷(18题有标准答案)自写:
  · 对**在位冠军**(brain() 读 D1,不是源码默认)judge 每题 REPEAT 次;
  · 判对率 = 众数 verdict == want 的题数占比(与 radar_race 同源判据);
  · **一致性** = 每题 REPEAT 次里众数占比的均值 —— radar_race 没有的维度。
    temperature=0 下仍不一致 = 这题判定不可靠(open-webui 那种弱关联的信号)。
  · 一致性低的题单独列出 → 反馈给 radar_race 改写者定向进化。
判定应答者固定 zhipu(与 adopt.judge 同源:判定任务实测合格,避免 agnes 占位符污染)。
零改生产:只读冠军、只调网关判定、只写报告 json;失败题不污染(记 ERR)。
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))

from radar_set import EXAM, score_one                      # noqa: E402
from radar_race import _ask_any, _parse_json               # noqa: E402
import adopt                                                # noqa: E402
from adopt import brain, JUDGE_SUPPLIER                     # noqa: E402
from _stack import scan_stack                               # noqa: E402


def judge_once(champ, q):
    """用冠军大脑判一题,返回 verdict(失败返回 'ERR')。
    注入家底清单,与 adopt.judge 生产判定同条件(否则家底盲区会被误算成判定力问题)。"""
    stack_ctx = (("\n\n【我们已在用的家底 —— 候选若命中其一即判 skip,这是家底不是新机会】\n"
                  + "\n".join(adopt.STACK_LINES)) if adopt.STACK_LINES else "")
    user = (f"项目:{q['repo']}\n星数:{q['stars']}\n语言:{q['lang']}\n"
            f"topics:\n简介:{q['desc']}") + stack_ctx
    try:
        txt, _ = _ask_any(champ, user, JUDGE_SUPPLIER,
                          max_tokens=500, json_mode=True, temperature=0)
        o = _parse_json(txt)
        v = str(o.get("verdict") or "").strip().lower()
        return v if v in ("adopt", "watch", "skip") else "skip"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3, help="每题判几次量一致性")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    # 体检必须和生产判定同条件:生产 judge 会注入家底清单,体检也得填,否则测的不是
    #   真实生产判定(家底盲区会被误算成判定力问题)。judge() 读的是 adopt 的 global。
    adopt.STACK_NAMES, adopt.STACK_LINES = scan_stack()
    champ = brain()
    print(f"judge 体检 · 卷子 {len(EXAM)} 题 · 每题 ×{a.repeat} · 应答者 {JUDGE_SUPPLIER}"
          f" · 家底 {len(adopt.STACK_NAMES)} 项已注入", flush=True)

    rows, unstable, wrong = [], [], []
    for q in EXAM:
        vs = [judge_once(champ, q) for _ in range(a.repeat)]
        c = Counter(vs)
        mode, cnt = c.most_common(1)[0]
        consistency = round(cnt / a.repeat, 3)
        # 判对率用众数 verdict 走同一确定性判分(只看 verdict 对不对,不要求 current/beats,
        # 因为这里量的是「判定方向稳不稳、对不对」,不是完整答题分)
        correct = (mode == q["want"])
        rows.append(dict(repo=q["repo"], want=q["want"], votes=vs,
                         mode=mode, consistency=consistency, correct=correct))
        flag = "✅" if correct else "❌"
        stab = "" if consistency == 1.0 else f"  ⚠️不稳({'/'.join(vs)})"
        print(f"  {flag} {q['repo']:32s} 应{q['want']:5s} 判{mode:5s} "
              f"一致{consistency:.2f}{stab}", flush=True)
        if consistency < 1.0:
            unstable.append(q["repo"])
        if not correct:
            wrong.append(f"{q['repo']}(应{q['want']}判{mode})")

    acc = round(sum(r["correct"] for r in rows) / len(rows), 4)
    avg_con = round(sum(r["consistency"] for r in rows) / len(rows), 4)
    by_cls = {}
    for r in rows:
        by_cls.setdefault(r["want"], [0, 0])
        by_cls[r["want"]][1] += 1
        if r["correct"]:
            by_cls[r["want"]][0] += 1

    print(f"\n=== 冠军判定力体检 ===", flush=True)
    print(f"  判对率 {acc:.4f} · 平均一致性 {avg_con:.4f}", flush=True)
    for cls, (ok, tot) in sorted(by_cls.items()):
        print(f"  {cls} 类: {ok}/{tot} 判对", flush=True)
    if wrong:
        print(f"  判错题({len(wrong)}): {' · '.join(wrong)}", flush=True)
    if unstable:
        print(f"  ⚠️判定不稳题({len(unstable)},temp=0 仍多结果): {' · '.join(unstable)}",
              flush=True)

    out = dict(exam_n=len(EXAM), repeat=a.repeat, supplier=JUDGE_SUPPLIER,
               accuracy=acc, avg_consistency=avg_con,
               by_class={k: {"correct": v[0], "total": v[1]} for k, v in by_cls.items()},
               wrong=wrong, unstable=unstable, rows=rows)
    if a.report:
        json.dump(out, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n体检表 → {a.report}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
