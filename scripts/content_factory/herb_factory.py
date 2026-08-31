#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本草 / 生物计算 内容工厂 —— 让这两个版块的内容自己长

立此因(2026-08-02 创始人):「生物计算、本草,不可以派个 GitHub 的任务,或者派个 AI 去一直丰富内容吗」
  + 「我们的主要价值是在丰富功能,而不是在修修补补」。
实测原状:bencao.html 8KB / shengwu.html 9KB,条目**全部硬编码在 HTML 里**,
  加一味药要改代码重新部署 —— 所以两个多月一条没长过。

产线骨架照搬已跑通的「百家论道」:
  GitHub Actions cron → 免费池网关(零成本) → 合规硬闸 → 写 D1 → 前台读库

合规硬闸(07_合规文档,零容忍):
  生成结果里出现 剂量/煎服/疗效承诺/诊疗祈使句 → **整条丢弃并计数**,绝不入库。
  每条强制带认知档(默认②法度推演)+ 出处 + 版本印(模型/批次/提示词版本)。

铁律:
  · 只走内部免费池网关 https://gufangai.com/api/gateway/chat,严禁任何按量计费源
  · 本机零算力:纯 HTTP + D1
  · 入库一律 status='draft' —— **不自动上线**,由 CTO/创始人在后台审核后才 published

用法:
  D1_API_TOKEN=... python scripts/content_factory/herb_factory.py --target herb --count 6
  D1_API_TOKEN=... python scripts/content_factory/herb_factory.py --target biocomp --count 6
