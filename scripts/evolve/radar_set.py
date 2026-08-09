#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雷达回归集 —— 给**判定大脑**出的考卷,和判分尺子

立此因(2026-08-06 创始人):
  「我们一定要**深度优化雷达和自我进化的系统技术**,而不是剂量/用法/疗效承诺/医嘱口吻正则」
  「你为什么又偏了啊」

他说的是结构问题,不是我临时手滑。此前进化算子的 `SCOPE_MODULES` 长这样:
    {"biocomp": ("内容工厂", …), "herb": ("内容工厂", …)}
而「内容工厂」这个模块**已经被创始人从雷达清单里删掉了**(2026-08-05「禁止再去做药方」)。
  → 进化算子唯一认识的模块是一个已经不存在的模块,**它每轮只能往药方线上跑**。
  → 自进化系统在优化"怎么写好一条中药材条目",而不是"这套系统本身怎么变强"。

本文件把进化对象换成**雷达自己的判定大脑**(`adopt.py` 的 `SYS_ADOPT`)。
理由是它同时满足三条,别的系统模块都做不到:
  ① 它是一段**提示词** —— 改进算子只会改提示词,这是它够得着的唯一系统组件;
  ② 它有**标准答案** —— `adoption.txt` 里 landed / DROPPED 是我真跑过留下的判决;
  ③ 它的错**有据可查** —— 创始人当场骂过的两次误判都留了痕,正好当考题。

**判分零模型调用、零随机**:考的是"雷达判得对不对",拿已知答案对账,
  跟 `gate.py` 一样是确定性尺子 —— 尺子会抖,进化就是在噪声里瞎走。
