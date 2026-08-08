#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Adopt 算子 —— GitHub 热门应用 → 进化我们的技术

立此因(2026-08-04 创始人):
  「你先实现 GitHub 热门应用推送进化我们的技术,这个就已经是成功了第一步」
  「他怎么会修改代码,谁来实现,谁审核?」

断点在哪(实测):
  `arsenal_radar.py` 每天在扫 GitHub 高星仓,`reports/arsenal/candidates.json` 里
  **躺着 60 个现成候选**(freeCodeCamp 45万星、iflytek/astron-rpa 5897 星…),
  而它自己在代码里写着「**不做采不采用的判断**,判断在 SueAI 议事会」——
  **那个议事会从来没建**。于是情报采回来了,没有任何东西把它变成动作。
  同一个毛病还有第二处:`evolve_candidates` 里灌的全是 Cloudflare 博客标题和
  workers-sdk 发版说明 —— 噪声,不是能改进我们系统的东西。

本算子补的就是"议事会":把每个候选**判到我们的具体模块上**。
  判据不是"这个仓好不好"(高星仓当然好),而是:
    ① 它能改进**我们哪个模块**(必须从真实模块清单里选,选不出就判"不相关");
    ② 预计改善**哪个指标**(说不出指标 = 没想清楚 = 不采纳);
    ③ 工作量多大;④ 采纳 / 观望 / 不要。
  说不清"改哪个模块、动哪个指标"的一律判 skip —— **这条判据本身就是过滤器**,
  防止又产出一份"很有启发"但没人能动手的情报日报。

谁改代码、谁审核(创始人直接问的,写死在这里):
  · 配置级(阈值/路由表/开关)→ 系统自己改,确定性判据兜底,全自动
  · 提示词级                  → 系统自己改,A/B 真成绩裁决,全自动(improve.py)
  · **真代码级**              → 系统**只有写权、没有合权**:
      在 Actions 里写 patch → 开 PR → CI(74 个测试 + 零 LIST 守卫 + build)
      → 红线审查 → **人 merge**。绝不允许自动 push main。
  本算子产出的是**提案**,不是改动;采纳入口是它开的 Issue,由创始人/CTO 点。

