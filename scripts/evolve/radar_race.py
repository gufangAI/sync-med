#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雷达赛马 —— 让免费池各家改写**判定大脑**,拿 15 题真题卷子分胜负

立此因(2026-08-06 创始人):
  「我们一定要深度优化**雷达和自我进化的系统技术**」「你为什么又偏了啊」

此前的赛马(`race.py`)进化的是**内容产线提示词**(biocomp/herb)——
  而「内容工厂」这个模块 08-05 已被创始人从雷达清单里删掉。
  进化算子唯一认识的模块已不存在,所以它每轮只能往药方线上跑。
本文件把进化对象换成**雷达自己的判定大脑** `adopt.py::SYS_ADOPT`:
  它是全系统唯一一段「既是提示词(改进算子够得着)、又有标准答案(adoption.txt 的真判决)、
  还有事故留痕(创始人当场骂过的两次误判)」的东西。

赛制(和内容赛马同一套纪律,一步不放松):
  · 同一张卷子(`radar_set.EXAM` 15 题,全部真发生过)
  · 同一个判分函数(`radar_set.score_all`,零调用零随机,按类别宏平均)
  · 同一份改进反馈(现任判定大脑在哪些题上错了,原样喂给每一家)
  · **冻结考卷**:同一段提示词 + 同一道题 → 复用缓存,保证多轮可复现
  · 只出结果表,**不自动换冠军**。晋升由人拍板。

