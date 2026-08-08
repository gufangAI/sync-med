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
import os, sys, json, time, hashlib, argparse, itertools

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
sys.path.insert(0, HERE)

from _ai import ask, ask_opencode, _unfence                    # noqa: E402
from radar_set import EXAM, score_all, score_one, baselines, SCORER_VERSION  # noqa: E402
sys.path.insert(0, HERE)
from improve import strip_wrapper                              # noqa: E402

# ── 认知裂变参数(创始人 2026-05-22 定义的组合序列网状裂变)──────────────
# 亲本上限 3 → 两两组合最多 3 对:够产生新认知,又不让考卷调用量爆炸
# (每对要跑一次合成 + 一整张 14 题卷;冻结考卷会吃掉重复题,实际开销更小)。
MAX_FISSION_PARENTS = int(os.environ.get("RADAR_FISSION_PARENTS", "3"))
MAX_FISSION_PAIRS = int(os.environ.get("RADAR_FISSION_PAIRS", "3"))
# 合成者固定一家:它只做"取两者之长"这一件事,不参与赛马,
# 换模型会让组合体的可比性漂移 —— 同一个合成者,组合结果才归因得清。
# 【2026-08-08 修·首跑实锤】原用 opencode 免密档,但 ask_opencode 注释自己写了
#   "带 system 消息时 deepseek 进思考模式、content 空、正文落 reasoning_content 被丢弃"。
#   裂变合成是长任务(两版提示词+角色设定),必触发思考模式 → 返回 0 字 → 组合体全灭。
#   改走网关侧免费家:zhipu glm-4-flash 直测稳定(1.1s)、不进思考模式、守 JSON 契约。
# 合成者兜底链(按 2026-08-08 直测通过顺序):zhipu 1.1s → cerebras 2.0s → nvidia 2.9s。
# 严格模式下单家 503 就是挂,给它一条链而不是死磕一家 —— 但**同一轮所有组合用同一个
# 成功家**,不许这对用 zhipu、那对用 nvidia(换合成者=组合可比性漂移)。
FISSION_CHAIN = os.environ.get("RADAR_FISSION_CHAIN", "zhipu,cerebras,nvidia").split(",")

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CACHE_DIR = os.path.join(REPO_ROOT, "reports", "evolve", "radar_cache")

# 白名单里的 platform → 网关 supplier(网关 provider 表里真实存在的接入点)。
# 白名单只记 `模型@平台`,网关按 supplier 索引 base+key,两边靠这张表对上。
_PLATFORM_SUPPLIER = {
    "tencent":    "tc_dsflash",   # tokenhub.tencentmaas.com,配 model 覆盖即可换模型
    "nvidia":     "nvidia",       # integrate.api.nvidia.com
    "openrouter": "openrouter",
}

# ── 主力赛手:opencode.ai/zen 的免密免费档 ────────────────────────
# 【2026-08-07 创始人点名:「opencode 的免费 deepseek 要用上」】
# 当天逐个实测(真跑同一道长提示词改写任务,判据=正文≥500字):
#   deepseek-v4-flash-free 1537字/6.6s · mimo-v2.5-free 1940字/18.5s
#   ling-3.0-flash-free 947字/3.7s · nemotron-3-ultra-free 3717字/47s
#   laguna-s-2.1-free 1121字/8.8s · longcat-2.0-free 1543字/46.6s
#   big-pickle 1696字/12.8s
#   (剔除:ling-3.0-tiny-free 503、north-mini-code-free 401)
# 为什么它比走网关的车道更适合当赛马主力:
#   ① **真免密**,不占任何 key、不吃额度;
#   ② NVIDIA 免费层实为一次性 5000 credits 试用,用完即止,不能当稳定赛道;
#   ③ 七个不同家族(deepseek/mimo/ling/nemotron/laguna/longcat/pickle),
#      **这才是赛马要的真多样性** —— 同一个模型戴七顶帽子仍是同一个脑子。
_OPENCODE_FREE = [
    ("opencode", "deepseek-v4-flash-free", "opencode · deepseek-v4-flash-free"),
    ("opencode", "mimo-v2.5-free",         "opencode · mimo-v2.5-free"),
    ("opencode", "ling-3.0-flash-free",    "opencode · ling-3.0-flash-free"),
    ("opencode", "nemotron-3-ultra-free",  "opencode · nemotron-3-ultra-free"),
    ("opencode", "laguna-s-2.1-free",      "opencode · laguna-s-2.1-free"),
    ("opencode", "longcat-2.0-free",       "opencode · longcat-2.0-free"),
    ("opencode", "big-pickle",             "opencode · big-pickle"),
]