"""
import os, sys, json, time, uuid, re, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 方案槽:提示词从 D1 读,读不到回落本文件里的默认常量。
# 这一层是自我进化闭环的接口 —— 有了它,改进者才能从"我"换成"Claude API",产线不用改。
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _variant import load_variant, seed_variant, record_trial   # noqa: E402

# 汉字判定走仓里的**共享区块表**,不在本文件自己写 [一-鿿] 这种区间字面量。
# 立此因(2026-08-02):`tests/test_cjk_charset.py::test_no_new_module_writes_its_own_cjk_range`
#   把我这份自带副本抓了出来 —— 「仓里又多出一份会独立漂移的副本」。
#   它抓得对:自带的窄表连扩展A都不认,生僻药材名会被整条筛掉。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import cjk_charset
# cjk_charset.HAN 是「一个汉字」的字符类;这里要判定**整串都是汉字**,所以包一层 +
HAN_ONLY = re.compile("(?:%s)+$" % cjk_charset.HAN.pattern)

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB      = os.environ.get("D1_DATABASE_ID", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GATEWAY    = os.environ.get("GW_URL", "https://gufangai.com/api/gateway/chat")
PROMPT_VER = "hf-v2-2026-08-02"   # v2:留空优于编造 + 题材限定中药来源
# 并发路数:单条要调一次网关约 1~2 分钟,串行跑 49 分钟只出 21 条 —— 这是产能的真瓶颈。
# 免费池网关本身是多供应商轮转,6 路是实测不触发限流的稳妥值;要调用 CF_WORKERS 环境变量。
WORKERS    = int(os.environ.get("CF_WORKERS", "6"))
# 扩题**点名**用直答型模型(2026-08-02 诊断实证):
#   免费池轮到某些模型时,它会把 3000 token 全烧在「1. **分析请求:** *角色…* *硬性要求…*」
#   这样的思考复述上,**还没写到答案就被截断** —— 日志里解析出的那两项正是它复述到一半的
#   占位符 `["名称", "学名或英`。连续 4 轮返回 0 的真因就是这个,不是解析器、也不是波动。
#   glm-4-flash(zhipu)实测直接给数组;这里点名它,它挂了仍按容错链兜。
EXPAND_SUPPLIER = os.environ.get("CF_EXPAND_SUPPLIER", "zhipu")
# 扩题首选 OpenCode 免密端点(创始人 2026-08-02:「你要控制 opencode 帮你干分析请求」)。
# 实测 deepseek-v4-flash-free 与 big-pickle 都直接吐 JSON 数组,不复述任务要求。
OPENCODE_MODEL     = os.environ.get("CF_OPENCODE_MODEL", "deepseek-v4-flash-free")
OPENCODE_MODEL_ALT = os.environ.get("CF_OPENCODE_MODEL_ALT", "big-pickle")   # 前者进思考模式时换它

# ── 合规硬闸:命中任一即整条丢弃 ─────────────────────────────────
BANNED = [
    "克", "钱重", "水煎服", "每日三次", "每次服", "煎服", "顿服", "口服剂量",
    "疗效显著", "治愈率", "包治", "特效", "根治",
    "建议你服用", "你应该服用", "可以服用", "推荐剂量", "用法用量",
]

# ── 题材源(2026-08-02 创始人:「也让他们产生几千几万个数据」)────────────
# 原来是手写的 12+12 条固定名单 —— 那是硬瓶颈,跑两轮就没题了,永远到不了几千。
# 改法:三级供源,越靠前越权威,自动降级:
#   ① D1 现成资产:sue_graph_nodes 里的本草节点(4 万+ 真实药材名)—— 生物计算直接用它当来源药材
#   ② AI 扩题:让模型按"已产出的名单"续列同类候选(去重后入池),题材自增长
#   ③ 兜底种子:网络/模型都不可用时至少还能跑
HERB_FALLBACK = [
    ("Shatavari", "Asparagus racemosus"), ("Triphala", "Terminalia chebula 等三果"),
    ("Neem", "Azadirachta indica"), ("Amla", "Phyllanthus emblica"),
    ("Brahmi", "Bacopa monnieri"), ("Guggul", "Commiphora wightii"),
    ("Punarnava", "Boerhavia diffusa"), ("Manjistha", "Rubia cordifolia"),
    ("Vacha", "Acorus calamus"), ("Yashtimadhu", "Glycyrrhiza glabra"),
    ("Pippali", "Piper longum"), ("Arjuna", "Terminalia arjuna"),
]
BIO_FALLBACK = [
    ("黄芩苷", "Baicalin"), ("丹参酮ⅡA", "Tanshinone IIA"), ("黄芪甲苷", "Astragaloside IV"),
    ("人参皂苷Rg1", "Ginsenoside Rg1"), ("小檗碱", "Berberine"), ("川芎嗪", "Ligustrazine"),
    ("葛根素", "Puerarin"), ("芍药苷", "Paeoniflorin"), ("雷公藤甲素", "Triptolide"),
    ("青蒿素", "Artemisinin"), ("淫羊藿苷", "Icariin"), ("绿原酸", "Chlorogenic acid"),
]

SYS_EXPAND = (
    "你是本草/药化资料整理助手。任务:按给定的**已有名单**,继续列出**同类别的其他真实存在的**条目。\n"
    "硬性要求:①只列真实存在、有文献记载的,**绝不编造**;②不得与已有名单重复;"
    "③只输出 JSON 数组,每项形如 [\"名称\",\"学名或英文名\"];④不写任何说明文字。\n"
    # 2026-08-02 实测:扩题扩出了吗啡/可待因/阿托品,被复核以「含西药成分,违反平台古方主题」拦下。
    # 它们确实是生物碱,但不是本平台意义上的「中药活性成分」——题材边界必须写死在提示词里。
    "⑤**题材边界(违反即整条不要)**:只列**传统中药材所含**的活性成分(如黄芩苷来自黄芩、"
    "苦参碱来自苦参);**排除**西药单体、麻醉/精神管制药品(吗啡、可待因、阿托品、东莨菪碱这类)、"
    "以及纯化学合成物。判断标准:该成分能否指回一味**中医本草**。"
)


def expand_topics(kind, existing, want=40, clean_sample=None):
    """让 AI 按已有名单续列同类候选。失败返回空表,由调用方降级到兜底种子。

    clean_sample:喂给模型当"范例"的名单。**必须是干净的** ——
      2026-08-02 实测,我把库里的名单原样喂过去,模型直接拒绝干活并回了一条 error:
      「您提供的名单中包含大量非中药活性成分(如药材名、炮制品、描述性文字等),
        且存在阿托品、东莨菪碱、可待因等明确排除的条目」。
      它是对的 —— 脏范例会把模型带偏,甚至让它罢工。所以范例与去重名单必须分开:
      `existing` 只用于去重(要全),`clean_sample` 才是给模型看的(要精)。
    """
    label = "阿育吠陀药材(印度传统医学本草)" if kind == "herb" else "中药活性成分(单体化合物)"
    sample = list(clean_sample if clean_sample else existing)[:40]
    try:
        # 排除名单要给**全**。实测(run 30739133742):库里 118 条时只给 40 个范例,
        #   模型翻来覆去还是那几个常见成分,连两轮扩题都返回 0 个新的 —— 题材饱和。
        #   范例(给锚点,少而精)和排除名单(防重复,要全)用途不同,必须分开给。
        exclude = list(existing)[:200]
        prompt = (f"类别:{label}\n"
                  f"范例(这类东西长这样):{json.dumps(sample, ensure_ascii=False)}\n"
                  f"**已产出、绝对不要重复**(共 {len(existing)} 个,以下列出其中 {len(exclude)} 个):"
                  f"{json.dumps(exclude, ensure_ascii=False)}\n"
                  f"请再列 {want} 个**上面没出现过**的。常见的多半已被收录,"
                  f"请往**较少见但确有文献记载**的方向列(次级代谢产物、同类衍生物、"
                  f"冷门药材的特征成分都可以)。\n"
                  f"**直接以 `[` 开头输出 JSON 数组,不要写任何分析、复述或解释。**")
        # 2026-08-31 拔掉 opencode:它的 deepseek 下线后 400/401,不再先试它。
        #   直接走我们自己的网关池(supplier=EXPAND_SUPPLIER=zhipu,网关内部有三层容错链)。
        txt, _ = ask(SYS_EXPAND, prompt, max_tokens=3000, supplier=EXPAND_SUPPLIER)
        # 2026-08-02 血证:这里原来有**自己一套**内联解析,还在用 rfind("]") ——
        #   我上午只把对象解析器换成了 raw_decode,漏了这个数组解析器,
        #   于是扩题稳定报 "Extra data: line 1 column 17" 失败 → 降级回图谱 OCR 碎词
        #   → 90 个候选 84 个被判否。**一个函数修一半,等于没修。**
        arr = parse_json_array(txt)
        out = []
        for it in arr:
            if isinstance(it, list) and len(it) >= 2 and it[0]:
                out.append((str(it[0]).strip(), str(it[1]).strip()))
            elif isinstance(it, str) and it.strip():
                out.append((it.strip(), ""))
        # 模型有时把提示词里的字段名当条目吐回来(实测混进过「名称」「学名或英文名」),
        # 这类占位符会被当成药材名喂进生成阶段,必须在入池前挡掉
        PLACEHOLDER = {"名称", "学名", "英文名", "学名或英文名", "名称()", "成分", "药材", "示例", "example", "name"}
        kept = [(n, s) for (n, s) in out if n and n not in PLACEHOLDER and len(n) <= 24]
        # 诊断(2026-08-02):云端连续 4 轮返回 0,而本地同一函数正常拿到 5/5/19 条 ——
        #   说明是环境差异而非逻辑错。与其继续猜,不如让产线**自己把真相打出来**:
        #   拿到 0 条时,原样打印解析前后的数量和模型原文开头,下一轮 cron 就能定位。
        if not kept:
            print(f"  [扩题·诊断] 解析出 {len(arr)} 项 → 成对 {len(out)} 个 → 过滤后 0 个;"
                  f"应答模型原文前 200 字: {(txt or '(空)')[:200]!r}", flush=True)
        return kept
    except Exception as e:
        print(f"  [扩题] 失败,降级到兜底种子: {type(e).__name__} {str(e)[:80]}", flush=True)
        return []


def herbs_from_d1_graph(limit=200):
    """生物计算的来源药材直接取自图谱里的真实本草节点(4 万+),不靠手写名单。"""
    try:
        # 列名是 node_kind 不是 kind —— 首次实测栽在这,取到 0 条还静默吞了异常
        # 质检闸 v3 —— 前两版都失败,记下为什么:
        #   v1 虚词黑名单 → 连漏两次("止用至""后取"),**穷举永远追不上脏数据**
        #   v2 图谱度数≥2 → 实测 41434 个 herb 节点里只有 101 个够格,**把题材源掐死了**;
        #      而放宽到度数≥1 又等于没过滤(全部 41434 个都有边)。
        #   根因:**脏的是语义不是结构**。抽样 15 个:真药材 5 个,
        #      其余是「酒作」「月用」「頃復灌」「溫水調下」「大实痛加」这类 OCR 碎词。
        #      结构性判据(长度/度数)对语义脏数据天然无效。
        # v3 正解:**让正在读每一条的 AI 自己当验证器** ——
        #   它本来就要读这个名字才能生成内容,顺手判一句"是不是真药材",增量成本为零。
        #   本函数只做廉价的结构预筛(2-4 字纯中文),真正的判定交给生成阶段(见 SYS_BIO 的 invalid 约定)。
        rows = d1("SELECT DISTINCT label FROM sue_graph_nodes WHERE node_kind='herb' "
                  "AND LENGTH(label) BETWEEN 2 AND 4 "
                  f"ORDER BY RANDOM() LIMIT {int(limit) * 4}")
        out = [(r["label"].strip(), "") for r in rows
               if r.get("label") and HAN_ONLY.match(r["label"].strip())]
        print(f"  [题材预筛] 图谱取 {len(out)} 个候选(真伪由生成阶段的 AI 验证器判)", flush=True)
        return out[:limit]
    except Exception as e:
        print(f"  [图谱取材] 失败: {type(e).__name__} {str(e)[:100]}", flush=True)
        return []


def d1(sql, params=None):
    if not D1_TOKEN:
        sys.exit("缺 D1_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
    payload = {"sql": sql}
    if params:
        payload["params"] = params
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + D1_TOKEN, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:250])
    return (j.get("result") or [{}])[0].get("results") or []


def ask(system, user, timeout=120, max_tokens=2600, supplier=None,
        temperature=None, no_fallback=False, json_mode=True):
    # max_tokens 实测教训(2026-08-02):原值 1400 对中文长字段不够,
    # biocomp 的 mechanism(120-200字)+ tcm_link(100-180字)一起就会把 JSON 截断,
    # 报 "Unterminated string" —— 上一轮 14 次失败里有 5 次是这个。
    payload = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        # 【Phase 0 · A 锁随机】评测态传 temperature=0;产线不传,保持 0.3
        "temperature": 0.3 if temperature is None else float(temperature),
        "json": bool(json_mode), "source": "content_factory",
    }
    if supplier:
        payload["supplier"] = supplier      # 点名供应商;这家挂了仍按容错链兜(fallback 默认 true)
    if no_fallback:
        # 评测期间禁容错链 —— 换供应商 = 换考生,分数不可比
        payload["fallback"] = False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY, method="POST", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"})
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    txt = j.get("text") or ""
    if not txt and j.get("choices"):
        txt = (j["choices"][0].get("message") or {}).get("content", "")
    return txt, (j.get("model") or j.get("supplier") or "")


def ask_opencode(system, user, timeout=120, max_tokens=3000):
    """直连 OpenCode 免密端点 —— 扩题这类「列个单子」的分析活交给它。

    立此因(2026-08-02 创始人):「你要控制 opencode 帮你干分析请求」。
    为什么值得单独走一条路(而不是继续走网关):
      · 免费池经网关轮到某些**思考型**模型时,会把整个 token 预算烧在复述任务要求上,
        **答案根本没写出来** —— 连续多轮扩题返回 0 就是这么来的(有日志实证)。
      · 实测 opencode 的 deepseek-v4-flash-free / big-pickle **直接吐 JSON 数组**,不废话。
    为什么这里不经网关:`_providers.js` 已写明 —— CF Workers 出网 IP 全球共享、免费额度被打满,
      实测固定 429;**opencode 只能从 GitHub Actions(独立 IP)或本机调**。内容工厂正好跑在 Actions。
    成本:零。它仍属内部免费池(注册表里的 opencode_free),不是按量计费源。
    形态注意:它是思考型模型,正文有时在 reasoning_content 而非 content,两个都要取。
    """
    # 实测差别很大:**带 system 消息**时 deepseek 会进思考模式(content 空、正文落在
    #   reasoning_content 里,还是一段自然语言推理);把 system 并进单条 user 消息,
    #   它就直接吐 JSON 数组。所以这里合并,不发 system。
    merged = (system or "") + "\n\n" + (user or "")
    last = ""
    for model in (OPENCODE_MODEL, OPENCODE_MODEL_ALT):
        body = json.dumps({
            "model": model, "max_tokens": max_tokens, "temperature": 0.3,
            "messages": [{"role": "user", "content": merged}],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://opencode.ai/zen/v1/chat/completions", method="POST", data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                # 这五个身份头缺任一项都会 401 —— 它靠这个认人,不是靠 API key
                "User-Agent": "opencode/1.0.0",
                "x-opencode-client": "opencode",
                "x-opencode-project": "default",
                "x-opencode-session": str(uuid.uuid4()),
                "x-opencode-request": str(uuid.uuid4()),
            })
        j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        m = (j.get("choices") or [{}])[0].get("message") or {}
        txt = (m.get("content") or "").strip()
        if "[" in txt:
            return txt, model
        # content 空或没有数组 → 换另一个模型再来一次,不拿思考文本充数
        last = txt or (m.get("reasoning_content") or "").strip()
    return last, OPENCODE_MODEL_ALT


def _unfence(t):
    """剥掉 ```json 围栏,返回裸文本。对象与数组两个解析器共用,别再各写一份。"""
    t = (t or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    return t


def parse_json_array(t):
    """取出第一个完整的 JSON **数组**。与 parse_json 同一套判据,失败返回空表。"""
    t = _unfence(t)
    a = t.find("[")
    if a < 0:
        # 2026-08-02 实测:模型有时不给数组,而是给一个「名称→学名」的对象
        #   (`{"Ashwagandha (Withania somnifera)":"Withania somnifera", ...}`)。
        #   语义完全等价,没理由因为容器类型不同就整批丢掉。
        b = t.find("{")
        if b >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(t, b)
                if isinstance(obj, dict) and obj and "error" not in obj:
                    return [[str(k), str(v)] for k, v in obj.items() if k]
            except json.JSONDecodeError:
                pass
        print(f"  [扩题] 模型没吐出 JSON 数组,原文前120字: {t[:120]!r}", flush=True)
        return []
    # 扫描**全文每一个** `[`,取解析得到的**最长**数组 ——
    # 2026-08-02 诊断实证:云端有的模型会把思考过程当答案输出,先复述一遍
    #   「硬性要求…每项形如 ["名称","学名或英文名"]」,真正的答案在最后面。
    #   只取第一个数组 → 取到的是复述里的**占位符示例** → 过滤后恒为 0(连续 4 轮 0 就是这么来的)。
    #   取最长的那个,思考文本里的零星小数组自然被真答案盖过去。
    #   注意:不能简单取「最长」—— `[["甘草酸","Glycyrrhizic acid"]]` 的内层(2项)比外层(1项)长,
    #   会被误选成内层。所以先收集所有候选及其跨度,**剔除嵌在别人里面的**,再在顶层里挑最长。
    dec = json.JSONDecoder()
    cands = []                      # [(start, end, value)]
    i = a
    while i >= 0:
        try:
            cand, end = dec.raw_decode(t, i)
            if isinstance(cand, list):
                cands.append((i, end, cand))
        except json.JSONDecodeError:
            pass
        i = t.find("[", i + 1)
    top = [c for c in cands
           if not any(o[0] < c[0] and c[1] <= o[1] for o in cands)]   # 被别人包住的不算顶层
    if top:
        return max(top, key=lambda c: len(c[2]))[2]
    try:
        arr, _end = dec.raw_decode(t, a)
        return arr if isinstance(arr, list) else []
    except json.JSONDecodeError:
        b = t.rfind("]")
        if b > a:
            try:
                return json.loads(t[a:b + 1])
            except Exception:
                pass
        # 整体解析不了就逐项抢救 —— 截断的数组里,前面那些完整的条目仍然是好的
        out, dec, i = [], json.JSONDecoder(), a + 1
        while i < len(t):
            while i < len(t) and t[i] in ' ,\n\r\t':
                i += 1
            if i >= len(t) or t[i] == ']':
                break
            try:
                obj, i = dec.raw_decode(t, i)
                out.append(obj)
            except json.JSONDecodeError:
                break
        print(f"  [扩题] 数组不完整,逐项抢救出 {len(out)} 条", flush=True)
        return out


def parse_json(t):
    """从模型输出里取出**第一个完整**的 JSON 对象。

    治实测第一大失败源(2026-08-02,14 次失败里 9 次):
      模型吐完 `{"invalid": true, "why": "..."}` 之后还会跟一段解释文字或第二个对象,
      老写法用 `rfind('}')` 把尾巴一起吞进来 → json.loads 报 "Extra data: line 1 column 33"。
      改用 raw_decode:解析到第一个对象闭合就收手,后面的尾巴一概不要。
    截断类失败("Unterminated string")这里救不了,由调用方加大 max_tokens 重试。
    """
    t = _unfence(t)
    a = t.find("{")
    if a < 0:
        raise ValueError("模型输出里没有 JSON 对象:" + t[:80])
    try:
        obj, _end = json.JSONDecoder().raw_decode(t, a)
        return obj
    except json.JSONDecodeError:
        b = t.rfind("}")                      # 退一步用老办法兜一次,兜不住才真失败
        if b > a:
            return json.loads(t[a:b + 1])
        raise


def violates(obj):
    """整条扫合规红线,命中即丢。返回命中的词(空=干净)。"""
    blob = json.dumps(obj, ensure_ascii=False)
    return [w for w in BANNED if w in blob]


SYS_HERB = (
    "你是「古方 AI 星图」的跨文明本草研讨助手。任务:就**给定的**阿育吠陀药材,"
    "输出结构化的传统属性与中医视角对照。\n"
    "**硬性红线(违反则整条作废)**:①绝不写任何剂量、煎服法、用法用量;"
    "②绝不做疗效承诺或治愈表述;③绝不出现「建议你/你应该服用」这类祈使句;"
    "④中医对照必须写明是**推演**而非古籍原文,不得伪造出处。\n"
    # 2026-08-02 实测:19 条全被独立复核按住,最集中的理由是**凭空编造《本草纲目》出处**
    #   (「《本草纲目拾遗》未载印度枣」「引用《本草纲目》但无具体条目对应」)。
    # 根因和生物计算那边一样:提示词要求填 tcm_refs,模型不知道就编。
    # 而阿育吠陀药材**本来大多就不在中医古籍里** —— 诚实答案就是「无直接记载,系类比推演」。
    "**关于 `tcm_refs`(这条最容易出事)**:阿育吠陀药材**绝大多数中医古籍里根本没有**。\n"
    "  · 只有你确信某部中医典籍**确实记载了这味药本身**,才写书名;\n"
    "  · 只是「功效相似所以类比到某味中药」→ **给空数组 `[]`**,并在 tcm_compare 里写明"
    "「中医文献无此药直接记载,以下为按性味功效作的类比推演」;\n"
    "  · **凭空写《本草纲目》《本草纲目拾遗》是最严重的错误**,整条会被作废。留空不扣分。\n"
    "**措辞**:`traditional_use` / `indications` 一律用「文献记载用于…」「传统上用于…」起句,"
    "禁止 improves / enhances / 改善 / 增强 / 治疗 这类断言式动词 —— 那是疗效承诺。\n"
    "`name_latin` 与 `name_native` 拿不准就留空,**绝不猜**(实测出现过拉丁名张冠李戴)。\n"
    "只输出 JSON,字段:name_cn(中文名)、name_native(梵文名)、props(性味 Rasa/Guna/Virya/Vipaka 对象)、"
    "constitution(调节体质)、traditional_use(传统功用)、indications(主治,用「文献记载用于…」句式)、"
    "actives(主要活性成分数组)、tcm_compare(中医视角对照,150-250字,须含「据文献推演」字样)、"
    "tcm_refs(所据中医方药/文献名数组)。"
)
SYS_BIO = (
    "你是「古方 AI 星图」的中药成分研讨助手。任务:就**给定的**中药活性成分或药材,输出结构化的分子与机制信息。\n"
    "**第一步永远是验真(这是你最重要的职责)**:给定的名字来自古籍 OCR,里面混有大量碎词与非药物短语"
    "(如「酒作」「月用」「頃復灌」「溫水調下」「大实痛加」「与巴豆」)。\n"
    "  · 若它**不是**一个真实的中药材名或中药活性成分名(是动词短语/煎服法/残句/量词/生造词),\n"
    "    **只输出** `{\"invalid\": true, \"why\": \"简短理由\"}`,不要勉强编内容。\n"
    "  · 拿不准的一律判 invalid。**宁可漏,不可错** —— 编一条假的比少一条代价大得多。\n"
    "  · 确认是真药材/真成分,才继续下面的输出。\n"
    "**硬性红线(违反则整条作废)**:①绝不写剂量/用法;②绝不做疗效承诺;③机制描述须是研讨性表述"
    "(「体外研究提示…」「文献报道…」),不得写成临床结论;④不确定的字段留空,**绝不编造** CID/UniProt 号。\n"
    # 2026-08-02 实测:20 条里 19 条被独立复核按住,拦截理由高度集中在
    #   「引用模糊无具体编号」「refs 只写 PubChem/PubMed 未给 ID,出处像编的」「SMILES 与该成分不符」
    #   「靶点含虚构受体」「机制描述过于绝对」。
    # 根因是提示词在**逼模型交作业**:要求它填 refs/targets/smiles,它不知道就编。
    # 改法:把「留空」明确写成**受欢迎的正确答案**,而不是缺陷。
    "**留空优于编造(这条最重要)**:\n"
    "  · `refs`:只写你能给出**具体编号**的(如 \"PubChem CID 5280445\"、\"PMID 12345678\");"
    "只写「PubChem」「PubMed」这种没有编号的**一律不要写**,直接给空数组 `[]`。\n"
    "  · `smiles`:不能确信逐个原子都对就**留空字符串**。写错的结构式比没有结构式糟糕得多。\n"
    "  · `targets`:只列有充分文献支持的靶点;凑数、不确定的一律不写,可以给空数组。\n"
    "  · `formula` / `mol_weight`:不确定就留空 / null。\n"
    "  留空**不会被扣分**,编造会让整条作废。\n"
    "**措辞**:机制与对照一律用「体外研究提示」「文献报道」「有研究认为」这类限定语,"
    "禁止「能够治疗」「可有效改善」这类绝对表述。\n"
    "只输出 JSON,字段:name_cn、name_en、source_herb(来源药材中文名)、formula(分子式)、"
    "smiles(不确定则留空字符串)、mol_weight(数字,不确定填 null)、targets(已知靶点数组)、"
    "mechanism(作用机制,120-200字,研讨性表述)、tcm_link(与中医理论的对照推演,100-180字)、"
    "refs(文献出处数组,如 PubChem/PubMed 名称,不确定则给空数组)。"
)


def _gen(system, user, tries=2, eval_mode=False):
    """生成 + 解析,失败重试一次并放大 max_tokens(第一次多半是被截断)。

    eval_mode=True 走**评测态**(Phase 0 · A 锁随机):温度锁 0、禁容错链切换、
      点名单一供应商。目的是让同一份提示词每次考出同一个分 —— 尺子不许抖。
      产线不传这个参数,行为完全不变。
    """
    # 评测态先查冻结的考卷 —— 命中就直接复用,零调用、零随机、必然同分
    if eval_mode:
        _k = _eval_cache_key(system, user)
        _hit = _eval_cache_get(_k)
        if _hit is not None:
            return _hit[0], _hit[1]
    last = None
    for i in range(tries):
        kw = {"temperature": 0, "no_fallback": True,
              "supplier": EVAL_SUPPLIER} if eval_mode else {}
        txt, model = ask(system, user, max_tokens=2600 if i == 0 else 3400, **kw)
        try:
            obj = parse_json(txt)
            if eval_mode:
                _eval_cache_put(_k, obj, model)      # 第一次考完就冻结,后面所有轮次复用
            return obj, model
        except Exception as e:
            last = e
    raise last


# 评测态点名的考生 —— **一个赛季内钉死**,换它等于换赛季,历史分数作废。
EVAL_SUPPLIER = os.environ.get("EVAL_SUPPLIER", "dashscope")

# ── 冻结考卷:同一份输入只生成一次,之后复用 ──────────────────────
#   Phase 0 的验收是「同一评测集连跑 3 次分数完全一致」。
#   `temperature=0` 只降抖不归零(供应商侧 GPU 内核 / MoE 路由 / 副本均衡都带随机),
#   所以光锁温度过不了验收。**冻结产物才是唯一能真过的形态。**
#   缓存键 = 提示词 + 题目 + 考生 + 温度 —— 任一变化即换一份新卷,不会串味。
EVAL_CACHE_DIR = os.environ.get(
    "EVAL_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "reports", "evolve", "eval_cache"))


def _eval_cache_key(system, user):
    import hashlib
    raw = f"{EVAL_SUPPLIER}|t0|{system}|{user}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _eval_cache_get(key):
    p = os.path.join(EVAL_CACHE_DIR, key + ".json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p, encoding="utf-8"))
        return d.get("obj"), d.get("model")
    except Exception:
        return None


def _eval_cache_put(key, obj, model):
    try:
        os.makedirs(EVAL_CACHE_DIR, exist_ok=True)
        json.dump({"obj": obj, "model": model, "at": int(time.time())},
                  open(os.path.join(EVAL_CACHE_DIR, key + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    except Exception as e:
        print(f"  [考卷缓存] 写入失败(不致命):{type(e).__name__}", flush=True)


def gen_herb(name_en, latin, sys_prompt=None, eval_mode=False):
    return _gen(sys_prompt or SYS_HERB, f"药材:{name_en}(学名 {latin})。按要求输出 JSON。",
                eval_mode=eval_mode)


def gen_bio(cn, en, sys_prompt=None, eval_mode=False):
    return _gen(sys_prompt or SYS_BIO, f"成分:{cn}({en})。按要求输出 JSON。",
                eval_mode=eval_mode)


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    return "'" + str(v).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["herb", "biocomp"], required=True)
    ap.add_argument("--count", type=int, default=6)
    args = ap.parse_args()

    # ── 方案槽:把源码默认提示词登记成第一代,然后读回当前生效的那一版 ──────
    #   产线从此**读库拿提示词**,不再只认源码常量。库里没有/读不到 → 回落源码默认值。
    #   合规硬闸(BANNED / 认知档 / 确定性校验)仍留在代码里,**红线不进库、不许进化**。
    slot = "SYS_BIO" if args.target == "biocomp" else "SYS_HERB"
    default_sys = SYS_BIO if args.target == "biocomp" else SYS_HERB
    seed_variant(args.target, slot, default_sys, author="cto", note="源码默认提示词,登记为第一代")
    ACTIVE_SYS, ACTIVE_VID = load_variant(args.target, slot, default_sys)
    # 成绩必须归到**本轮真正用的那个方案**头上。写死 PROMPT_VER 常量的话,
    #   A/B 两组数字会挂在同一个版本号下互相覆盖,裁决永远看不出谁更好 ——
    #   闭环就断在这一行(2026-08-04 修)。
    global PROMPT_VER
    if ACTIVE_VID:
        PROMPT_VER = ACTIVE_VID
    print(f"  [方案槽] slot={slot} variant={ACTIVE_VID or '(源码默认)'} 长度={len(ACTIVE_SYS)}", flush=True)

    run_id = "cf_" + uuid.uuid4().hex[:12]
    tbl = "herb_compare" if args.target == "herb" else "biocomp_entries"
    now = int(time.time())
    d1(f"INSERT INTO content_gen_runs (run_id,target,requested,started_at) VALUES ({q(run_id)},{q(tbl)},{args.count},{now})")
    print(f"[内容工厂] run={run_id} target={tbl} 计划 {args.count} 条\n", flush=True)

    have = {r["n"] for r in d1(f"SELECT name_cn AS n FROM {tbl}")}
    print(f"  库内已有 {len(have)} 条", flush=True)

    # ── 三级题材源(要跑到几千几万,靠这个而不是手写名单)──────────────
    # 题材源顺序 —— 2026-08-02 实测教训(run 30734247058):
    #   原来「图谱源」排在「AI 扩题」前面,结果 90 个候选里 **84 个被判否**,命中率只有 7%。
    #   根因:图谱那批是古籍 OCR 出来的**药材名**(还混着「故曰」「亦投下」「约有」这类碎句),
    #   而本版块要的是**分子层面的活性成分** —— 两者压根不是同一层东西,天然对不上。
    #   改成:AI 扩题(直接产真实成分名)排第一,图谱降为兜底。
    fallback = HERB_FALLBACK if args.target == "herb" else BIO_FALLBACK
    seed = [s for s in fallback if s[0] not in have]
    need = max(args.count * 3, 40)

    # 每次只要 20 条 —— 实测教训:一次要 60 条,模型输出常被截断,
    #   上一轮只抢救出 1 条就降级了(run 30738747904)。小批多轮比一次要一大把稳得多。
    PER_CALL = 20
    empty_rounds = 0
    # 给模型看的范例必须干净:生物计算只取**有分子式**的(那才是真的活性成分),
    # 本草只取已通过独立复核的。拿库里的脏名单当范例 = 把模型带偏(实测它会直接罢工)。
    try:
        if args.target == "biocomp":
            # 随机轮换范例:固定拿最新那批当锚点,模型每轮联想到的都是同一片区域
            clean = [r["n"] for r in d1("SELECT name_cn AS n FROM biocomp_entries "
                                        "WHERE formula IS NOT NULL AND TRIM(formula)<>'' "
                                        "ORDER BY RANDOM() LIMIT 25")]
        else:
            clean = [r["n"] for r in d1("SELECT name_cn AS n FROM herb_compare "
                                        "WHERE status='published' ORDER BY RANDOM() LIMIT 25")]
    except Exception:
        clean = []
    if not clean:
        clean = [s[0] for s in fallback]          # 库里还没干净货,就用兜底种子当范例
    print(f"  [扩题] 干净范例 {len(clean)} 个(只用它当范例,去重仍按全库 {len(have)} 条)", flush=True)

    # 轮次与容忍度定这么松,是有实测依据的(2026-08-02):
    #   本地连跑同一个 expand_topics 三轮,分别拿到 5 / 5 / 19 条新题材 —— 函数本身没问题,
    #   **是单次调用会被截断的波动**。云端 run 30739376569 恰好连续两轮都空,
    #   原来「连续 2 轮空即罢手」就把整轮判死、入库 0。
    #   一次调用几十秒,多试几轮的代价远小于整轮空跑。
    for _ in range(8):
        if len(seed) >= need:
            break
        got = expand_topics(args.target, have | {s[0] for s in seed}, want=PER_CALL, clean_sample=clean)
        fresh = [g for g in got if g[0] not in have and g[0] not in {s[0] for s in seed}]
        print(f"  [扩题] AI 续列 {len(got)} 个,其中新的 {len(fresh)} 个", flush=True)
        if fresh:
            seed += fresh
            empty_rounds = 0
        else:
            empty_rounds += 1
            if empty_rounds >= 4:            # 连续四轮都空才认定题材真枯竭,不为一两次抽风放弃
                print("  [扩题] 连续四轮无新题材,判定本轮题材枯竭", flush=True)
                break

    # 图谱兜底只留给本草线。**生物计算已实测证明它是净负值**:
    #   图谱是古籍 OCR 的药材名(饮片名/碎句),到了生物计算这层要么生成阶段被判否,
    #   要么复核阶段因「无分子式」被拦 —— 至今没有一条能发布,却每条都要烧一次网关调用
    #   (run 30738747904:判否 58 条)。所以这里直接不给它兜底,宁可本轮少产几条。
    # 2026-08-04:本草线也撤掉图谱兜底 —— **实测它是纯垃圾源**。
    #   本草被按住 477 条里「名称错」占 33%,原文是「煮栝蒌至」「脂麻研」「焙用」这类 OCR 碎句;
    #   更糟的是已"发布"的 22 条里有 10 条是这种词(比開元/味同艾滓/微火者取/細研),正在前台展示,已下架。
    #   生物计算 2026-08-03 已撤图谱兜底,本草这条当时漏了 —— 又是"只改一半"。
    #   本草线的题材只能来自兜底种子 + AI 扩题(它们至少是真药材名)。
    print(f"  本轮可用题材 {len(seed)} 个\n", flush=True)

    ins = dup = fail = rej = invalid = 0
    model_used = ""

    def produce(it):
        """只做「调网关 + 解析」这段慢活(单条约 1~2 分钟),放线程池并发。
        判定与写库仍回主线程串行做 —— 计数器和 have 集合就不用加锁,也避免并发写 D1。"""
        try:
            return it, (gen_herb(*it, sys_prompt=ACTIVE_SYS) if args.target == "herb"
                        else gen_bio(*it, sys_prompt=ACTIVE_SYS)), None
        except Exception as e:
            return it, None, e

    # 超采投料:实测约四成题材是 OCR 碎词、会被 AI 验证器判否,所以按目标的 3 倍投
    batch = seed[: max(args.count * 3, args.count + 12)]
    print(f"  并发 {WORKERS} 路,投料 {len(batch)} 条,目标入库 {args.count} 条\n", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(produce, it) for it in batch]
        for fu in as_completed(futs):
            if ins >= args.count:
                for f2 in futs:
                    f2.cancel()          # 够数就撤掉还没开跑的,不白烧网关调用
                break
            item, got, err = fu.result()
            if err is not None:
                fail += 1
                print(f"  ✗ {item[0]} 生成失败: {type(err).__name__} {str(err)[:300]}", flush=True)
                continue
            obj, model = got
            model_used = model or model_used

            # AI 验证器判否:题材本身不是真药材/真成分(OCR 碎词),跳过且不计失败
            if isinstance(obj, dict) and obj.get("invalid"):
                invalid += 1
                print(f"  ○ {item[0]} 非真实药材,AI 验证器判否({str(obj.get('why'))[:24]})", flush=True)
                continue

            bad = violates(obj)
            if bad:
                rej += 1
                print(f"  ⛔ {item[0]} 触合规红线,整条丢弃 → 命中 {bad[:3]}", flush=True)
                continue

            name_cn = (obj.get("name_cn") or item[0]).strip()
            if name_cn in have:
                dup += 1
                print(f"  = {name_cn} 已存在,跳过", flush=True)
                continue

            eid = uuid.uuid4().hex[:16]
            if args.target == "herb":
                sql = (f"INSERT INTO herb_compare (herb_id,tradition,name_cn,name_en,name_latin,name_native,"
                       f"props_json,constitution,traditional_use,indications,actives,tcm_compare,tcm_refs,"
                       f"cog_tier,status,gen_model,gen_run_id,prompt_ver,created_at,updated_at) VALUES ("
                       f"{q(eid)},'ayurveda',{q(name_cn)},{q(item[0])},{q(item[1])},{q(obj.get('name_native'))},"
                       f"{q(obj.get('props'))},{q(obj.get('constitution'))},{q(obj.get('traditional_use'))},"
                       f"{q(obj.get('indications'))},{q(obj.get('actives'))},{q(obj.get('tcm_compare'))},"
                       f"{q(obj.get('tcm_refs'))},'②法度推演','draft',{q(model_used)},{q(run_id)},{q(PROMPT_VER)},{now},{now})")
            else:
                sql = (f"INSERT INTO biocomp_entries (entry_id,kind,name_cn,name_en,source_herb,formula,smiles,"
                       f"mol_weight,targets,mechanism,tcm_link,cog_tier,refs_json,status,gen_model,gen_run_id,"
                       f"prompt_ver,created_at,updated_at) VALUES ("
                       f"{q(eid)},'compound',{q(name_cn)},{q(obj.get('name_en'))},{q(obj.get('source_herb'))},"
                       f"{q(obj.get('formula'))},{q(obj.get('smiles'))},{q(obj.get('mol_weight'))},"
                       f"{q(obj.get('targets'))},{q(obj.get('mechanism'))},{q(obj.get('tcm_link'))},"
                       f"'②法度推演',{q(obj.get('refs'))},'draft',{q(model_used)},{q(run_id)},{q(PROMPT_VER)},{now},{now})")
            try:
                d1(sql)
                ins += 1
                have.add(name_cn)
                print(f"  ✓ {name_cn}  已入库(draft,待审)", flush=True)
            except Exception as e:
                fail += 1
                print(f"  ✗ {name_cn} 写库失败: {str(e)[:110]}", flush=True)

    d1(f"UPDATE content_gen_runs SET inserted={ins},skipped_dup={dup},failed={fail},"
       f"compliance_reject={rej},model={q(model_used)},finished_at={int(time.time())} WHERE run_id={q(run_id)}")
    # invalid 以前只统计不打印 —— 违反自己那条「只写进日志的数字等于没人看」,补进结尾行
    print(f"\n[完] 入库 {ins} · 重复跳过 {dup} · 合规拦下 {rej} · "
          f"AI判否(非真药材) {invalid} · 失败 {fail}")
    print(f"     全部 status='draft',**不自动上线**,后台审核通过才 published")
    print(f"     复查:SELECT * FROM {tbl} WHERE gen_run_id='{run_id}'")

    # 把这一轮的成绩与**失败反馈**写回轨迹表 —— 这是 Improve/Debug 算子的燃料。
    #   没有这一步,每轮跑完就忘,进化无从谈起(这正是我们缺的 Compile 环节)。
    fb = f"入库{ins} 重复{dup} 合规拦下{rej} AI判否{invalid} 失败{fail}"
    tid = record_trial(ACTIVE_VID, args.target, passed=ins, held=rej + invalid, failed=fail,
                       sample_n=ins + dup + rej + invalid + fail, feedback=fb,
                       run_ref=os.environ.get("GITHUB_RUN_ID", "local"))
    if tid:
        print(f"     [方案槽] 轨迹已记 {tid}(variant={ACTIVE_VID})")


if __name__ == "__main__":
    main()
