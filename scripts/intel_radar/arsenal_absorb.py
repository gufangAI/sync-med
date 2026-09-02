#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐个项目提取「它身上哪一个点我们能用」—— 全部过一遍,不筛选。

立此因(创始人 2026-09-02):
  「一定要都用起来!哪怕只是一个点,都可能是价值」
  「不是应该,你的判断总是错的!」

这两句推翻了此前的做法。此前 arsenal_category 的详解 prompt 里写着
「对我们有没有用…或者直说『用不上,因为…』」—— 那是让 **我(模型)去筛**,
而筛选本身就是瓶颈:我判断"用不上"的那一刻,这个项目就永远消失了,
没人会再去看它。创始人已经指出过两次同一个病(「你可以想到的有限」)。

所以这个脚本换一个前提:**默认每个项目都有可用之处,任务是把它找出来。**
不许答"用不上"。哪怕是一行正则、一个阈值、一个目录结构的做法,
也要具体说出是哪一个点、落在我们哪一步。

三条硬约束(防止它为了完成任务而胡编 —— 编出来的点比说"用不上"更坏):
  ① 只依据给定的信息写,信息不够就说"信息不足,需要读它的源码才能确定",
     **这不算"用不上",而是"待深读"** —— 会进 pending 队列下轮补
  ② 提取的点必须**具体**:能落到函数级/参数级/流程级,
     不许写"它的架构值得借鉴"这种没法执行的话
  ③ 必须写清落在我们哪条产线的哪一步

规模:候选池数千,全量跑一轮约半小时(并发 6,走内部免费额度网关)。
按分数降序跑,断点续跑,跑过的存进 absorb.json 不重复烧。

红线:AI 调用只走内部免费额度网关,零按量计费源。
"""
import io
import json
import os
import sys
import time
import importlib.util
import concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ARSENAL = os.path.join(ROOT, "reports", "arsenal")
OUT = os.path.join(ARSENAL, "absorb.json")

spec = importlib.util.spec_from_file_location("ac", os.path.join(HERE, "arsenal_category.py"))
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

PLATFORM = os.environ.get("PLATFORM_CONTEXT") or """一个古籍数字化 AI 平台,若干条产线:
1) 向量检索线   2) 扫描件 OCR 线   3) 视频产线   4) 图文产线
5) 自建模型网关(只走免费额度,零按量计费)   6) 开源情报挖掘线
运行约束:无服务器(边缘函数 / CI runner)、本地禁算力无 GPU、
AI 调用只走内部免费池、检索向量供应商已锁定不可更换。
(具体产线数字与卡点走 PLATFORM_CONTEXT 环境变量注入,不写进公开仓 —— 
 2026-09-02 血证:六条产线的卡点、家底数字、技术绑定曾被我完整写进 PUBLIC 仓注释。)"""

SYS = """你是本平台的技术吸收官。给你一个开源项目,
你的任务是找出**它身上哪一个点我们能用**。

前提(这条是硬的):**默认每个项目都有可用之处,你的活是把它找出来,不是判断它有没有用。**
创始人原话:「一定要都用起来,哪怕只是一个点,都可能是价值」。
所以**不许回答"用不上"**。

输出严格三行,每行以标签开头,不要多写:
可用点：<一句话说清是哪一个具体的点>
怎么用：<落到我们哪条产线的哪一步,具体到函数级/参数级/流程级>
把握度：<高|中|低>

「可用点」的合格标准 —— 要具体到能动手:
  合格:"它按 Retry-After 头做退避而不是固定 sleep,这个判据可以搬进我们网关的冷却逻辑"
  合格:"它把 PDF 先分类成扫描版/文字版再走不同管线,这个前置路由能省掉我们大量无效 OCR"
  合格:"它的 SKILL.md 目录结构,可以照着把我们的古籍知识打包成 agent 技能"
  不合格:"它的架构值得借鉴"(没法动手)
  不合格:"可以用于内容生成"(太泛)

如果给你的信息实在看不出具体的点,「可用点」写:
  信息不足，需读源码确定
**注意这不等于"用不上"**,只是这轮没读到源码。把握度写"低"。

