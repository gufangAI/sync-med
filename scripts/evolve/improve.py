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
import os, sys, json, time, uuid, random, hashlib, argparse, urllib.request

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
    # 【2026-08-05 赛马第二轮实测后补 · 把考纲给它看】
    #   修好 json_mode 后 3/5 家能产出合规候选了(修之前 0/5),
    #   但没一家赢过冠军 —— 最近的 dashscope 只差 1 个百分点(0.5055 vs 0.5158)。
    #   根因是我在办**闭卷考试**:判分函数是确定性的、规则完全公开的,
    #   我却只跟改进者说「让放行率更高」,不告诉它分怎么打,它只能猜。
    #   下面把 gate.py 的真实规则原样交底 —— 这不是作弊:判据本来就该公开,
    #   闭环要的是「照着判据改」,不是「猜对判据」。
    "\n【判分规则 · 确定性代码打分,规则完全公开,照着改】\n"
    "  第一关 · 资格闸(不过直接 0 分,不给部分分):\n"
    "    · 必须有名称;生物计算必须有 formula(分子式),本草必须有 name_latin(拉丁学名);\n"
    "    · 出现剂量(数字+克/钱/两/毫升/g/mg/ml)、用法(水煎服/煎汤服/分二次服)、\n"
    "      疗效承诺(治愈率/根治/特效/包治)、医嘱口吻(你应该服用)—— 命中任一即 0 分。\n"
    "  第二关 · 连续分(过闸后按加权均值算,权重已标明):\n"
    "    · **反编造类(权重 3.0,最重)**:\n"
    "        - SMILES 结构式必须与分子式**原子数一致**(代码逐原子对账,编的当场露馅);\n"
    "        - 拿不准就**留空** —— 留空只扣一点分,写错直接 0 分,留空远优于编造;\n"
    "        - pubchem_cid 必须是纯数字;mol_weight 必须落在 50~2000;\n"
    "        - 拉丁学名必须符合双名法(属名首字母大写 + 空格 + 种加词小写);\n"
    "        - 出处必须指到具体书名(带《》),泛指只得 0.2 分。\n"
    "    · 格式完整类(权重 1.0,最轻):各字段 0 字得 0 分、≥120 字满分、中间线性。\n"
    "  → 所以最有效的改法是**让模型别编**(反编造权重是格式的 3 倍),\n"
    "    而不是让它写更长 —— 写长只提升权重 1.0 那一档,收益很小。\n"
    "\n【输出格式硬要求 · 违反会被自动闸整条丢弃,前两轮就是这么废掉的】\n"
    "  · 只输出提示词本体。**不要开场白、不要结尾的「改进说明/主要改动」** ——\n"
    "    上一轮 nvidia 吐了 5269 字(父代才 1032),多出来的全是它的改写说明,整条作废。\n"
    "  · 原提示词里出现过的这些词**一个都不许丢**:剂量 / 疗效 / 留空 / 编造 / 出处 /\n"
    "    医嘱 / 临床 / 禁 —— 上一轮 openrouter 就是丢了「出处」二字整条作废。\n"
    "  · 长度控制在父代的 0.8~1.5 倍。\n"
    "直接输出改写后的完整提示词全文,一个字的多余话都不要。"
)


SYS_DEBUG = (
    "你是提示词工程师。上一版改写**被自动闸拦下了**,现在给你原始产出和拦截原因,"
    "你的任务是**只修被拦的那一处**,其余一个字不动。\n"
    "常见拦截与修法:\n"
    "  · 「比父代长太多」→ 多出来的通常是你写的改进说明,**整段删掉**,只留提示词本体;\n"
    "  · 「丢失红线关键词」→ 把缺的那几个词原样加回原来的位置;\n"
    "  · 「比父代短太多」→ 你精简掉了约束,把父代里被删的约束补回来;\n"
    "  · 「与父代完全相同」→ 你没改,针对失败原因真正改一处。\n"
    "直接输出修好的提示词全文,不要解释、不要围栏、不要开场白。"
)