# 网关侧补充赛手:只留 2026-08-07 直测**真通**的(nvidia 默认 2.9s / zhipu 1.1s /
# cerebras 2.0s)。刻意不含 dashscope:付费源,memory 铁律定性「只兜底」,
# 不该当固定赛手(当天严格模式暴露:它一度是唯一跑得通的挑战者)。
# openrouter 也不含:实测它的 served_by 是 nvidia/nemotron-3-ultra-550b:free,
# 与 nvidia 默认**同一个模型** —— 点两家等于同一个脑子,是第二层假多样性。
_GATEWAY_RACERS = [
    ("nvidia",   None, "gateway · nvidia 默认(nemotron-3-ultra)"),
    ("zhipu",    None, "gateway · 智谱 glm-4-flash"),
    ("cerebras", None, "gateway · cerebras zai-glm-4.7"),
]

_FALLBACK_RACERS = _OPENCODE_FREE + _GATEWAY_RACERS


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
    # 【2026-08-07 实测后调序】白名单的模型名是 07-25 探的,13 天后再跑
    # **7 家里 6 家 HTTP 503** —— 不是平台挂了,是那几个模型名在上游已失效
    # (deepseek-v4-flash 在 NVIDIA/腾讯两边都死、llama-4-maverick 死、
    #  kimi-k2.7-code-highspeed 死;而同平台的 youtu-vita 1.2s 秒通)。
    # 「可用性是运行时统计量,不是配置里的布尔值」——静态清单必然衰减。
    # 所以主力改用当天实测通过的免密档,白名单降级为**追加候选**:
    # 它带来的多样性是净增量,但不能再当唯一来源。
    racers = list(_FALLBACK_RACERS)
    path = os.path.join(REPO_ROOT, "scripts", "model_pool_whitelist.json")
    try:
        with open(path, encoding="utf-8") as f:
            grp = (json.load(f).get("recommend") or {}).get("race_diversity") or []
    except Exception as e:
        print(f"  [赛手] 白名单读取失败({type(e).__name__}),只用实测名单", flush=True)
        return racers
    out = []
    seen = {(s, m) for s, m, _ in racers}
    added = 0
    for item in grp:
        if "@" not in str(item):
            continue
        mdl, plat = str(item).rsplit("@", 1)
        sup = _PLATFORM_SUPPLIER.get(plat)
        if not sup:                       # 平台没有对应网关接入点 = 跳过,不硬凑
            continue
        if (sup, mdl) in seen:            # 与实测名单重复的不再追加
            continue
        seen.add((sup, mdl))
        racers.append((sup, mdl, f"whitelist · {plat}/{mdl}"))
        added += 1
    print(f"  [赛手] 实测免密 {len(_OPENCODE_FREE)} + 网关实测 {len(_GATEWAY_RACERS)} "
          f"+ 白名单追加 {added} = 共 {len(racers)} 家", flush=True)
    return racers


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