铁律:只走内部免费池网关;不碰生产数据;不自动改任何代码。
"""
import os, sys, json, time, hashlib, argparse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
from _ai import d1, q, ask, parse_json                      # noqa: E402
sys.path.insert(0, HERE)
from _stack import scan_stack, already_have                 # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CAND_JSON = os.path.join(REPO_ROOT, "reports", "arsenal", "candidates.json")

# 我们系统的**真实模块清单**。判定必须落到这里面的某一个 ——
#   这是整个算子最关键的设计:不给它自由发挥的余地,它就编不出"很有启发"的空话。
MODULES = [
    ("检索路由",     "/api/retrieve 统一检索入口,按意图分派到 wiki/graph/volume/FTS"),
    ("RAG向量检索",  "tcm-rag-768 索引 183 万向量,古籍与医案的语义召回"),
    ("知识图谱星图", "sue_graph_nodes/edges 6.9 万节点,2D/3D 力导向可视化"),
    ("AI寻脉",       "古籍辨证参阅报告,RAG grounding + 多段结构化输出"),


    ("OCR产线",      "古籍扫描件转文字,云端 Actions 批量跑"),
    ("采集下载线",   "海外馆藏抓取、对账、归档"),
    ("自进化闭环",   "方案槽 / A-B 分流 / 适应度裁决 / 改进算子"),
    ("前台阅读器",   "沉浸式阅读、分页、字号、会员门禁"),
    ("免费模型池",   "网关多供应商容错链、探活、配额调度"),
]
# 【2026-08-05 创始人明令「禁止再去做药方」】
#   原清单里有「内容工厂(本草/生物计算条目生成)」与「导读生成」——
#   本轮收件箱就往那儿推了 4 条(data-diff / paperbanana / ClaimeAI / newsflow-oss)。
#   **清单不删,鹰眼就会一直把审核人往药方线上带。**
#   自我进化的对象是**系统**(采集/判定/路由/引擎/前台),不是内容质量。
#   领域内容的判断归领域专家,不归技术 CTO。
MODULE_NAMES = [m[0] for m in MODULES]

SYS_ADOPT = (
    "你是「古方 AI 星图」平台的**技术选型评审员**。给你一个 GitHub 开源项目,"
    "判断它**能不能用来改进我们某个具体模块**。\n"
    "我们的模块清单(只能从这里选,不许自创):\n"
    + "\n".join(f"  · {n} —— {d}" for n, d in MODULES) + "\n\n"
    "判定规则(从严):\n"
    "  · 说不出它改进**哪一个**模块 → verdict=\"skip\";\n"
    "  · 说不出预计改善**哪个可量化指标** → verdict=\"skip\";\n"
    "  · 只是「很有启发」「值得关注」而没有具体接法 → verdict=\"skip\";\n"
    "  · 通用基础设施(语言、框架、教程仓、awesome 列表)一律 skip;\n"
    "  · **绝不因为星多就采纳** —— 星数不是判据,能不能接进我们的模块才是。\n"
    # 【2026-08-04 首轮月榜实测】写「绝不因为星多就采纳」完全没用:
    #   redis(75876★)被判成「RAG向量检索/检索响应延迟」、caddy(74627★)被判成
    #   「前台阅读器/阅读体验流畅度」—— 明星光环压过了判据。
    #   抽象劝告没用,**必须把我们的部署形态作为硬约束写进去**,让它有具体的东西可判。
    "  · **我们的运行形态是 Cloudflare Serverless(Workers/Pages/D1/R2/Vectorize)+ "
    "GitHub Actions,零常驻主机、零月费**。任何需要**常驻服务进程**的项目 —— "
    "数据库服务器(Redis/Postgres/MySQL)、搜索集群(Elasticsearch/OpenSearch/Meilisearch)、"
    "图数据库(Neo4j)、反向代理/Web 服务器(Nginx/Caddy)、消息队列、K8s —— "
    "**一律 skip**,不管它多少星、多么优秀。我们没有机器跑它。\n"
    "  · 判 adopt 前先自问:**这东西是纯库/纯脚本/纯算法(能直接 import 或在 Actions 里跑),"
    "还是要开一台机器常驻?** 后者一律 skip。\n"
    "verdict 三档:adopt(该接,给出接法) / watch(方向对但现在不动) / skip(不相关)。\n"
    # 【2026-08-04 创始人当场骂:「是头猪都知道 PaddleOCR 这好用」】
    #   判定器把 PaddleOCR/tesseract/MinerU 全判成「改进 OCR 产线」——
    #   而 `pip install paddlepaddle paddleocr` 就在我们自己的 ocr_race.yml 里,
    #   我们早跑过 OCR 引擎对赛、手上有它的实测数据;RapidOCR 更是已落地的主力。
    #   同一轮还把 firecrawl/browser-use/crawl4ai/Scrapling 四个爬虫全堆到采集线上。
    #   **这不是判断,是「这个领域最有名的工具是什么」的同义词联想。**
    #   根因:判定器不知道自家家底,而候选又按星数喂进去 → 高星 + 无知 = 常识复读机。
    #   两处焊死:① 已有的零成本硬跳过(见 already_have) ② 必须说出「我们现在用什么、它强在哪」。
    "  · **不许推荐我们已经在用的东西**。下面会给出我们的真实家底,凡是已在其中的一律 skip。\n"
    "  · 判 adopt 必须答出两件事,答不出就 skip:"
    "**① 这个模块我们现在用的是什么;② 候选在哪个可量化指标上胜过它。**\n"
    "  · 「集成 X 提高准确率」这种话**不算答案** —— 那是同义词复读,不是判断。\n"
    "只输出 JSON:{\"module\":\"清单里的模块名或空\",\"verdict\":\"adopt|watch|skip\","
    "\"metric\":\"预计改善的指标,如「检索召回率」「导读生成耗时」\",\"how\":\"40字以内具体接法\","
    "\"effort\":\"小|中|大\","
    "\"current\":\"我们该模块现在用的是什么\",\"beats\":\"在哪个可量化指标上胜过它\","
    "\"why\":\"30字以内理由\"}"
)

# 全局家底(main 里扫一次填上)—— 判定前先拿它零成本筛掉我们已经有的
STACK_NAMES, STACK_LINES = set(), []


GAP_JSON = os.path.join(REPO_ROOT, "reports", "arsenal", "gap_candidates.json")

# ── 确定性预筛词表 ──────────────────────────────────────────────
# 创始人 2026-08-04:「要多采集,然后筛选自己需要的」。
#   广采的前提是**筛得起**:每个仓一次模型调用,几百个候选就是几百次调用。
#   所以先用零成本的确定性预筛砍掉明显无关的,模型只判剩下的。
#   实测第一轮 30 个里 20 个是 freeCodeCamp / TypeScript 这类 —— 这些根本不该花模型调用。
NOISE = ("awesome-", "tutorial", "curriculum", "interview", "roadmap", "cheatsheet",
         "bootcamp", "learn-", "-course", "study-", "examples", "boilerplate",
         "starter", "template", "demo", "playground", "hello-world", "free-programming")
# 命中任一即进入模型精判 —— 覆盖我们 11 个模块真正吃的技术面
SIGNAL = ("rag", "retriev", "embed", "vector", "rerank", "hybrid search", "bm25",
          "knowledge graph", "graphrag", "graph visual", "force-directed", "force graph",
          "edge bundl", "louvain", "community detect", "layout",
          "ocr", "document parsing", "pdf extract", "layout analysis", "handwrit",
          "hallucinat", "groundedness", "citation", "attribution", "fact check", "self-rag",
          "eval", "benchmark", "gold set", "llm-as-judge", "prompt optim", "dspy",
          "agent", "workflow orchestr", "self-improv", "evolutionary", "genetic",
          "tcm", "traditional chinese medicine", "herb", "chinese medic", "classical chinese",
          "ancient text", "cjk", "chemistry", "molecul", "smiles", "pubchem", "bioinformatic",
          "llm gateway", "proxy", "load balanc", "free api", "model router", "fallback",
          "crawler", "scraper", "iiif", "digital librar", "archive")


def _prefilter(r):
    """零成本预筛。返回 (是否进模型精判, 原因)。"""
    name = str(r.get("repo") or r.get("full_name") or "").lower()
    blob = " ".join([name, str(r.get("description") or ""),
                     " ".join(r.get("topics") or [])]).lower()
    if any(k in name for k in NOISE):
        return False, "教程/模板/awesome 类"
    # 缺口驱动的必须**先于**信号词检查放行 —— 它是拿我们自己的短板去定向搜回来的,
    #   相关性由检索式保证,不该再要求它的英文描述里出现我们的词表。
    #   (自测抓到:这句原来写在信号词检查之后,等于把最该留的一路当噪声砍掉。)
    if str(r.get("found_by") or "").startswith("gap:"):
        return True, "缺口定向"
    hits = [k for k in SIGNAL if k in blob]
    if not hits:
        return False, "描述里没有任何我们吃得上的技术面"
    return True, "命中:" + "/".join(hits[:3])


def load_repo_candidates(limit=400):
    """三条采集路线合流:**月榜全量库**(主)+ 缺口驱动 + 热门榜快照。

    创始人 2026-08-04:「不如直接看月榜,**把所有的搜集到我们的后台**,按照那来内部消化」。
      月榜是 trending.py 全量灌进 `gh_repo_pool` 的,采集端一个都不筛;
      筛在这里做 —— 这样判据改了可以**重新消化历史全量**,不用重新采集。
    合流之后三条路的采纳率可以直接对比,哪条标准更好不靠嘴说,靠数字。
    """
    rows = []
    try:
        # 【2026-08-05 实测修·鹰眼断在这】原来是 `ORDER BY stars DESC LIMIT 2000`,
        #   等于**每天从同一批高星头部捞**。那批已经判过 222 个,剩下的被预筛砍光,
        #   于是本轮只精判 7 个、采纳 0 —— 不是判得不好,是**没有新东西可判**。
        #   而当天新入库的 72 个是「星速高、总星低」的新项目,按总星排永远在 2000 名之后,
        #   **永远轮不到**。星速(star_delta)正是月榜独有的信号,采回来了却没用在排序上。
        # 改:未判过的优先,按**星速**排;星速相同再看总星。
        # 【2026-08-05 二修】上一版直接用 star_delta 排序,D1 报 400 ——
        #   那列是 ranks.py 用 ALTER 加的,**不保证真加上了**(我又猜了列名)。
        #   改成:先试带星速的,报错就退回不带的,并打印实际走了哪条 —— 不静默降级。
        base = ("SELECT repo, url, description, stars, lang, topics, found_by "
                "FROM gh_repo_pool "
                "WHERE repo NOT IN (SELECT title FROM evolve_candidates WHERE kind='repo') ")
        try:
            pool = d1(base + "ORDER BY COALESCE(star_delta,0) DESC, COALESCE(stars,0) DESC LIMIT 2000")
            print("  [采集] 排序=星速优先(star_delta)", flush=True)
        except Exception as e1:
            print(f"  [采集] 星速列不可用({str(e1)[:50]}),退回总星排序", flush=True)
            pool = d1(base + "ORDER BY COALESCE(stars,0) DESC LIMIT 2000")
        for r in pool:
            r["topics"] = [t for t in str(r.get("topics") or "").split(",") if t]
        print(f"  [采集] 月榜全量库:{len(pool)} 个", flush=True)
        rows += pool
    except Exception as e:
        print(f"  [采集] 读月榜库失败({str(e)[:70]}),走文件兜底", flush=True)
    for p, tag in ((GAP_JSON, "缺口驱动"), (CAND_JSON, "热门榜快照")):
        if not os.path.exists(p):
            print(f"  [采集] 缺 {os.path.basename(p)}({tag}),跳过", flush=True)
            continue
        d = json.load(open(p, encoding="utf-8"))
        rs = d if isinstance(d, list) else (d.get("rows") or d.get("candidates") or [])
        print(f"  [采集] {tag}:{len(rs)} 个", flush=True)
        rows += rs
    seen, out = set(), []
    for r in rows:
        n = str(r.get("repo") or r.get("full_name") or "")
        if n and n not in seen:
            seen.add(n)
            out.append(r)
    return out[:limit]


def already_judged():
    try:
        return {r["title"] for r in d1("SELECT title FROM evolve_candidates "
                                       "WHERE kind='repo' AND status!='new'")}
    except Exception:
        return set()


_BRAIN = {"body": None, "vid": None}


def brain():
    """本轮要用的判定大脑:优先 D1 方案槽的在位冠军,取不到回落源码 SYS_ADOPT。

    【2026-08-07 刀2·闭合雷达断头】此前 radar_race 每天赛马、算出更好的判定
    提示词,然后**只打印一张结果表**给人看 —— 冠军永远换不上去,赛马等于空转。
    这里读 D1 让晋升真能落地,`radar_race.py --promote` 是写入端。

    **雷达刻意不做 A/B 流量分流(explore=0)**,与内容线不同:
    判定大脑是整个闭环的入口,判错 = 后面所有环节没有燃料,风险不对称;
    而挑战者已在 14 题冻结考卷 + 确定性判分上充分验证过(影子评测),
    不需要再拿真实判定流量当试验田。这正是架构定的
    「无生产流量的对象:影子评测通过即可直接换 champion」。

    取不到一律回落源码常量 —— D1 挂了雷达照跑,绝不因方案槽读失败停摆。
    """
    if _BRAIN["body"] is None:
        body, vid = SYS_ADOPT, None
        try:
            rows = d1("SELECT variant_id, body FROM evolve_variants "
                      "WHERE scope='radar' AND slot='SYS_ADOPT' AND status='active' "
                      "ORDER BY updated_at DESC LIMIT 1")
            if rows and (rows[0].get("body") or "").strip():
                body, vid = rows[0]["body"], rows[0]["variant_id"]
                print(f"  [判定大脑] 走 D1 在位冠军 {vid}", flush=True)
            else:
                print("  [判定大脑] 方案槽空,走源码默认 SYS_ADOPT", flush=True)
        except (Exception, SystemExit) as e:
            # SystemExit 必须一起接:_ai.d1() 缺凭据时走的是 sys.exit() 而不是
            # raise,只写 except Exception 的话本地/凭据失效时**整个进程直接死**,
            # 与本函数"D1 挂了雷达照跑"的设计意图正好相反。实测抓到,不是推演。
            print(f"  [判定大脑] 读方案槽失败({type(e).__name__}),回落源码默认", flush=True)
        _BRAIN["body"], _BRAIN["vid"] = body, vid
    return _BRAIN["body"]


def judge(r, sys_body=None):
    """判一个候选。sys_body 缺省=冠军大脑;星网共判时传入其他认知路径的提示词——
    **判定逻辑与硬闸完全同一套**,只有大脑不同,票才可比。"""
    b = sys_body or brain()
    name = str(r.get("repo") or r.get("full_name") or "").strip()
    desc = str(r.get("description") or "")[:400]
    topics = ",".join(r.get("topics") or [])[:200]
    user = (f"项目:{name}\n星数:{r.get('stars')}\n语言:{r.get('lang')}\n"
            f"topics:{topics}\n简介:{desc}")
    txt, model = ask(b, user, max_tokens=500)
    try:
        o = parse_json(txt)
    except Exception:
        # 首轮实测 30 个里 3 个解析失败(10%)—— 模型没吐 JSON。重试一次再放弃。
        # 重试也必须走同一个大脑 —— 主路径读 D1 冠军、重试路径读源码常量,
        # 等于同一轮判定用了两套提示词,分数无从归因(「只修一半」的老病)。
        txt, model = ask(b, user + "\n\n【必须只输出 JSON,不要任何别的字】",
                         max_tokens=500)
        try:
            o = parse_json(txt)
        except Exception as e:
            return None, f"解析失败 {type(e).__name__}(重试后仍失败)"
    # ── 占位符闸(2026-08-07 立·全池实测 26% 的 skip 是假判定)────────────
    # 创始人:「GitHub 这种好东西太多了,而我们的鹰眼就是瞎子」。用 Langflow 验尸:
    #   langflow(152826★) / dify(151261★) / Flowise(55132★) 三个都**采到了**,
    #   全部 status='skip',而 verdict_note 是
    #     {"module":"...","metric":"...","how":"...","effort":"...","why":"..."}
    #   —— 模型把提示词里的示例格式原样吐了回来,一个字的真实判断都没有。
    # 旧逻辑下它会怎样:module="..." 不在清单 → 自动降级 skip → 入库沉没。
    #   于是「模型没干活」被记成了「判定为拒绝」,而 skip 不会再被复审。
    # **判定失败和判定为拒绝是两件事**,混在一起就是这 59 条的由来。
    # 返回 None 走已有的失败路径(continue 不入库),候选留在 new,下轮重判。
    def _placeholder(x):
        s = str(x or "").strip().strip('"').strip()
        return (not s) or s in ("...", "…", "..", "n/a", "N/A", "todo", "TODO", "xxx", "XXX")
    blank = [k for k in ("module", "metric", "how", "why") if _placeholder(o.get(k))]
    if len(blank) >= 2:
        return None, f"占位符判定(字段 {'/'.join(blank)} 是模板占位,模型没真判)—— 不入库,下轮重判"

    v = str(o.get("verdict") or "skip").lower()
    if v not in ("adopt", "watch", "skip"):
        v = "skip"
    mod = str(o.get("module") or "").strip()
    # 硬闸:模块必须在清单里,指标必须写了 —— 否则一律降到 skip。
    #   这条不问模型,确定性判据;不然它会自创模块名把话说圆。
    if v in ("adopt", "watch") and (mod not in MODULE_NAMES or not str(o.get("metric") or "").strip()):
        v = "skip"
        o["why"] = f"(自动降级)模块「{mod}」不在清单或未给出指标 · 原判 {o.get('verdict')}"
    # 说不出「我们现在用什么 / 它强在哪」= 没做判断,只是联想 → 降级。确定性判据,不问模型。
    if v == "adopt" and not (str(o.get("current") or "").strip() and str(o.get("beats") or "").strip()):
        v = "watch"
        o["why"] = "(自动降级 adopt→watch)说不出我们现在用什么 / 它强在哪 · " + str(o.get("why") or "")[:40]
    o["verdict"], o["module"], o["_model"] = v, mod, model
    return o, None


# ── 星网共判(认知裂变第 4 件·2026-08-08)────────────────────────────
# 创始人 2026-05-22 对裂变的定义,最后半句一直没落地:
#   「第 5 本书进来时,是**很多已成形的组合认知一起读它**」——
#   此前判定始终是一个大脑判一次,网状根本没网起来。
# 落法(升级审,不是全量陪审):
#   · 冠军初判 skip → 直接过(skip 类考卷准确率 0.90-1.00,陪审是浪费);
#   · 初判 adopt/watch =「有意思的书」→ 赛马场上 ≥基线的活体认知
#     (单体+裂变组合体,radar_race_result.json 里带 body 的)各自再判一次,
#     同一套 judge() 硬闸,多数票定终判 —— 这正打在最弱的 adopt 类准确率上。
#   · 判定失败不计票(判定失败≠判定为拒绝,占位符闸同源的规矩);
#   · 平票 → watch(网内有分歧本身就是"值得盯"的信号);
#   · 终判升到 adopt 时,用真投了 adopt 的那个读者的完整输出落库
#     (它过了 current/beats 硬闸,冠军的 watch 输出没有这两样)。
MAX_CO_READERS = 3
_PATHS = {"v": None}


def cognition_paths():
    """读者名单:赛马结果里 ≥基线的活体(与在位冠军去重)。缺文件/无合格行 → 空,退回单脑。"""
    if _PATHS["v"] is None:
        paths = []
        try:
            fp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "radar_race_result.json")
            d = json.load(open(fp, encoding="utf-8"))
            base = d.get("baseline") or 0
            champ = brain().strip()
            for row in d.get("rows") or []:
                b = (row.get("body") or "").strip()
                if b and row.get("score") is not None and row["score"] >= base and b != champ:
                    paths.append({"who": str(row.get("who") or "?"), "body": b,
                                  "score": row["score"]})
            paths.sort(key=lambda p: -p["score"])
        except Exception as e:
            print(f"  [星网] 读认知路径失败({type(e).__name__}),本轮单脑判定", flush=True)
        _PATHS["v"] = paths[:MAX_CO_READERS]
        if _PATHS["v"]:
            print("  [星网] 共判读者 " + " · ".join(
                f"{p['who']}({p['score']:.2f})" for p in _PATHS["v"]), flush=True)
    return _PATHS["v"]


def co_judge(r, o):
    """星网共判:返回(终判 o, 票面说明)。冠军的 o 已在手,读者们各判一票。"""
    votes, adopt_o = [("冠军", o["verdict"])], (o if o["verdict"] == "adopt" else None)
    for p in cognition_paths():
        po, perr = judge(r, sys_body=p["body"])
        if perr:
            print(f"    ~ 读者 {p['who']} 判定失败不计票({str(perr)[:40]})", flush=True)
            continue
        votes.append((p["who"], po["verdict"]))
        if po["verdict"] == "adopt" and adopt_o is None:
            adopt_o = po
    tally = {"adopt": 0, "watch": 0, "skip": 0}
    for _, pv in votes:
        tally[pv] += 1
    best = max(tally.values())
    winners = [k for k, c in tally.items() if c == best]
    final = winners[0] if len(winners) == 1 else "watch"
    face = f"{tally['adopt']}A/{tally['watch']}W/{tally['skip']}S"
    if final == "adopt" and adopt_o is not None:
        o = adopt_o                       # 带着过闸的 current/beats 落库
    if final != o["verdict"] or len(votes) > 1:
        o["why"] = f"[星网{face}]" + str(o.get("why") or "")[:250]
    o["verdict"] = final
    return o, face


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30, help="本轮评审几个候选")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-starnet", action="store_true", help="关星网共判,回退单脑(排障用)")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    global STACK_NAMES, STACK_LINES
    STACK_NAMES, STACK_LINES = scan_stack()
    print(f"  [家底] 扫出 {len(STACK_NAMES)} 个已有技术名,判定前先拿它筛", flush=True)
    rows = load_repo_candidates()
    done = already_judged()
    fresh = [r for r in rows if str(r.get("repo") or r.get("full_name") or "") not in done]

    # 两级筛:① 零成本确定性预筛 → ② 模型精判(只花在过了预筛的身上)
    kept, dropped = [], 0
    for r in fresh:
        ok, why = _prefilter(r)
        if ok:
            r["_pre"] = why
            kept.append(r)
        else:
            dropped += 1
    todo = kept[:a.limit]
    print(f"=== Adopt 算子 · 采到 {len(rows)} · 已判 {len(done)} · "
          f"预筛砍掉 {dropped}(零成本)· 本轮精判 {len(todo)} ===", flush=True)

    out, now = {"adopt": [], "watch": [], "skip": 0, "fail": 0,
                "prefilter_dropped": dropped, "harvested": len(rows)}, int(time.time())
    # 同一模块每轮最多 2 条 adopt —— 防四个爬虫全堆在采集线上。
    # 【2026-08-05】上一轮 run 30968296973 就挂在这:我加了用它的地方,**漏了定义**,
    #   `NameError: per_module is not defined` 整条产线挂掉 —— 又一次"只改一半"。
    per_module = {}
    for r in todo:
        name = str(r.get("repo") or "")
        o, err = judge(r)
        if err:
            out["fail"] += 1
            print(f"  ✗ {name}: {err}", flush=True)
            continue
        # 星网共判:初判 adopt/watch 的"有意思的书"才升级到多认知共读
        if o["verdict"] in ("adopt", "watch") and not a.no_starnet and cognition_paths():
            v0 = o["verdict"]
            o, face = co_judge(r, o)
            print(f"  🕸️ {name}: 冠军初判 {v0} → 星网 {face} → 终判 {o['verdict']}", flush=True)
        v = o["verdict"]
        line = {"repo": name, "stars": r.get("stars"), "url": r.get("url"),
                "module": o.get("module"), "metric": o.get("metric"),
                "how": o.get("how"), "effort": o.get("effort"), "why": o.get("why")}
        if v == "adopt":
            k = o.get("module")
            per_module[k] = per_module.get(k, 0) + 1
            if per_module[k] > 2:
                v = "watch"
                o["why"] = f"(自动降级)模块「{k}」本轮已采纳 2 条,同类堆叠没有意义"
        if v == "skip":
            out["skip"] += 1
        else:
            out[v].append(line)
            print(f"  {'🎯' if v=='adopt' else '👀'} {name}({r.get('stars')}★)"
                  f" → {o.get('module')} · {o.get('metric')} · {o.get('effort')}", flush=True)
        if a.dry_run:
            continue
        cid = "c_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
        note = json.dumps({k: o.get(k) for k in ("module", "metric", "how", "effort", "why")},
                          ensure_ascii=False)[:900]
        try:
            d1("INSERT OR REPLACE INTO evolve_candidates "
               "(cand_id,kind,title,url,radar_run,score,status,verdict_note,created_at,updated_at) "
               f"VALUES ({q(cid)},'repo',{q(name)},{q(str(r.get('url') or '')[:300])},"
               f"{q(str(now))},{q(r.get('stars') or 0)},{q(v)},{q(note)},{now},{now})")
        except Exception as e:
            print(f"    写库失败:{str(e)[:90]}", flush=True)

    print(f"\n=== 本轮:采纳 {len(out['adopt'])} · 观望 {len(out['watch'])} · "
          f"不相关 {out['skip']} · 失败 {out['fail']} ===", flush=True)
    if a.report:
        json.dump(out, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