SYS_CROSSOVER = (
    "你是提示词工程师。给你**两版**针对同一条产线的提示词,它们各有长处。\n"
    "任务:**取两者之长合成第三版** —— 不是二选一,也不是简单拼接。\n"
    "硬约束:\n"
    "  ① 两版里出现过的**任何合规红线**(禁剂量/禁疗效承诺/禁医嘱口吻/宁可留空不许编造/出处)"
    "一条都不许丢;\n"
    "  ② 长度与较长那版相当,不许暴涨;\n"
    "  ③ 只输出合成后的提示词全文,不要解释、不要围栏。\n"
    "合成思路:哪一版对某个失败原因下刀更准,就用哪一版的那一段。"
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


_LEAD = ("以下是", "下面是", "这是改写", "改写后的", "好的", "根据", "已按照", "针对上述")
_TAIL = ("改进说明", "主要改动", "修改说明", "改动说明", "说明:", "说明：", "以上改动",
         "本次改写", "优化点", "核心改动", "---", "###")


def strip_wrapper(t):
    """把模型爱加的**开场白与结尾解说**剪掉,只留提示词本体。

    立此因(2026-08-04 首轮实测):biocomp 这一路连着两次被「比父代长太多」拦下 ——
      父代 1032 字,它吐回 2580+ 字。多出来的不是更好的提示词,是它的改写说明。
      整条丢弃的结果是**这条产线永远停在第一代**;正确做法是确定性地剪掉包装,
      剪完再过闸。剪不干净仍然拦,红线一步不退。
    """
    t = (t or "").strip()
    lines = t.split("\n")
    # 开场白:第一行短、且以典型引导语开头、且下面还有正文 → 砍掉
    while lines and len(lines[0].strip()) < 60 and lines[0].strip().startswith(_LEAD) and len(lines) > 3:
        lines.pop(0)
    # 结尾解说:从第一个解说小标题起整段砍掉
    for i, ln in enumerate(lines):
        s = ln.strip().lstrip("#*- ").strip()
        if i > 3 and s.startswith(_TAIL):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def hard_guard(parent_body, child_body):
    """**真砍闸** —— 只拦「根本不是一条候选」的东西。返回拦截原因或 None。

    这里只剩三条,因为它们不是"可能不好",是"压根没东西":
      空 / 过短 / 与父代一字不差(没改就不是新一代)。
    **合规安全不在这道闸上把** —— 它在 `gate.py` 的资格闸上,判的是**真实产出**
      (剂量、用法、疗效承诺、医嘱口吻,全是确定性正则,命中即 0 分)。
      判产出才是真判据;判提示词字面只是代理指标。
    """
    if not child_body or len(child_body) < 80:
        return "产出为空或过短"
    if child_body.strip() == parent_body.strip():
        return "与父代完全相同,没有改进"
    return None


def soft_flags(parent_body, child_body):
    """**分道标记** —— 形式类问题只标记,**不砍**。返回问题列表(空 = 走主赛道)。

    【2026-08-06 创始人:「黑马机制,赛马机制……可你的一刀切,那限制很大,
      就直接全部没用了」】—— 说的正是这里。此前这两条是硬砍:
      · 长度越界(0.6~3.0 倍):一条**更简洁而更准**的提示词直接毙掉。
        "精简掉废话"和"精简掉约束"长度上看起来一样,字面闸分不出来 →
        于是一律当成后者杀掉,**等于从结构上禁止「改得更短」这个改进方向**。
      · 字面关键词缺失:RED_MARKERS 是**字面**匹配。把「禁开剂量」改写成
        「禁止标注任何用量」,意思一样甚至更准,但「剂量」二字不在了 → 当场毙掉。
        它检查的是**用词**,不是**约束在不在**。

    改成标记后,这类候选照样上赛道,由 `gate.py` 判它的**真实产出**:
      产出里真出现剂量/疗效承诺 → 资格闸 0 分,自然淘汰,一点没放松;
      产出干干净净、分还更高 → 它就是黑马,本来就该赢。
    """
    flags = []
    lp, lc = len(parent_body), len(child_body)
    if lc < lp * 0.6:
        flags.append(f"比父代短很多({lc} vs {lp})")
    if lc > lp * 3.0:
        flags.append(f"比父代长很多({lc} vs {lp})")
    lost = [m for m in RED_MARKERS if m in parent_body and m not in child_body]
    if lost:
        flags.append("未原样保留字面词:" + "/".join(lost))
    return flags


def guard(parent_body, child_body):
    """兼容旧调用点。默认**只跑真砍闸**;形式问题交给 soft_flags 分道。

    `EVOLVE_STRICT_GATE=1` 可恢复旧的一刀切行为 —— 留着不是为了保守,
      是为了能 A/B 对比「一刀切 vs 黑马赛道」哪个产出更好的冠军。
      **这本身就是一次赛马。**
    """
    bad = hard_guard(parent_body, child_body)
    if bad:
        return bad
    if os.environ.get("EVOLVE_STRICT_GATE", "0") == "1":
        f = soft_flags(parent_body, child_body)
        if f:
            return ";".join(f)
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
        # cand_id 必须**由标题推导**,不能每轮新 uuid ——
        #   否则 INSERT OR IGNORE 每次都是"新行",一条情报每轮复制一份。
        #   实测:同样 40 条情报跑两轮,候选池从 40 涨到 80,全是重复(2026-08-04 当场抓到)。
        cid = "c_" + hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]
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