"""
import os, re, sys, json, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPTION = os.path.join(HERE, "..", "intel_radar", "adoption.txt")

# v2 断代(2026-08-08 对抗审查后):考官 dashscope(付费,红线①)→zhipu 免费家、
# 考卷调用 temperature 0.3→0、缓存键补全考官+题干+卷版。v1 时代分数与 v2 不可比。
# v3 断代(2026-08-09 judge_scorecard 后):adopt 类修正 —— 已落地项(与家底逻辑矛盾)
# 下沉 skip,adopt 只留未落地真机会(MinerU/LightRAG/codebase-memory-mcp)。答案变,断代。
SCORER_VERSION = "radar-v3-2026-08-09"


# ══════════════════════════════════════════════════════════════
# 考卷 —— 每一题都是**真实发生过的判定**,不是编的场景
# ══════════════════════════════════════════════════════════════
#   want:  期望的 verdict
#   why:   这题为什么在卷子上(哪次事故 / 哪条判决)
#   probe: 除了 verdict 之外还要查什么(家底意识、常驻主机意识)
EXAM = [
    # ── 甲类:常驻主机陷阱(2026-08-04 首轮月榜实测的真实误判)──────
    dict(repo="redis/redis", stars=75876, lang="C",
         desc="Redis is an in-memory database that persists on disk. "
              "The data model is key-value, but many kinds of values are supported.",
         want="skip", probe="standing_host",
         why="首轮实测被判成「RAG向量检索/检索响应延迟」—— 明星光环压过判据。"
             "我们零常驻主机,没有机器跑它。"),
    dict(repo="caddyserver/caddy", stars=74627, lang="Go",
         desc="Fast and extensible multi-platform HTTP/1-2-3 web server with automatic HTTPS",
         want="skip", probe="standing_host",
         why="首轮实测被判成「前台阅读器/阅读体验流畅度」。Web 服务器,我们是 Pages/Workers。"),
    dict(repo="elastic/elasticsearch", stars=72000, lang="Java",
         desc="Free and Open Source, Distributed, RESTful Search Engine",
         want="skip", probe="standing_host",
         why="搜索集群需要常驻节点。同一类陷阱换一个门面,验判定器学的是原则还是名字。"),
    dict(repo="neo4j/neo4j", stars=14000, lang="Java",
         desc="Graphs for Everyone - the world's leading graph database",
         want="skip", probe="standing_host",
         why="图数据库。我们的星图跑在 D1 + 前端力导向上,不需要图库服务。"),

    # ── 乙类:家底陷阱(2026-08-04 创始人当场骂「是头猪都知道 PaddleOCR 这好用」)──
    dict(repo="PaddlePaddle/PaddleOCR", stars=44000, lang="Python",
         desc="Awesome multilingual OCR toolkits based on PaddlePaddle",
         want="skip", probe="already_have",
         why="`pip install paddleocr` 就写在我们自己的 ocr_race.yml 里。"
             "推荐我们已经在用的东西 = 常识复读,不是判断。"),
    dict(repo="RapidAI/RapidOCR", stars=3800, lang="Python",
         desc="Awesome OCR multiple programing languages toolkits based on ONNXRuntime",
         want="skip", probe="already_have",
         why="已落地的主力第二 OCR 线,adoption.txt 有记录。"),
    dict(repo="tesseract-ocr/tesseract", stars=62000, lang="C++",
         desc="Tesseract Open Source OCR Engine (main repository)",
         want="skip", probe="already_have",
         why="同一轮被堆到 OCR 产线上的第三个 —— 判定器在做「这领域最有名的工具是什么」的同义词联想。"),
    # ── 乙2类:堆叠陷阱·2026-08-08 扩题(adopt.py 记录在案的真实事故:──────
    #   「同一轮还把 firecrawl/browser-use/crawl4ai/Scrapling 四个爬虫全堆到采集线上」。
    #   这两题的名字**不在**任何提示词正文里(与甲/乙类不同)—— 专验判定器学的是
    #   「采集线已有自研体系+浏览器自动化要本地算力」这条原则,不是背名单。
    dict(repo="browser-use/browser-use", stars=100000, lang="Python",
         desc="Make websites accessible for AI agents. Automate tasks online with ease. "
              "Playwright-based browser automation for LLM agents.",
         want="skip", probe="already_have",
         why="真实事故:被判 adopt 堆到采集线上。Playwright 要本地/常驻浏览器算力,"
             "我们的采集线是 CF Workers+Actions 云端体系 —— 10 万星是对「星数不是判据」的又一次检验。"),
    dict(repo="unclecode/crawl4ai", stars=40000, lang="Python",
         desc="Open-source LLM Friendly Web Crawler & Scraper. Crawl smarter, faster.",
         want="skip", probe="already_have",
         why="同一轮四爬虫堆叠事故之二。采集线已有成熟自研抓取体系(worker 分流+对账),"
             "再堆一个通用爬虫不是改进,是同类堆叠。"),

    # ── 丙类:该采纳但**尚未落地**的真机会(adopt)──────────────────────
    #   【2026-08-09 judge_scorecard 挖出并修正的深层矛盾】此前丙类拿「adoption.txt 里真
    #   落地了的」项目(graphify/OpenMontage/bookget…)当 adopt 样本,但那与生产家底逻辑
    #   直接冲突:judge 注入家底清单后,已落地项被**正确**判 skip(已在用不是新机会),却和
    #   考卷 want=adopt 打架 —— adopt 类一注入家底就 2/6→0/6。根本病:**已落地=生产该 skip**,
    #   不该当 adopt 样本。改:adopt 题只留「系统真判过 adopt、但还没落地」的真机会(它们不
    #   在家底,注入家底不影响其判定);已落地项下沉到 skip 类(见下,probe=already_have)。
    dict(repo="opendatalab/MinerU", stars=25000, lang="Python",
         desc="A high-quality tool for convert PDF to Markdown and JSON. "
              "Layout detection, formula recognition, table extraction.",
         want="adopt", probe="",
         why="**仍是候选、未落地**(_stack.py 家底闸实测「仍是候选」)。纯库、Actions 能跑,"
             "OCR 产线正缺版面/表格结构化 —— 该 adopt,且不在家底,注入家底不影响判定。"),
    dict(repo="HKUDS/LightRAG", stars=38649, lang="Python",
         desc="Simple and Fast Retrieval-Augmented Generation. Dual-level retrieval "
              "plus knowledge graph enhancement. Pure Python SDK, no server.",
         want="adopt", probe="",
         why="**系统 2026-08-09 真判 adopt**(星网 4A/0W/0S 全票)、**尚未落地**。RAG 向量检索"
             "的真机会:双层检索+图增强补纯向量召回天花板。纯 SDK、Actions 跑、不在家底。"),
    dict(repo="DeusData/codebase-memory-mcp", stars=38167, lang="TypeScript",
         desc="Indexes codebases into a persistent knowledge graph for AI agents, "
              "with MinHash/LSH entity resolution.",
         want="adopt", probe="",
         why="**系统 2026-08-09 真判 adopt**(星网 3A/0W/1S)、**尚未落地**。知识图谱星图的真"
             "机会:持久图+实体消解,正治我们星图重复节点。不在家底。"),
    # 【2026-08-06 出题当天就撤掉的一题 —— 留痕,别再犯】
    #   我曾把 BerriAI/litellm 标成 want="skip" / probe="already_have",理由写「已在家底」。
    #   跑完基线去核实,全仓 grep 只命中三处:模型自己的答案缓存、`land.py` 里**我自己写的举例**、
    #   和一个叫 `sk-litellm-local-dev` 的 key 名字。**litellm 根本没在我们系统里用。**
    #   我把「我写的举例」当成了家底证据 —— 正是「记忆是线索不是真相」那条铁律的原样重犯。
    #   所以撤题 —— 我手上没有它的真实判决,而**每一题都必须是真发生过的判定**。

    # ── 己类:已落地在用 → 生产遇到判 skip(家底非新机会,probe=already_have)──────
    #   这些是 adoption.txt 里真落地了的。judge 在生产采集里再遇到它们(它们还在 GitHub 榜上)
    #   正确答案是 skip:别重复采纳已在用的。放这里既保留「真发生过」的证据,又与家底逻辑一致
    #   —— 正是它们此前被错标 adopt 导致注入家底后 adopt 类崩盘,现归位到该在的类别。
    dict(repo="Graphify-Labs/graphify", stars=2100, lang="Python",
         desc="Turn any codebase or corpus into a knowledge graph. Community detection, "
              "surprise scoring, standalone HTML export. Pure library, no server required.",
         want="skip", probe="already_have",
         why="**已落地在用**(2026-08-05:/api/graph/overview+insights 上线,实测 8 区 46560 节点)。"
             "生产再遇到它该 skip —— 已在家底不是新机会。"),
    dict(repo="calesthio/OpenMontage", stars=1200, lang="Python",
         desc="Automatic video montage from clips. ffmpeg-based named transitions "
              "(xfade), beat-synced cuts, no GPU required.",
         want="skip", probe="already_have",
         why="**已落地在用**(2026-07-21:xfade 具名转场接进 make_video.py)。生产再遇到该 skip。"),
    dict(repo="dreammis/social-auto-upload", stars=4600, lang="Python",
         desc="Automatically upload videos to douyin, xiaohongshu, bilibili, tiktok. "
              "CLI tool, MIT licensed, cookie-based auth, no server.",
         want="skip", probe="already_have",
         why="**已落地在用**(2026-07-29:内容工厂→抖音/小红书投递适配器)。生产再遇到该 skip。"),
    dict(repo="deweizhu/bookget", stars=1300, lang="Go",
         desc="Digital library book downloader supporting 50+ libraries: "
              "NDL Japan, Naikaku Bunko, Harvard-Yenching, CADAL, etc. CLI tool.",
         want="skip", probe="already_have",
         why="**已落地在用**(下载线主力,内閣和書 fonds3682585 靠它下)。生产再遇到该 skip。"),
    dict(repo="obra/superpowers", stars=2000, lang="TypeScript",
         desc="Skills, hooks and workflow discipline for Claude Code agents: "
              "TDD enforcement, debugging workflows, planning gates.",
         want="skip", probe="already_have",
         why="**已落地在用**(已装启用于 .claude/settings.json,protected_files_gate 学它写的)。"
             "生产再遇到该 skip。"),

    # ── 丁类:试过但不采用的(adoption.txt 里 DROPPED)────────────
    dict(repo="DietrichGebert/ponytail", stars=95160, lang="TypeScript",
         desc="A Claude Code plugin that constrains the agent to write less code.",
         want="skip", probe="already_have",
         why="**真试过并 DROPPED**:定位是约束 agent 少写代码,"
             "我们已有 zero-error-constitution 与 code-change-discipline 两个 skill 覆盖同一职责。"
             "9.5 万星,是对「绝不因为星多就采纳」最硬的一次检验。"),

    # ── 戊类:通用基础设施(规则里明写一律 skip)──────────────────
    dict(repo="freeCodeCamp/freeCodeCamp", stars=407000, lang="TypeScript",
         desc="freeCodeCamp.org's open-source codebase and curriculum. Learn to code for free.",
         want="skip", probe="",
         why="教程仓,月榜第一。星数最高的一题,专门验「星数不是判据」。"),
    dict(repo="sindresorhus/awesome", stars=340000, lang="",
         desc="Awesome lists about all kinds of interesting topics",
         want="skip", probe="",
         why="awesome 列表,规则里点名一律 skip。"),
]

# 家底关键词 —— 出现在候选名里就说明"我们已经有了",判 adopt 即为家底盲区。
OWNED = ["paddleocr", "rapidocr", "tesseract", "ponytail", "litellm", "mineru"]
# 常驻主机特征词 —— 出现即表示要开机器,我们零常驻主机。
STANDING = ["database", "web server", "search engine", "cluster", "server",
            "graph database", "message queue", "kubernetes", "proxy"]


def score_one(q, verdict_obj):
    """给一题打分。**确定性**:只比对答案与必答字段,不问模型。

    【2026-08-06 第一版当场被自检打脸,记在这里】
      第一版是 0.6 判对 + 0.25「答出 current/beats」+ 0.15「没踩陷阱」,
      而后两档对 skip 题是**白送**(skip 不要求 current/beats、v!=adopt 就算没踩陷阱)。
      结果:一个"什么都判 skip"的傻瓜策略在 10skip/1adopt 的卷子上拿 **0.9227**。
      **退化策略是最优解 = 这卷子在教系统闭嘴。**
    改法两条:
      ① 类别平衡(现在 adopt 与 skip 数量相当);
      ② **白送分取消** —— 每一档都必须真答对才给,答不出就是答不出。
    并且 main() 会把三种傻瓜基线(全 skip / 全 adopt / 全 watch)一起打出来,
      任何一次"分数好看"都先跟基线比 —— 这是防自欺的常设仪表,不是一次性检查。
    """
    v = (verdict_obj or {}).get("verdict", "")
    got, detail = 0.0, []

    # ① verdict 对不对(0.6)。watch 当半对 —— 它没误导,但也没给出判断。
    if v == q["want"]:
        got += 0.60; detail.append("判对")
    elif v == "watch" and q["want"] == "skip":
        got += 0.25; detail.append("判成 watch(没误导但也没判断)")
    else:
        detail.append(f"判错({v}≠{q['want']})")

    # ② 说清楚了没有(0.25)。**两类都要求**,不再白送:
    #    · adopt → 必须答出「我们现在用什么 / 它强在哪」,否则就是同义词复读;
    #    · skip  → 必须答出 why(为什么不要),否则是随口否掉,学不到判据。
    #    **前提是 verdict 判对了** —— 第三次自检才发现的漏洞:在 adopt 题上判错成 skip,
    #      居然还能靠「说清了不要的理由」拿 0.25。**答错了却因为讲得好而得分**,
    #      这正是全判 skip 还能拿 0.625 的那 0.25 从哪来的。堵掉后傻瓜基线落到 0.5。
    if v != q["want"]:
        detail.append("判错,说明分不计")
    elif v == "adopt":
        cur = str((verdict_obj or {}).get("current") or "").strip()
        beat = str((verdict_obj or {}).get("beats") or "").strip()
        if len(cur) >= 4 and len(beat) >= 4:
            got += 0.25; detail.append("答出 current/beats")
        else:
            detail.append("adopt 却答不出 current/beats")
    else:
        why = str((verdict_obj or {}).get("why") or "").strip()
        if len(why) >= 6:
            got += 0.25; detail.append("说清了不要的理由")
        else:
            detail.append("否掉却说不出理由")

    # ③ 陷阱(0.15)。**只有带 probe 的题才有这一档可挣**,不带 probe 的题白送
    #    会重新养出退化策略,所以改成:无 probe 的题这 0.15 并入①的判对里。
    if q["probe"]:
        if v == "adopt":
            detail.append(f"踩中{q['probe']}陷阱")
        else:
            got += 0.15
    elif v == q["want"]:
        got += 0.15

    return round(got, 4), ";".join(detail)


def score_all(answers):
    """全卷总分 —— **按 want 类别宏平均**,不是всех题直接取均值。

    【2026-08-06 第二次自检才修对】把白送分收紧后,全判 skip 仍拿 0.8000,
      因为卷子是 11 skip / 4 adopt —— **类别不平衡**,直接取均值等于给多数类加权。
    当时有两条路:
      ① 编几道 adopt 题把类别配平 —— **否决**。每一题都必须是真发生过的判定,
         为了让分数好看去编题,等于伪造回归集,比分数难看严重得多。
      ② 改判分口径为**宏平均**(先算每个类别的平均分,再对类别求平均)——
         这是不平衡分类的标准做法,而且傻瓜策略天然落到 0.5 附近,正是我们要的天花板。
    取②。

    answers: [(题目, 判定对象)] 或与 EXAM 等长的判定对象列表
    """
    if answers and isinstance(answers[0], tuple):
        pairs = answers
    else:
        pairs = list(zip(EXAM, answers))
    from collections import defaultdict
    per = defaultdict(list)
    for q, obj in pairs:
        per[q["want"]].append(score_one(q, obj)[0])
    cls = {k: sum(v) / len(v) for k, v in per.items()}
    return round(sum(cls.values()) / len(cls), 4), {k: round(v, 4) for k, v in cls.items()}


def baselines():
    """三种傻瓜基线 —— 任何变体的分数必须先跟这三个比,比不过就是没学到东西。"""
    out = {}
    for name, obj in [("全判skip", {"verdict": "skip", "why": "不相关不相关"}),
                      ("全判adopt", {"verdict": "adopt", "current": "无", "beats": "无"}),
                      ("全判watch", {"verdict": "watch", "why": "再看看再看看"})]:
        s, _ = score_all([obj] * len(EXAM))
        out[name] = s
    return out


def exam_stats():
    from collections import Counter
    c = Counter(q["want"] for q in EXAM)
    p = Counter(q["probe"] for q in EXAM if q["probe"])
    return dict(total=len(EXAM), want=dict(c), probes=dict(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="打印考卷")
    a = ap.parse_args()
    st = exam_stats()
    print(f"雷达回归集 {SCORER_VERSION}:{st['total']} 题")
    print(f"  期望判定:{st['want']}")
    print(f"  陷阱分布:{st['probes']}")
    if a.show:
        for i, q in enumerate(EXAM, 1):
            print(f"\n{i:2d}. {q['repo']}  {q['stars']}★  → 应判 {q['want']}"
                  f"{'  [' + q['probe'] + ']' if q['probe'] else ''}")
            print(f"    {q['why']}")
    # 自检一:判分函数不许抖 —— 同一份答案跑两次必须同分
    fake = {"verdict": "skip", "why": "不相关不相关"}
    s1 = [score_one(q, fake)[0] for q in EXAM]
    s2 = [score_one(q, fake)[0] for q in EXAM]
    assert s1 == s2, "判分函数不确定!"

    # 自检二:**傻瓜基线不许及格** —— 这是防「进化学会闭嘴」的常设仪表
    bl = baselines()
    print("\n傻瓜基线(任何变体必须先赢过它们):")
    for k, v in bl.items():
        print(f"  {k:10s} {v:.4f}")
    top = max(bl.values())
    if top > 0.60:
        print(f"\n⚠️ 卷子有问题:最好的傻瓜策略拿 {top:.4f} —— 退化策略成了最优解,"
              f"进化只会学到那个。必须继续平衡类别 / 收紧白送分。")
        return 1
    print(f"\n✓ 判分自检通过:最好的傻瓜策略只有 {top:.4f},两次跑分一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