绝不编造:不许说它有你没看到的功能;不许把我们已有的技术
(平台自有技术栈,见 PLATFORM_CONTEXT)说成是它的。
总长不超过 160 字。"""


_UA = "gufang-absorb/1.0"


def readme(repo, cap=5000):
    """抓 README 正文。

    **这一步决定产出是不是空话。** 只喂 description(一句英文营销语)时,
    模型只能凭书名猜,实测硬凑出「AIPex 的浏览器自动化 → 用于网关路由探活」
    这种毫不相干的话 —— 浏览器自动化和模型路由没有任何关系。
    读过 README 之后它才有依据。

    抓不到就返回空串,调用方照常走(那时产出应是"信息不足,需读源码"),
    **绝不因为抓不到 README 就跳过这个项目** —— 跳过就等于又在筛选。
    """
    import urllib.request
    h = {"Accept": "application/vnd.github.raw", "User-Agent": _UA}
    tok = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = "Bearer " + tok
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/readme" % repo, headers=h)
        return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")[:cap]
    except Exception:                                            # noqa: BLE001
        return ""


def one(r):
    rm = readme(r["repo"]) if os.environ.get("ABSORB_READ_README", "1") == "1" else ""
    p = ("%s\n\n项目:%s\n星数:%s  语言:%s\ntopics:%s\n描述:%s\n补充:%s"
         % (PLATFORM, r["repo"], r.get("stars"), r.get("lang") or "未知",
            ", ".join(str(t) for t in (r.get("topics") or []))[:220],
            (r.get("desc") or "")[:320], (r.get("capability") or "")[:200]))
    if rm:
        p += ("\n\nREADME(前 5000 字。**判断必须依据这段,不许凭项目名猜**):\n" + rm)
    else:
        p += ("\n\n(README 抓不到 —— 这种情况「可用点」必须写"
              "「信息不足，需读源码确定」,不许凭名字硬猜)")
    return r["repo"], ac.ask_gateway(p, SYS, timeout=60)


def parse(txt):
    """把三行拆成结构。解析失败不丢内容 —— 整段塞进 point,
    宁可格式难看也不要静默丢掉一条真结论。"""
    d = {"point": "", "how": "", "conf": ""}
    if not txt:
        return d
    for ln in txt.split("\n"):
        s = ln.strip()
        for key, lab in (("point", "可用点"), ("how", "怎么用"), ("conf", "把握度")):
            for sep in ("：", ":"):
                if s.startswith(lab + sep):
                    d[key] = s[len(lab) + 1:].strip()
    if not d["point"]:
        d["point"] = txt.strip()[:160]
    return d


def main():
    limit = int(os.environ.get("ABSORB_LIMIT", "60"))
    workers = int(os.environ.get("ABSORB_WORKERS", "6"))

    pool = ac.load_pool()
    done = {}
    if os.path.isfile(OUT):
        try:
            done = json.load(io.open(OUT, encoding="utf-8")).get("absorbed") or {}
        except Exception:                                        # noqa: BLE001
            done = {}

    # 顺序:**先吃对口的,不是先吃星多的**。
    #
    # 第一版按星数降序,实测代价立刻显现:前 80 个全是 freeCodeCamp(编程教学网站)、
    # build-your-own-x(教程集合)、awesome(资源清单)这类巨型仓 —— 它们身上没有
    # 技术可吃,而 prompt 又不许答"用不上",模型只好硬凑,写出
    # 「把 freeCodeCamp 嵌入图文产线提供编程学习资源」这种对中医平台毫无意义的话。
    #
    # 硬凑的根因不在"不许说用不上"这条要求上,在**喂给它的东西本来就不对口**。
    # mined.json 里已经有 score(相关性打分,见 arsenal_mine.score),直接用它排序,
    # 先吃 firecrawl / crawl4ai 这类真有技术的。星数只作同分时的次级排序。
    #
    # 排序确定 + 断点续跑 = 每个项目迟早都轮得到,只是对口的先轮到,
    # 不对口的排在后面 —— 这跟"全部都要吃"不冲突,是先后问题不是取舍问题。
    todo = [r for r in pool.values()
            if r.get("repo") and r["repo"] not in done and r.get("stars") is not None]
    todo.sort(key=lambda x: (-(x.get("score") or 0), -(x.get("stars") or 0)))
    todo = todo[:limit]

    print("吸收官:候选池 %d · 已吃 %d · 本轮吃 %d(并发 %d)"
          % (len(pool), len(done), len(todo), workers), flush=True)
    if not todo:
        print("  本轮无新项目", flush=True)
        return 0

    ok = thin = fail = 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (repo, txt) in enumerate(ex.map(one, todo), 1):
            if not txt:
                fail += 1
                print("  [%d/%d] %s 网关无返回" % (i, len(todo), repo[:40]), flush=True)
                continue
            d = parse(txt)
            d["repo"] = repo
            done[repo] = d
            if "信息不足" in d["point"]:
                thin += 1
                print("  [%d/%d] %s ○ 待深读" % (i, len(todo), repo[:40]), flush=True)
            else:
                ok += 1
                print("  [%d/%d] %s ✓ %s" % (i, len(todo), repo[:40], d["point"][:52]),
                      flush=True)

    if not os.path.isdir(ARSENAL):
        os.makedirs(ARSENAL)
    json.dump({"absorbed": done, "total": len(done)},
              io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n本轮:找到可用点 %d · 待深读 %d · 失败 %d · 耗时 %.0f 秒"
          % (ok, thin, fail, time.time() - t0), flush=True)
    print("累计已吃 %d / 候选池 %d(%.1f%%)· 落盘 %s"
          % (len(done), len(pool), 100.0 * len(done) / max(1, len(pool)), OUT), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