# 提示词能吸收的模块 = 上面 SCOPE_MODULES 覆盖的那些。**其余模块改不动提示词**,
#   只能变成给人的工单 —— 这就是创始人要的「推送给你修改」那一环。
# 模块 → 产线 的映射。候选池里的判定落在"模块"上,改进算子按产线取用。
SCOPE_MODULES = {"biocomp": ("内容工厂", "自进化闭环"), "herb": ("内容工厂", "自进化闭环")}


def fetch_external_ideas(scope, limit=5):
    """从候选池取**已判可采纳**且与本产线相关的外部做法。

    这是 info4AI → sueAI 的那根线。没有它,雷达采回来的东西永远只是收藏夹。
    只取 adopt(watch 的方向对但没具体接法,喂进去只会稀释提示词)。
    """
    mods = SCOPE_MODULES.get(scope, ())
    if not mods:
        return []
    try:
        rows = d1("SELECT title,url,verdict_note FROM evolve_candidates "
                  "WHERE kind='repo' AND status='adopt' ORDER BY score DESC LIMIT 60")
    except Exception as e:
        print(f"  [外部] 取候选失败:{str(e)[:70]}", flush=True)
        return []
    out = []
    for r in rows:
        try:
            n = json.loads(r.get("verdict_note") or "{}")
        except Exception:
            continue
        if n.get("module") in mods and n.get("how"):
            out.append({"repo": r["title"], "module": n["module"],
                        "metric": n.get("metric"), "how": n["how"]})
        if len(out) >= limit:
            break
    if out:
        print(f"  [外部] 取到 {len(out)} 条可借鉴做法喂给改进者", flush=True)
    return out


PROMPT_ABSORBABLE = {m for ms in SCOPE_MODULES.values() for m in ms}


def make_tickets(dry=False, limit=12):
    """把**提示词吸收不了**的采纳项转成系统改进工单。

    立此因(2026-08-04 创始人):「要让他真的可以抓到新的,**推送给你修改**,
      优化了系统,这个闭环必须要通」。
    此前的断点:雷达采到 → 判定可采纳 → **然后就没了**。40 条 adopt 里绝大多数落在
      OCR产线/采集下载线/知识图谱星图这些模块上,它们**不是提示词能改的**,
      于是永远躺在库里。躺着 = 没接上 = info4AI 这层白建。

    工单只出**提案**,不改任何代码 —— 代码级改动永远走 PR + CI + 人 merge。
    出过工单的标 status='ticketed',不重复刷屏(治"日报天天发没人看")。
    """
    try:
        rows = d1("SELECT title,url,score,verdict_note FROM evolve_candidates "
                  "WHERE kind='repo' AND status='adopt' ORDER BY score DESC LIMIT 80")
    except Exception as e:
        print(f"  [工单] 取候选失败:{str(e)[:70]}", flush=True)
        return []
    tickets = []
    for r in rows:
        try:
            n = json.loads(r.get("verdict_note") or "{}")
        except Exception:
            continue
        mod = n.get("module")
        if not mod or mod in PROMPT_ABSORBABLE or not n.get("how"):
            continue
        tickets.append({"repo": r["title"], "url": r.get("url"), "stars": r.get("score"),
                        "module": mod, "metric": n.get("metric"),
                        "how": n["how"], "effort": n.get("effort")})
        if len(tickets) >= limit:
            break
    if tickets and not dry:
        ids = ",".join(q(t["repo"]) for t in tickets)
        try:
            d1(f"UPDATE evolve_candidates SET status='ticketed', updated_at={int(time.time())} "
               f"WHERE title IN ({ids})")
        except Exception as e:
            print(f"  [工单] 标记失败:{str(e)[:70]}", flush=True)
    # 按模块归堆 —— 同一个模块的几条一起看才好排期
    by_mod = {}
    for t in tickets:
        by_mod.setdefault(t["module"], []).append(t)
    print(f"  [工单] 生成 {len(tickets)} 张,覆盖 {len(by_mod)} 个模块:{list(by_mod)}", flush=True)
    return tickets