铁律:全程免费池(网关 + OpenCode 免密端点),零按量计费源。
"""
import os, sys, json, time, hashlib, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
sys.path.insert(0, HERE)

from _ai import ask, ask_opencode                              # noqa: E402
from radar_set import EXAM, score_all, score_one, baselines, SCORER_VERSION  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CACHE_DIR = os.path.join(REPO_ROOT, "reports", "evolve", "radar_cache")

# 白名单里的 platform → 网关 supplier(网关 provider 表里真实存在的接入点)。
# 白名单只记 `模型@平台`,网关按 supplier 索引 base+key,两边靠这张表对上。
_PLATFORM_SUPPLIER = {
    "tencent":    "tc_dsflash",   # tokenhub.tencentmaas.com,配 model 覆盖即可换模型
    "nvidia":     "nvidia",       # integrate.api.nvidia.com
    "openrouter": "openrouter",
}

# 兜底赛手:白名单读不到时用,**全部免费家**。
# 刻意不含 dashscope —— 它是付费源,memory 铁律定性「只兜底」,
# 不该当固定考生/赛手(2026-08-07 严格模式暴露:它一度是唯一跑得通的挑战者)。
_FALLBACK_RACERS = [
    ("opencode",   None, "OpenCode 免密端点 · deepseek-v4-flash"),
    ("nvidia",     None, "NVIDIA NIM · 默认模型"),
    ("zhipu",      None, "智谱 · glm-4-flash"),
    ("openrouter", None, "OpenRouter 聚合 free 档"),
]


def load_racers():
    """赛手从**实测白名单**取,不再硬编码。

    【2026-08-07 接上零引用的白名单】`scripts/model_pool_whitelist.json`
    2026-07-25 建成:192 个注册模型 → 实测 90 个能应答 → 37 个过中医判据,
    还预先分好了 race_diversity / batch_quality / bulk_fast 等组。
    **然后全仓零引用,躺了 13 天。** 而赛马这边硬编码 5 家、其中 3 家今天实测 503。
    "192 个只驾驭 3 个"不是没资源,是**探活结果没有入口喂进来**。
    race_diversity 组 7 个模型跨 3 家平台(腾讯/NVIDIA/OpenRouter),
    模型家族也真分散(youtu / kimi / mistral / deepseek / llama / ling)——
    这才是赛马要的真多样性:同厂同款戴几顶帽子仍是同一个脑子。
    """
    path = os.path.join(REPO_ROOT, "scripts", "model_pool_whitelist.json")
    try:
        with open(path, encoding="utf-8") as f:
            grp = (json.load(f).get("recommend") or {}).get("race_diversity") or []
    except Exception as e:
        print(f"  [赛手] 白名单读取失败({type(e).__name__}),用兜底名单", flush=True)
        return _FALLBACK_RACERS
    out = []
    for item in grp:
        if "@" not in str(item):
            continue
        mdl, plat = str(item).rsplit("@", 1)
        sup = _PLATFORM_SUPPLIER.get(plat)
        if not sup:                       # 平台没有对应网关接入点 = 跳过,不硬凑
            print(f"  [赛手] 跳过 {item}:平台 {plat} 无网关接入点", flush=True)
            continue
        out.append((sup, mdl, f"{plat} · {mdl}"))
    if not out:
        print("  [赛手] 白名单无可用项,用兜底名单", flush=True)
        return _FALLBACK_RACERS
    print(f"  [赛手] 白名单 race_diversity 取到 {len(out)} 家 "
          f"(平台:{sorted({r[2].split(' · ')[0] for r in out})})", flush=True)
    return out


RACERS = load_racers()

SYS_REWRITE = (
    "你是**技术选型评审员的提示词优化师**。给你:现任评审提示词、它在一套真题上的失分明细。\n"
    "任务:改写这段提示词,让它在同一套真题上判得更准。\n\n"
    "【这套系统在判什么】\n"
    "  一个开源项目值不值得接进我们平台。我们的运行形态是 Cloudflare Serverless"
    "(Workers/Pages/D1/R2/Vectorize)+ GitHub Actions —— **零常驻主机、零月费**。\n"
    "【它反复犯的两类错(真事故)】\n"
    "  · **明星光环**:redis 75876★ 被判成能改进「RAG 向量检索」、caddy 74627★ 被判成"
    "能改进「前台阅读器」。它们要开机器常驻,我们没有机器。\n"
    "  · **常识复读**:把 PaddleOCR / tesseract 判成能改进 OCR 产线 —— 而 "
    "`pip install paddleocr` 就写在我们自己的 workflow 里。推荐我们已经在用的东西不是判断,"
    "是「这个领域最有名的工具是什么」的同义词联想。\n\n"
    "【判分规则 · 确定性代码打分,完全公开,照着改】\n"
    "  · verdict 判对得 0.60(watch 当半对 0.25);\n"
    "  · 判对之后:adopt 必须答出 `current`(我们现在用什么)与 `beats`(在哪个可量化指标上更强),"
    "skip 必须答出 `why`,才得 0.25。**判错则说明分不计** —— 答错了不因为讲得好而得分;\n"
    "  · 陷阱题(常驻主机 / 已在家底)判成 adopt 即失掉 0.15;\n"
    "  · 总分按 **want 类别宏平均**(先算 skip 类均分与 adopt 类均分,再取平均)——"
    "所以「一律判 skip」这种退化策略只能拿 0.5,想赢必须两类都判准。\n\n"
    "【输出格式硬要求】\n"
    "  · 只输出改写后的提示词全文。不要开场白、不要结尾的「改进说明」、不要用 ``` 包裹。\n"
    "  · 必须保留原提示词结尾的 JSON 输出格式约定"
    "(module/verdict/metric/how/effort/current/beats/why 八个字段),少一个字段整条作废。\n"
    "  · 长度控制在原文的 0.7~1.6 倍。"
)


def _ask_any(system, user, supplier, max_tokens=3000, json_mode=False, model=None):
    """本文件的每一次调用都是评测/赛马,所以**一律走严格模式**(no_fallback)。

    【2026-08-07 修·实锤在 Issue #205】点名 nvidia / zhipu / openrouter 三家,
    结果"实际模型"列全是 `Qwen/Qwen3-8B` —— 网关的容错链把点名吃掉了,
    三家赛马实际是同一个模型跟自己吵了三次,赛马从根上是假的。
    容错链在生产是优点(用户不该看见报错),在评测是污染源:
      · 考卷这一路换供应商 = 换了考生,分数不可比;
      · 改写这一路换供应商 = 多样性归零,赛马失去意义。
    严格模式下失败就是失败,记 fail 让它显形(两处调用方都已有 except 记录),
    **绝不静默换马** —— 看不见的失败比失败本身更贵。
    opencode 是免密端点直连,不经网关,天然无容错链,不受此参数影响。
    """
    if supplier == "opencode":
        return ask_opencode(system, user, max_tokens=max_tokens, need="")
    return ask(system, user, max_tokens=max_tokens, supplier=supplier,
               json_mode=json_mode, no_fallback=True, model=model)


# ── 冻结考卷 ────────────────────────────────────────────────────
#   同一段提示词 + 同一道题 = 同一个答案,直接复用。
#   没有这个,「连跑 3 次分数一致」就永远验不出来 —— temperature=0 也不是字节确定的。
def _ck(body, repo):
    h = hashlib.sha1((body + "||" + repo).encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, h + ".json")


def _cget(body, repo):
    p = _ck(body, repo)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def _cput(body, repo, obj):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        json.dump(obj, open(_ck(body, repo), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


def _parse_json(t):
    t = (t or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        t = t.lstrip("json").strip()
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    return json.loads(t)


def exam_one(body, supplier, judge_system):
    """完整跑一遍卷子。judge_system = 被考的那段判定提示词。"""
    answers, cached = [], 0
    for q_ in EXAM:
        user = (f"项目:{q_['repo']}\n星数:{q_['stars']}\n语言:{q_['lang']}\n"
                f"topics:\n简介:{q_['desc']}")
        got = _cget(judge_system, q_["repo"])
        if got is not None:
            cached += 1
        else:
            try:
                txt, _ = _ask_any(judge_system, user, supplier,
                                  max_tokens=500, json_mode=True)
                got = _parse_json(txt)
            except Exception as e:
                got = {"verdict": "", "_err": f"{type(e).__name__}:{str(e)[:60]}"}
            _cput(judge_system, q_["repo"], got)
        answers.append(got)
    total, per_cls = score_all(answers)
    lines = []
    for q_, a in zip(EXAM, answers):
        s, d = score_one(q_, a)
        lines.append(f"    {q_['repo']:32s} 应{q_['want']:5s} 判{str(a.get('verdict') or '空'):5s} "
                     f"{s:.2f}  {d}")
    return total, per_cls, lines, cached


def failure_feedback(lines, total, per_cls):
    bad = [l for l in lines if "判错" in l or "踩中" in l or "答不出" in l]
    # 【2026-08-06 落「下一刀」】此前失分明细按题号顺序原样喂 —— 而 skip 类失分
    #   最好修(拒绝比识别容易),改进者自然全往那边跑:首轮实测涨分 100% 来自
    #   skip 类,adopt 类纹丝不动。要它啃真瓶颈,反馈必须自己指方向:
    #   adopt 类失分排最前(sort 稳定,类内保持原序,截断也截不掉它们)+ 明写价值排序。
    bad.sort(key=lambda l: 0 if "应adopt" in l else 1)
    n_adopt = sum(1 for l in bad if "应adopt" in l)
    sk, ad = per_cls.get("skip", 0), per_cls.get("adopt", 0)
    return (f"现任判定提示词总分 {total:.4f}(skip 类 {sk:.4f} / adopt 类 {ad:.4f})。\n"
            f"【修哪类最值钱 · 按这个顺序啃】\n"
            f"  · adopt 类(认出真机会){ad:.4f} 是瓶颈:拒绝的价值有上限,认出机会的"
            f"价值没有上限 —— 漏报一个真机会 = 整个自进化闭环少一份燃料。"
            f"下面 {n_adopt} 道 adopt 类失分题排在最前,优先修它们。\n"
            f"  · skip 类(拒绝陷阱){sk:.4f}:已有的拒绝能力一分都不能丢,"
            f"但在它上面继续加码几乎不涨总分。\n"
            f"失分明细(共 {len(bad)} 题,adopt 类在前):\n" + "\n".join(bad[:12]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-supplier", default="dashscope",
                    help="用哪家来考卷(考生固定一家,否则分数不可比)")
    ap.add_argument("--out", default="radar_race_result.json")
    ap.add_argument("--baseline-only", action="store_true",
                    help="只跑现任大脑,验可复现,不派改进者")
    ap.add_argument("--promote", action="store_true",
                    help="过闸即真写方案槽换冠军;不给则只打印「本应晋升谁」(影子期默认)")
    a = ap.parse_args()

    sys.path.insert(0, HERE)
    # brain() = 真正在跑的那个大脑(D1 在位冠军,取不到才回落源码常量)。
    # 必须用它而不是 SYS_ADOPT:晋升一次之后源码常量就不再是现任,
    # 拿它当基准 = 赛马比的不是真正在跑的东西,分数全部失去意义。
    from adopt import brain                                    # noqa: E402
    SYS_ADOPT = brain()

    bl = baselines()
    print(f"雷达赛马 · 卷子 {SCORER_VERSION} · {len(EXAM)} 题 · 考生 {a.baseline_supplier}")
    print(f"傻瓜基线:" + " · ".join(f"{k} {v:.4f}" for k, v in bl.items()))
    print("─" * 76)

    t0 = time.time()
    base_total, base_cls, base_lines, base_cached = exam_one(
        SYS_ADOPT, a.baseline_supplier, SYS_ADOPT)
    print(f"【现任判定大脑】总分 **{base_total:.4f}** "
          f"(skip {base_cls.get('skip', 0):.4f} / adopt {base_cls.get('adopt', 0):.4f}) "
          f"· 缓存命中 {base_cached}/{len(EXAM)} · {time.time()-t0:.1f}s")
    for l in base_lines:
        print(l)
    if base_total <= max(bl.values()):
        print(f"  ⚠️ 现任大脑没赢过傻瓜基线({max(bl.values()):.4f})—— 这本身就是结论。")

    rows = [dict(who="现任(冠军)", supplier="-", score=base_total, delta=0.0,
                 cls=base_cls, note=f"缓存 {base_cached}/{len(EXAM)}")]

    if not a.baseline_only:
        fb = failure_feedback(base_lines, base_total, base_cls)
        for sup, want_model, label in RACERS:
            t1 = time.time()
            print(f"\n── {label}")
            try:
                txt, model = _ask_any(
                    SYS_REWRITE,
                    f"【现任评审提示词】\n{SYS_ADOPT}\n\n【它的真实失分】\n{fb}\n\n"
                    f"改写它。只输出提示词全文。", sup, model=want_model)
                # 点名的 vs 真跑的:两者不一致 = 网关换了马,分数不能算在点名那家头上。
                # (架构参谋的判词:记分只认 served_by,不认点名 —— 这是赛马变真的开关)
                if want_model and model and want_model.split("/")[-1] not in str(model):
                    print(f"   ⚠️ 点名 {want_model} 实跑 {model} —— 记分按实跑")
            except Exception as e:
                # 截到 70 字会把网关带回来的 tried/errors 切掉,而那才是真因所在
                # ——「503」这三个字本身没有任何诊断价值。放到 400。
                msg = str(e)[:400]
                print(f"   ✗ 调用失败 {type(e).__name__}: {msg}")
                rows.append(dict(who=label, supplier=sup, want_model=want_model,
                                 score=None, delta=None, cls={},
                                 note=f"调用失败 {type(e).__name__}: {msg[:200]}"))
                continue
            cand = (txt or "").strip()
            if cand.startswith("```"):
                cand = cand.split("```")[1].lstrip("json").strip()
            # 硬闸:JSON 八字段一个都不能少(少了雷达根本解析不了它的判定)
            miss = [f for f in ["module", "verdict", "metric", "how", "effort",
                                "current", "beats", "why"] if f'"{f}"' not in cand]
            if len(cand) < 200 or miss:
                why = "产出过短" if len(cand) < 200 else "丢失 JSON 字段:" + "/".join(miss)
                print(f"   ✗ {why}")
                rows.append(dict(who=label, supplier=sup, score=None, delta=None,
                                 cls={}, note=why))
                continue
            tot, cls, lines, cch = exam_one(cand, a.baseline_supplier, cand)
            d = round(tot - base_total, 4)
            flag = "🏆 赢了" if d > 0 else ("持平" if d == 0 else "没赢")
            print(f"   {model} · 提示词 {len(cand)} 字 · 总分 **{tot:.4f}** "
                  f"({d:+.4f}) {flag} · {time.time()-t1:.1f}s")
            rows.append(dict(who=label, supplier=sup, score=tot, delta=d, cls=cls,
                             note=f"{model} · {len(cand)}字", body=cand))

    # ── 晋升闸 ────────────────────────────────────────────────
    # 【2026-08-06 首轮实测后立此闸】首次有挑战者赢过冠军(+0.0500),
    #   但拆开看:**涨分 100% 在 skip 类(0.9000→1.0000),adopt 类 0.5000 纹丝不动**。
    #   它学会的是"更会拒绝",不是"更会认出机会"。
    #   宏平均防住了「全判 skip」这个退化策略,却没防住次级退化:
    #     skip 类 10 题、adopt 类 4 题,而**拒绝比识别容易得多** ——
    #     只要在 skip 类刷满分,总分就涨,adopt 类可以一动不动。
    #   而对一个情报雷达来说:**拒绝的价值有上限(最多省下审核人的时间),
    #     认出机会的价值没有上限 —— 那才是整个自进化系统的燃料。**
    #   一个从不认机会的雷达,拒绝得再干净也是废的。
    # 所以晋升要两个条件同时成立,总分涨不够:
    #   ① 总分涨;② **adopt 类不许退步**(退步 = 用真机会换假干净,亏本买卖)。
    def promotable(r):
        if not r.get("score") or r["score"] <= base_total:
            return False, "总分没涨"
        a_new = (r.get("cls") or {}).get("adopt", 0)
        a_old = base_cls.get("adopt", 0)
        if a_new < a_old:
            return False, f"adopt 类退步({a_old:.4f}→{a_new:.4f})—— 拿真机会换假干净"
        if a_new == a_old:
            return True, f"可晋升,但**只是更会拒绝**(adopt 类 {a_new:.4f} 没动)"
        return True, f"可晋升,且真的更会认机会(adopt {a_old:.4f}→{a_new:.4f})"

    win = [r for r in rows[1:] if r.get("delta") and r["delta"] > 0]
    print("\n" + "═" * 76)
    if win:
        b = max(win, key=lambda r: r["delta"])
        ok, why = promotable(b)
        print(f"{'✅' if ok else '⛔'} 最高分挑战者:{b['supplier']} "
              f"{b['score']:.4f} vs 现任 {base_total:.4f}({b['delta']:+.4f})")
        print(f"   晋升闸:{why}")
        print(f"   分类明细:skip {base_cls.get('skip',0):.4f}→{(b.get('cls') or {}).get('skip',0):.4f} · "
              f"adopt {base_cls.get('adopt',0):.4f}→{(b.get('cls') or {}).get('adopt',0):.4f}")
        print("   → **不自动换冠军**,晋升由人拍板。")
        # 点名却落到同一个模型 = 赛马是假的,必须当场喊出来
        mods = [str(r.get("note", "")).split(" · ")[0] for r in rows[1:] if r.get("score")]
        if len(mods) > 1 and len(set(mods)) == 1:
            print(f"   ⚠️ **这不是多家赛马**:点名的 {len(mods)} 家全落到同一个模型 "
                  f"`{mods[0]}` —— 网关 fallback 把点名吃掉了,等于同一个模型赢了 {len(mods)} 次。")
    else:
        print(f"❌ 没有一家改得更准。现任 {base_total:.4f} 保持冠军。")
        print("   注意:**本轮无改进也是合法结果**,不许为了让它赢去松判据。")

    # ── 晋升写入端(刀2b·闭合断头)────────────────────────────────
    # 此前到上面为止就结束了:算出更好的大脑,只打印一张表给人看,
    # 冠军永远换不上去 —— 赛马天天跑、天天空转,这就是"人读报告=没自动化"。
    # 现在真写方案槽。**默认 dry-run**,`--promote` 才真写:
    #   创始人已拍板"配置级自动晋升",但同时定了 7 天影子期先验判得对不对。
    #   影子期里每天打印"本应晋升谁",人工复核正确率,达标后 workflow 加 --promote。
    if win:
        b = max(win, key=lambda r: r["delta"])
        ok, why = promotable(b)
        if ok and b.get("body"):
            if a.promote:
                vid = "v_" + hashlib.sha1(
                    (b["body"] + SCORER_VERSION).encode("utf-8")).hexdigest()[:12]
                now = int(time.time())
                try:
                    from _ai import d1, q                       # noqa: E402
                    # 先退位再上任:同一 slot 只许一个 active,否则 load 到谁全看排序
                    d1(f"UPDATE evolve_variants SET status='retired',updated_at={now} "
                       f"WHERE scope='radar' AND slot='SYS_ADOPT' AND status='active'")
                    d1(f"INSERT INTO evolve_variants (variant_id,scope,slot,body,status,"
                       f"mode,author,note,fitness,sample_n,created_at,updated_at) VALUES ("
                       f"{q(vid)},'radar','SYS_ADOPT',{q(b['body'])},'active','improve',"
                       f"{q('freepool:' + str(b.get('supplier'))[:32])},"
                       f"{q('赛马晋升 %.4f→%.4f · %s' % (base_total, b['score'], why))},"
                       f"{b['score']},{len(EXAM)},{now},{now})")
                    print(f"✅ 已晋升 {vid} —— 下一轮 adopt 判定即用新大脑")
                except Exception as e:
                    print(f"⛔ 晋升写库失败({type(e).__name__}:{str(e)[:80]}),冠军未变")
            else:
                print(f"🅿️ [影子期·dry-run] **本应晋升** {b['supplier']} "
                      f"{base_total:.4f}→{b['score']:.4f} · {why}")
                print("   （复核无误后给 workflow 加 --promote 即真写；本轮未动库）")

    json.dump(dict(scorer=SCORER_VERSION, baseline=base_total, baselines=bl,
                   supplier=a.baseline_supplier, promoted=bool(a.promote), rows=rows),
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"结果表 → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
