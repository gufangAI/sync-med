#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改进者赛马 —— 验证闭环的**命门**:免费模型到底能不能写出更好的提示词

立此因(2026-08-05 创始人):「说了1个多月的闭环,你都没完成就去做没用的细节」
  + 2026-08-04 的单任务恢复令:「使用系统当前已经存在的免费模型和多模型 Router,
    对同一个失败提示词生成候选并进行公平评测,判断免费模型是否具备产生优于当前冠军方案的能力」

为什么这是命门:
  整套自进化系统的假设只有一句话 —— **改进者能写出更好的提示词**。
  这句话**从来没验证过**,而我在它上面建了:方案槽、A/B 分流、适应度结算、
  四道裁决闸、回归集、资格闸、连续分、控制面字段、暂停牌。
  假设若不成立,上面全是围着一个死掉的中心转的脚手架。

而且我此前的失败方式很蠢:`improve.py` 调 `ask()` **没指定 supplier**,
  一直走容错链落在最弱的那家(多半 glm-4-flash),
  池子里的 nemotron-550b / DeepSeek / qwen-plus **一个都没试过**。
  然后我据此下结论「免费池不行,要买 Claude API」—— 结论是错的,过程是懒的。

本脚本做且只做一件事(**做完就停,不自动换冠军**):
  同一份失败反馈 → 每家免费模型各写一版提示词 → **同一张卷子 + 同一个确定性判分**
  → 出一张真实结果表 → 结束。晋升与否由人拍板。