# 认知裂变专用的合成提示词。
# 【2026-08-08 修·首跑实锤】此前借用了内容线的 SYS_CROSSOVER —— 那个是给
#   biocomp/herb 内容产线写的,只说"保留 JSON 输出格式约定"但没点明是哪 8 个字段。
#   合成模型不知道判定大脑要输出 module/verdict/.../why,就把格式丢了,
#   裂变首跑组合体全军覆没("丢失 JSON 字段")。
# 这正是"学透"的反面在我自己身上:复用了 SYS_CROSSOVER 却没读它要模型输出什么。
# 判定大脑有它自己的输出契约(见 SYS_REWRITE 结尾),裂变的合成必须守同一份契约。
SYS_FISSION = (
    "你是**技术选型评审提示词的合成师**。给你**两版**评审提示词,它们在同一套真题上各有长处。\n"
    "任务:**取两者之长合成第三版** —— 不是二选一,不是简单拼接。哪一版在某类判断上更准,"
    "就吸收它那一段的判断逻辑。目标是让第三版在**两个亲本都没做好的地方**也判得准。\n\n"
    "【这套系统在判什么】一个开源项目值不值得接进平台。运行形态 Cloudflare Serverless "
    "+ GitHub Actions,零常驻主机、零月费。两类高频错:明星光环(高星但要常驻机器)、"
    "常识复读(推荐我们已在用的工具)。\n"
    "【判分:确定性打分】verdict 判对 0.60;adopt 须答 current(现用什么)+beats(哪个指标更强),"
    "skip 须答 why,判对才 +0.25;判错说明分不计;陷阱题(常驻/已在家底)判 adopt 失 0.15;"
    "总分按类别宏平均,skip/adopt 两类都要准。\n\n"
    "【输出格式硬要求 —— 与被合成的提示词完全一致,少一条整版作废】\n"
    "  · 只输出合成后的提示词全文。不要开场白、不要「合成说明」、不要用 ``` 包裹。\n"
    "  · **必须在提示词结尾原样保留这段 JSON 输出约定**(逐字复制,不要改写、不要用 markdown 列表):\n"
    "    只输出 JSON:{\"module\":\"清单里的模块名或空\",\"verdict\":\"adopt|watch|skip\","
    "\"metric\":\"可量化指标\",\"how\":\"怎么接\",\"effort\":\"工作量\","
    "\"current\":\"我们现在用什么\",\"beats\":\"在哪个指标上更强\",\"why\":\"理由\"}\n"
    "  · 上面八个字段名必须是**带双引号的 JSON key**(形如 \"module\"),"
    "不许写成 markdown 反引号(`module`)或列表项 —— 那样判定大脑解析不了。\n"
    "  · 长度与较长那版相当,不许暴涨。"
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
        return ask_opencode(system, user, max_tokens=max_tokens, need="", model=model)
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
    ap.add_argument("--no-fission", action="store_true",
                    help="关掉认知裂变(只跑单体赛马)。默认开 —— 裂变是创始人钦定的核心机制")
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
            rows.append(dict(who=label, supplier=sup, want_model=want_model,
                             served_by=model,        # ← 真跑的那个,不是点名的那个
                             score=tot, delta=d, cls=cls,
                             note=f"{model} · {len(cand)}字", body=cand))

    # ══ 认知裂变(2026-08-07 首次落地·创始人 2026-05-22 10:20 定义)══════
    # 创始人原话:「不是 1+2+3+4=一个总模型,也不是四个专家平行讨论,而是
    #   1、2、3、4、1+2、1+3、1+4、2+3、2+4、3+4、1+2+3…… 这些不同组合
    #   都会成为 SueAI 的认知路径。所以第 5 本书进来时,不是一个模型读它,
    #   而是很多已经形成的组合认知一起读它。」他给它命名:组合序列网状裂变学习机制。
    #
    # **这条定义至今零行代码** —— 14 份自进化文档零命中(2026-08-07 全文考古)。
    # 此前的赛马是「17 个模型各改一版 → 选最高分」= **选拔**,不是裂变:
    #   赢家通吃、输家消失,下一轮从冠军再派生 —— 近亲繁殖,认知只有一条线。
    # 裂变要的是:让活下来的认知**互相组合**,组合体也是一等公民、也要上考卷。
    #
    # 本轮落地前三件(第四件「多认知路径共同判定」是 adopt 侧,下一步):
    #   ① 组合:取本轮**有价值的**候选两两合成新认知路径(SYS_CROSSOVER 早写好、从未被调用)
    #   ② 组合体上同一张冻结考卷,与单体同台竞争
    #   ③ **不否定任何一个** —— 全部候选(含没赢的)进休眠池,下轮可复活参与组合
    #      (创始人 2026-06-20:「总有人排到后面,但随着技术提升也会进步,
    #        所以不否定任何一个!只是排后」;2026-08-06 又骂过一刀切)
    if not a.baseline_only and not a.no_fission:
        scored = [r for r in rows[1:] if r.get("score") is not None and r.get("body")]
        # 组合亲本判据:**不是简单取 top-K**,而是"有价值的" ——
        #   ≥ 现任基线的都算有价值(哪怕只是持平:它可能在别的类别上有独到之处,
        #   而宏平均把它抹平了)。这正是"不否定任何一个"在选亲本时的落法。
        parents = sorted([r for r in scored if r["score"] >= base_total],
                         key=lambda r: -r["score"])[:MAX_FISSION_PARENTS]
        if len(parents) >= 2:
            pairs = list(itertools.combinations(range(len(parents)), 2))[:MAX_FISSION_PAIRS]
            print(f"\n{'─'*76}\n🧬 认知裂变:{len(parents)} 个有价值认知 → 组合 {len(pairs)} 条新路径")

            def _synthesize(user):
                """按兜底链找一个能出货的合成家;同一轮里第一个成功的家会被记住,
                后续组合都用它,保证组合结果可比。全链挂才抛。"""
                errs = []
                for sup in ([_synthesize.locked] if _synthesize.locked else FISSION_CHAIN):
                    try:
                        t, mo = _ask_any(SYS_FISSION, user, sup)
                        _synthesize.locked = sup          # 锁定本轮合成家
                        return t, mo
                    except Exception as e:
                        errs.append(f"{sup}:{type(e).__name__}")
                        continue
                raise RuntimeError("合成链全挂 " + " / ".join(errs))
            _synthesize.locked = None

            for i, j in pairs:
                pa, pb = parents[i], parents[j]
                tag = f"{pa['who']} ⊕ {pb['who']}"
                t2 = time.time()
                print(f"\n── 🧬 {tag}")
                try:
                    txt, cmodel = _synthesize(
                        f"【版本A】\n{pa['body']}\n\n【版本B】\n{pb['body']}\n\n"
                        f"【它们各自的成绩】A={pa['score']:.4f}(skip {(pa.get('cls') or {}).get('skip',0):.4f}/"
                        f"adopt {(pa.get('cls') or {}).get('adopt',0):.4f}) · "
                        f"B={pb['score']:.4f}(skip {(pb.get('cls') or {}).get('skip',0):.4f}/"
                        f"adopt {(pb.get('cls') or {}).get('adopt',0):.4f})\n"
                        f"合成第三版。只输出提示词全文。")
                except Exception as e:
                    print(f"   ✗ 合成失败 {type(e).__name__}: {str(e)[:200]}")
                    continue
                cand = strip_wrapper(_unfence(txt or ""))
                miss = [f for f in ("module", "verdict", "metric", "how", "effort",
                                    "current", "beats", "why") if f'"{f}"' not in cand]
                if len(cand) < 200 or miss:
                    why = "产出过短" if len(cand) < 200 else "丢失 JSON 字段:" + "/".join(miss)
                    print(f"   ✗ {why}")
                    rows.append(dict(who=f"🧬 {tag}", supplier="fission", kind="crossover",
                                     score=None, delta=None, cls={}, note=why))
                    continue
                tot, cls, _lines, _c = exam_one(cand, a.baseline_supplier, cand)
                d = round(tot - base_total, 4)
                # 组合体的真价值不只看总分:它可能在**单个亲本都没做到的类别**上突破
                pa_ad = (pa.get("cls") or {}).get("adopt", 0)
                pb_ad = (pb.get("cls") or {}).get("adopt", 0)
                c_ad = cls.get("adopt", 0)
                breakthrough = c_ad > max(pa_ad, pb_ad)
                mark = "🏆 赢了" if d > 0 else ("持平" if d == 0 else "没赢")
                extra = "  ⭐**adopt 类超过两个亲本**(裂变真出了新东西)" if breakthrough else ""
                print(f"   {cmodel} · {len(cand)}字 · 总分 **{tot:.4f}** ({d:+.4f}) {mark}{extra}")
                rows.append(dict(who=f"🧬 {tag}", supplier="fission", kind="crossover",
                                 served_by=cmodel, score=tot, delta=d, cls=cls,
                                 parents=[pa["who"], pb["who"]], breakthrough=breakthrough,
                                 note=f"裂变 · {cmodel} · {len(cand)}字", body=cand))
        else:
            print(f"\n🧬 认知裂变:有价值认知不足 2 个(得 {len(parents)} 个),本轮不组合")

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
        # ── 真多样性核算:一律按 served_by,不按点名 ──────────────
        # 【学 OpenRouter 的思维,不是用 OpenRouter】它值钱的地方是
        #   **served_by 必回** —— 你永远知道实际是谁跑的。我们今天两次被这个咬:
        #   ① fallback 静默换马:点名 5 家、3 家 503,被换成同一个 Qwen3-8B;
        #   ② 两个 supplier 配了同一模型:openrouter 的 served_by 实测是
        #      `nvidia/nemotron-3-ultra-550b:free`,与 nvidia 默认同一个脑子。
        # 按 supplier 去重看不见这两层,按 served_by 才看得见。
        served = [str(r.get("served_by") or "") for r in rows[1:] if r.get("score") is not None]
        uniq = {s for s in served if s}
        print(f"   真多样性:{len(served)} 家跑出成绩 → 实际 **{len(uniq)}** 个不同模型")
        if served and len(uniq) < len(served):
            from collections import Counter
            dup = [f"{m}×{n}" for m, n in Counter(served).items() if n > 1]
            print(f"   ⚠️ 有点名落到同一模型(按 served_by 认):{', '.join(dup)} "
                  f"—— 记分只认 served_by,同一模型不重复计入多样性。")
        if len(uniq) == 1 and len(served) > 1:
            print(f"   ⛔ **这不是多家赛马**:全部落到 `{list(uniq)[0]}`,等于一个模型跑了 {len(served)} 次。")
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
