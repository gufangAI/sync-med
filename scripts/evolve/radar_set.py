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

SCORER_VERSION = "radar-v1-2026-08-06"


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

    # ── 丙类:真该采纳的(adoption.txt 里真落地了的)──────────────
    #   这一类必须够多。第一版只放了 1 题,自检当场抓到:
    #   **一个"什么都判 skip"的傻瓜策略拿 0.9227** —— 卷子是废的,
    #   进化只会学到"一律说 skip"。类别必须平衡,否则退化策略就是最优解。
    dict(repo="Graphify-Labs/graphify", stars=2100, lang="Python",
         desc="Turn any codebase or corpus into a knowledge graph. Community detection, "
              "surprise scoring, standalone HTML export. Pure library, no server required.",
         want="adopt", probe="",
         why="**真落地了**:吸收其意外分与社群摘要思路,上线 /api/graph/overview 与 "
             "/api/graph/insights,线上实测 8 个知识区覆盖 46560 节点、32 条跨区桥接。"
             "纯库、无常驻服务 —— 这正是我们能吃下的形态。"),
    dict(repo="calesthio/OpenMontage", stars=1200, lang="Python",
         desc="Automatic video montage from clips. ffmpeg-based named transitions "
              "(xfade), beat-synced cuts, no GPU required.",
         want="adopt", probe="",
         why="**真落地了**(adoption.txt 2026-07-21):吸收其 ffmpeg xfade 具名转场,"
             "接进 make_video.py 的 render_body_clips()。纯脚本 + ffmpeg,Actions 里跑得动。"),
    dict(repo="dreammis/social-auto-upload", stars=4600, lang="Python",
         desc="Automatically upload videos to douyin, xiaohongshu, bilibili, tiktok. "
              "CLI tool, MIT licensed, cookie-based auth, no server.",
         want="adopt", probe="",
         why="**真落地了**(adoption.txt 2026-07-29):当内容工厂 → 抖音/小红书的投递适配器。"
             "CLI 形态,不需要常驻。"),
    # 【2026-08-06 出题当天就撤掉的一题 —— 留痕,别再犯】
    #   我曾把 BerriAI/litellm 标成 want="skip" / probe="already_have",理由写「已在家底」。
    #   跑完基线去核实,全仓 grep 只命中三处:模型自己的答案缓存、`land.py` 里**我自己写的举例**、
    #   和一个叫 `sk-litellm-local-dev` 的 key 名字。**litellm 根本没在我们系统里用。**
    #   我把「我写的举例」当成了家底证据 —— 正是「记忆是线索不是真相」那条铁律的原样重犯。
    #   判定大脑判它 adopt(理由:替换现有网关层)不是错,**是我标错了答案**。
    #   回归集里一道错答案比没有回归集更糟:它会把雷达往错的方向训。
    #   所以撤题,不改标签硬留 —— 我手上没有它的真实判决,而**每一题都必须是真发生过的判定**。
    dict(repo="opendatalab/MinerU", stars=25000, lang="Python",
         desc="A high-quality tool for convert PDF to Markdown and JSON. "
              "Layout detection, formula recognition, table extraction.",
         want="adopt", probe="",
         why="**仍是候选**(_stack.py 家底闸实测返回「仍是候选」)。纯库形态、"
             "能在 Actions 里跑,而我们 OCR 产线正缺版面/表格结构化 —— 这题的正解是 adopt。"),

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