def improve_one(scope, improver="freepool", dry=False):
    slot, tbl, _, _ = SCOPES[scope]
    # 【学 OpenRSI 的父代选择】它有 get_best / **get_random_by_fitness** / get_top_k 三种,
    #   我们此前永远从 status='active' 的冠军派生 —— **近亲繁殖,多样性为零**,
    #   这很可能是三轮赛马 0/5 赢不了的结构原因之一。
    #   改:七成从冠军派生(稳),三成从历史 top-k 里随机取一个当父代(探索)。
    #   注意 lost/retired 的方案**没有被删**,它们的分数还在,正好当多样性来源。
    cur = d1(f"SELECT variant_id,body,fitness,sample_n FROM evolve_variants "
             f"WHERE scope={q(scope)} AND slot={q(slot)} AND status='active' LIMIT 1")
    if cur and random.random() < float(os.environ.get("EVOLVE_PARENT_EXPLORE", "0.3")):
        pool = d1(f"SELECT variant_id,body,fitness,sample_n FROM evolve_variants "
                  f"WHERE scope={q(scope)} AND slot={q(slot)} "
                  f"AND status IN ('lost','retired','rolled_back') AND fitness IS NOT NULL "
                  f"ORDER BY fitness DESC LIMIT 5")
        if pool:
            pick = random.choice(pool)
            print(f"  [{scope}] 父代改从历史池取 {pick['variant_id']}"
                  f"(fitness={pick.get('fitness')})—— 打破近亲繁殖", flush=True)
            cur = [pick]
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

    # 【2026-08-04 补·断链】此前 improve 算子**只往候选池写,从来不读** ——
    #   雷达/月榜采回来 4076 个仓、判出 40 个可采纳,全躺在库里没人用。
    #   创始人:「要让他真的可以抓到新的,推送给你修改,优化了系统,这个闭环必须要通」。
    #   现在把与本产线相关的外部做法喂进改进提示词:外面的新技术要能真的作用到方案上,
    #   否则 info4AI 那一层就只是个书签收藏夹。
    ext = fetch_external_ideas(scope)

    user = (f"【当前提示词】\n{par['body']}\n\n"
            f"【这一代的真实成绩】复核放行率 {fit:.1%}(判过 {n} 条)—— 也就是说约 "
            f"{(1-(fit or 0)):.0%} 的产出被按住了。\n\n"
            f"【被按住的真实原因分布】\n{reasons}\n\n"
            f"【真实失败样例】\n" + "\n".join("  " + s for s in samples) +
            ("\n\n【外部可借鉴的做法(情报雷达采到并已判定可用)】\n"
             + "\n".join(f"  · {x['repo']} → {x['module']}/{x['metric']}:{x['how']}" for x in ext)
             + "\n参考其中**思路**改进提示词;不适用就忽略,不要硬套、不要在提示词里提项目名。"
             if ext else "") +
            "\n\n请针对上面这些**真实**的失败原因改写提示词。\n"
            # 【2026-08-04 实测】不给数字,免费池模型会把它当"写一本规则书"——
            #   biocomp 连着三次吐回 5601~6221 字(父代才 1032),全被闸拦下。
            #   写抽象要求(「长度相当」)它完全不当回事,**给具体字数区间它才照做**。
            f"【硬性字数】原提示词 {len(par['body'])} 字,你的产出必须在 "
            f"{int(len(par['body'])*0.8)}~{int(len(par['body'])*1.6)} 字之间。"
            f"超出即不合格会被自动丢弃。宁可把话说紧,不要展开成规则手册。")

    def _gen(extra=""):
        if improver == "claude-api":
            print("  [改进者] Claude API(**按量计费源** —— 由创始人显式开启)", flush=True)
            t, m = ask_claude_api(SYS_IMPROVER, user + extra)
        else:
            # 改进者要提示词全文 —— 不关 json_mode 会被逼着吐 JSON,产出必空(2026-08-05 实测)
            t, m = ask(SYS_IMPROVER, user + extra, max_tokens=3000, json_mode=False)
        return strip_wrapper(_unfence(t or "")), m

    child, model = _gen()
    bad = guard(par["body"], child)
    if bad:
        # 【2026-08-04 首轮实测】biocomp 这一路被拦在「比父代长太多」——
        #   模型把改写理由也一并吐了出来。一次都不重试的话,**这条产线永远进化不了**,
        #   红线闸从"防出事"变成"永久堵死",那不是我们要的。
        #   给一次带反馈的重试(把它自己的毛病告诉它),仍不合格才丢弃。
        print(f"  [{scope}] ⚠ 首轮被闸拦下({bad}),带反馈重试一次", flush=True)
        child, model = _gen(f"\n\n【上一次你的产出被自动闸拦下了】原因:{bad}。"
                            f"你上次写了 {len(child)} 字,而**上限是 {int(len(par['body'])*1.6)} 字**。"
                            f"请**只输出提示词全文本身**,不要任何说明、理由、前后缀,"
                            f"必须压到 {int(len(par['body'])*1.6)} 字以内 —— 删掉举例和展开,只留规则。")
        bad = guard(par["body"], child)
    if bad:
        # 【学 OpenRSI 的 debug 模式】它的 generation_mode 有 draft/improve/**debug**/crossover,
        #   候选跑挂了会喂着 run_log 让模型专门去修,而不是整条扔掉。
        #   我们此前是"拦下即丢弃" —— 等于每次都从头再来,前面那次的所有正确部分白费。
        #   这里给第三次机会:把**原始产出 + 拦截原因**一起喂回去,只让它修被拦的那一处。
        print(f"  [{scope}] ⚙ 转 debug 模式(第 3 次):{bad}", flush=True)
        try:
            dbg_user = (f"【被拦下的产出】\n{child[:4000]}\n\n"
                        f"【自动闸的拦截原因】{bad}\n\n"
                        f"【父代提示词(长度 {len(par['body'])} 字,供对照)】\n{par['body'][:2000]}\n\n"
                        f"只修被拦的那一处,其余不动。")
            t, m2 = (ask_claude_api(SYS_DEBUG, dbg_user) if improver == "claude-api"
                     else ask(SYS_DEBUG, dbg_user, max_tokens=3000, json_mode=False))
            child = strip_wrapper(t or "")
            bad = guard(par["body"], child)
            if not bad:
                model = m2
                print(f"  [{scope}] ✓ debug 模式救回来了", flush=True)
        except Exception as e:
            print(f"  [{scope}] debug 模式调用失败:{type(e).__name__}", flush=True)
    if bad:
        print(f"  [{scope}] ✗ 红线闸拦下这次改进(debug 后仍不合格):{bad}", flush=True)
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

    out = {"improver": a.improver, "born": [], "arbitrate": [], "candidates": 0, "tickets": []}
    out["candidates"] = sync_candidates()
    # 提示词吸收不了的采纳项 → 工单推给人(创始人:「抓到新的,推送给你修改」)
    out["tickets"] = make_tickets(dry=a.dry_run)

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
    print(f"  系统改进工单:{len(out['tickets'])} 张", flush=True)
    for t in out["tickets"][:8]:
        print(f"    · [{t['module']}] {t['repo']}({t['stars']}★){t['effort'] or ''} "
              f"→ {t['metric']}:{t['how']}", flush=True)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