铁律:只走内部免费池网关;不改生产 improve.py;不碰进化闭环;零付费 API;不提交不部署。
"""
import os, sys, json, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
sys.path.insert(0, HERE)
from _ai import d1, q, ask, ask_opencode                     # noqa: E402
from gate import score as gate_score, SCORER_VERSION         # noqa: E402
import herb_factory as HF                                    # noqa: E402
from improve import SYS_IMPROVER, diagnose, strip_wrapper, guard, SCOPES  # noqa: E402

# 参赛的改进者 —— **点名**,不走容错链。这正是此前那个错误的反面:
#   不点名 = 每次都落在链头最弱的那家,而我却拿它的失败去否定整个免费池。
RACERS = [
    ("opencode",   "OpenCode 免密端点 · deepseek-v4-flash(创始人点名:这个还不错)"),
    ("nvidia",     "NVIDIA NIM · nemotron 系(池内参数量最大)"),
    ("dashscope",  "阿里云百炼 · qwen-plus"),
    ("zhipu",      "智谱 · glm-4-flash(此前默认落到的那家)"),
    ("openrouter", "OpenRouter 聚合 free 档"),
]


def _ask_any(system, user, supplier, max_tokens=3000):
    """点名调用。**opencode 走免密直连,不经网关** —— 它是池子里唯一另一条通道。"""
    if supplier == "opencode":
        # need="" 是因为我们要的是提示词全文,不是 JSON,不能拿 "[" 当起始符判据
        return ask_opencode(system, user, max_tokens=max_tokens, need="")
    return ask(system, user, max_tokens=max_tokens, supplier=supplier)
EVAL_WORKERS = int(os.environ.get("EVAL_WORKERS", "4"))


def champion(scope):
    slot = SCOPES[scope][0]
    r = d1(f"SELECT variant_id, body, fitness FROM evolve_variants "
           f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='active' LIMIT 1")
    return r[0] if r else None


def evalset(scope, n=12):
    r = d1(f"SELECT eval_id, items_json FROM evolve_evalsets WHERE scope={q(scope)} "
           f"ORDER BY created_at DESC LIMIT 1")
    if r:
        return r[0]["eval_id"], json.loads(r[0]["items_json"])[:n]
    items = (HF.HERB_FALLBACK if scope == "herb" else HF.BIO_FALLBACK)[:n]
    return "(临时)", [list(x) for x in items]


def make_candidate(scope, parent_body, supplier, reasons, samples):
    """让一家改进者写一版提示词。返回 (body, 模型名, 失败原因)。"""
    fit = 0.0
    user = (f"【当前提示词】\n{parent_body}\n\n"
            f"【被复核按住的真实原因分布】\n{reasons}\n\n"
            f"【真实失败样例】\n" + "\n".join("  " + s for s in samples) +
            "\n\n请针对上面这些**真实**的失败原因改写提示词。"
            f"长度控制在原提示词的 0.8~1.5 倍(原文 {len(parent_body)} 字)。")
    try:
        txt, model = _ask_any(SYS_IMPROVER, user, supplier)
    except Exception as e:
        return None, supplier, f"调用失败 {type(e).__name__}: {str(e)[:60]}"
    body = strip_wrapper(txt or "")
    bad = guard(parent_body, body)
    if bad:
        # 给一次带反馈的重试 —— 与生产 improve.py 同样待遇,不厚此薄彼
        try:
            txt, model = _ask_any(
                SYS_IMPROVER,
                user + f"\n\n【上次被自动闸拦下】原因:{bad}。只输出提示词全文,不要任何说明。",
                supplier)
            body = strip_wrapper(txt or "")
            bad = guard(parent_body, body)
        except Exception as e:
            return None, model, f"重试调用失败 {type(e).__name__}"
    if bad:
        return None, model, f"不合规:{bad}"
    return body, model, None


def run_exam(scope, body, items):
    """跑同一张卷子,用同一个确定性判分。返回 (均分, 过闸数, 逐题分)。"""
    def one(it):
        try:
            obj, _ = (HF.gen_herb(it[0], it[1], sys_prompt=body) if scope == "herb"
                      else HF.gen_bio(it[0], it[1], sys_prompt=body))
        except Exception:
            return 0.0, False
        if HF.violates(obj):
            return 0.0, False
        r = gate_score(scope, obj)
        return r["score"], bool(r["qualified"])
    scores, quals = [], 0
    with ThreadPoolExecutor(max_workers=EVAL_WORKERS) as ex:
        for s, okq in ex.map(one, items):
            scores.append(s)
            quals += 1 if okq else 0
    return (sum(scores) / len(scores) if scores else 0.0), quals, [round(x, 3) for x in scores]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="biocomp", help="biocomp / herb")
    ap.add_argument("--items", type=int, default=12)
    ap.add_argument("--out", default="race_result.json")
    ap.add_argument("--save-candidates", default="race_candidates")
    a = ap.parse_args()
    scope = a.scope

    ch = champion(scope)
    if not ch:
        print(f"[{scope}] 没有在位冠军,无法比"); return 1
    eid, items = evalset(scope, a.items)
    reasons, samples = diagnose(scope)
    os.makedirs(a.save_candidates, exist_ok=True)

    print(f"=== 改进者赛马 · {scope} ===")
    print(f"  卷子   {eid}({len(items)} 题) · 判分函数 {SCORER_VERSION}")
    print(f"  冠军   {ch['variant_id']}(库内记录 fitness={ch.get('fitness')})")
    print(f"  参赛   {', '.join(s for s, _ in RACERS)}\n")

    rows = []
    # ① 冠军先考一次 —— 基准必须在**同一轮、同样条件**下取,不能拿库里的旧数字比
    t0 = time.time()
    base, baseq, basedetail = run_exam(scope, ch["body"], items)
    rows.append({"who": "冠军(现役)", "supplier": "-", "model": "-", "ok": True,
                 "score": round(base, 4), "qualified": baseq, "n": len(items),
                 "delta": 0.0, "secs": round(time.time() - t0, 1),
                 "detail": basedetail, "note": ch["variant_id"]})
    print(f"  [基准] 冠军 连续分 {base:.4f} · 过闸 {baseq}/{len(items)} · {time.time()-t0:.0f}s\n")

    # ② 每家免费模型各写一版,考同一张卷
    for supplier, desc in RACERS:
        t = time.time()
        body, model, err = make_candidate(scope, ch["body"], supplier, reasons, samples)
        if err:
            print(f"  ✗ {supplier:12s} {err}")
            rows.append({"who": supplier, "supplier": supplier, "model": model, "ok": False,
                         "score": None, "qualified": None, "n": len(items),
                         "delta": None, "secs": round(time.time() - t, 1),
                         "detail": [], "note": err})
            continue
        open(os.path.join(a.save_candidates, f"{scope}_{supplier}.txt"), "w",
             encoding="utf-8").write(body)
        sc, qn, detail = run_exam(scope, body, items)
        d = sc - base
        print(f"  {'✓' if d > 0 else '·'} {supplier:12s} 连续分 {sc:.4f} "
              f"({d:+.4f}) · 过闸 {qn}/{len(items)} · {model} · {time.time()-t:.0f}s")
        rows.append({"who": supplier, "supplier": supplier, "model": model, "ok": True,
                     "score": round(sc, 4), "qualified": qn, "n": len(items),
                     "delta": round(d, 4), "secs": round(time.time() - t, 1),
                     "detail": detail, "note": f"候选存于 {a.save_candidates}/{scope}_{supplier}.txt"})

    win = [r for r in rows if r["ok"] and r["delta"] and r["delta"] > 0]
    print("\n=== 结论 ===")
    if win:
        b = max(win, key=lambda r: r["delta"])
        print(f"  ✅ **有免费模型胜过冠军**:{b['supplier']} {b['score']:.4f} "
              f"(冠军 {base:.4f},+{b['delta']:.4f})")
        print("  → 免费改进者可行。下一步:接赛马机制,由人拍板是否晋升。")
    else:
        okc = [r for r in rows if r["ok"] and r["who"] != "冠军(现役)"]
        print(f"  ❌ **没有一家胜过冠军**({len(okc)}/{len(RACERS)} 家产出了合规候选)")
        print("  → 但注意:能产出合规候选 ≠ 不可行。若合规率高而分数不高,")
        print("     问题在**改进提示词与评测方法**,不在模型;若连合规候选都产不出,才谈更强模型。")
    json.dump({"scope": scope, "eval_id": eid, "scorer": SCORER_VERSION,
               "baseline": round(base, 4), "rows": rows},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n  结果表 {a.out} · 候选原文 {a.save_candidates}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
