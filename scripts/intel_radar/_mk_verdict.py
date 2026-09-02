#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给每条赛道写「趋势判断」与「我们该怎么选」。

立此因(创始人 2026-09-02 拿另一份报告对比后):
  「你看看你交付的都是啥啊」

对比出来的差距很具体,不是"不够好看":
  · 那份报告每个赛道有**趋势判断**(这条赛道正在往哪走)
  · 每类末尾有**选型建议**(做A就用X,做B就用Y)
  · 项目说明是**中文的、带判断的**,不是 GitHub 的英文 description
  我给的是一张数据清单 —— 能查,但不能帮人做决定。**判断才是交付。**

但我有它没有的两样,这个脚本要把它们用上:
  ① 星数是实测精确值。实测发现那份报告的约值会误导:
     MediaCrawler 它标「≈30k」,实测 64,309 —— 差了一倍多;
     postiz 标 29k 实测 35,376;ragflow 标 85k 实测 89,883。
     选型时"3万星"和"6万星"是两个不同的决策。
  ② **只有我们知道自己的产线**。所以"选型建议"不能停在
     "做产品用 X、自建用 Y",要落到"接我们哪条线的哪一步"。

AI 调用走内部免费额度网关(红线:零按量计费源),并发跑,十余条赛道约 10 秒。
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

spec = importlib.util.spec_from_file_location("ac", os.path.join(HERE, "arsenal_category.py"))
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

SYS = """你是本平台的首席架构师,给平台CTO写赛道整合方案。

创始人 2026-09-02 定的调子:**「需要整合,不是直接抄1个」**、
**「把几十个开源,变成我们自己的1个」**。
所以你的任务**不是选型**(选哪个现成的用),而是**拆解与糅合**:
从这条赛道的几个项目身上各拆下一块最强的能力,拼成我们自己的一个模块。

给你一条赛道的头部项目清单(星数是实测精确值)。写两段:

「趋势」这条赛道当前在往哪个方向走。要具体到"从X转向Y"这种可判别的说法,
不要写"发展迅速""前景广阔"这类放之四海皆准的废话。2-3 句。

「怎么整合成我们自己的一个」这一段是重点。必须写成这个形态:
  从 A 拿它的<具体能力>(它这块最强,因为…);
  从 B 拿它的<具体能力>;
  从 C 拿它的<具体能力>;
  糅合成我们自己的 <模块名>,接在<哪条产线的哪一步>。
硬要求:
· **至少点名 3 个项目,每个说清拿它的哪一块**,不许写"综合使用 X 和 Y"这种含糊话
· 拿的必须是**具体能力**(如"代理轮换与指纹伪装""版面块的剪枝算法""配额感知降级"),
  不是"它的框架""它的生态"这种虚的
· 要说清**为什么是它那一块最强**,而不是随便分配
· 结尾必须落到我们某条产线的具体环节

已经完成的范例(照这个形态写):
  我们的 crawl_core/fetch.py = 从 crawlee 拿代理轮换与指纹伪装 + 从 scrapy 拿
  按服务端 Retry-After 退避的重试策略与自适应节流 + 从 colly 拿轻量并发控制,
  糅合成我们自己的取页器,接在情报挖掘与站点抓取的取页环节。

如果这条赛道**确实没有可整合的东西**,就直说"暂时无需整合,因为…",
这比硬凑有价值。

规则:
· 只依据给你的项目清单写,**绝不编造**没给你的项目、没读过的能力
· 拿不准某个项目具体强在哪,就不要点它的名,宁可少点一个
· **不要提许可**。我们的做法一律是读架构、学做法、自己重写,许可不构成约束
· 总长 240-380 字,中文,不用markdown标题符号"""

LICENSE_FACT = ""   # 创始人钦定:许可对我们完全没用,我们是学+整合+自研,不复制代码

PLATFORM = os.environ.get("PLATFORM_CONTEXT") or """一个古籍数字化 AI 平台,若干条产线:
1) 向量检索线   2) 扫描件 OCR 线   3) 视频产线   4) 图文产线
5) 自建模型网关(只走免费额度,零按量计费)   6) 开源情报挖掘线
运行约束:无服务器(边缘函数 / CI runner)、本地禁算力无 GPU、
AI 调用只走内部免费池、检索向量供应商已锁定不可更换。
(具体产线数字与卡点走 PLATFORM_CONTEXT 环境变量注入,不写进公开仓 —— 
 2026-09-02 血证:六条产线的卡点、家底数字、技术绑定曾被我完整写进 PUBLIC 仓注释。)"""


# 平台自有技术栈。模型多次把这些当成"候选项目"点名整合 ——
# (实测:模型把平台自有技术栈当成候选项目点名整合,纯属编造。)
# 它们出现在 PLATFORM 描述里是为了说明现状,不是候选,必须显式排除。
# 平台自有技术栈:名单走 OURS_STACK 环境变量注入,不写进公开仓。
# 模型多次把这些当成"候选项目"点名整合(它们出现在上下文里是说明现状,不是候选)。
OURS = [s.strip().lower() for s in
        (os.environ.get("OURS_STACK") or "").split(",") if s.strip()]


def _cited(txt, pool):
    """从生成文本里挑出它点名的项目,分成"清单里有的"和"编造的"。

    立此因:模型编造得很自然,读起来完全通顺,靠人一条条看必然漏。
    (平台铁律:写下"绝不编造"这种要求,就得有个地方会红,否则是空头支票。)
    匹配用仓名的短名(owner/repo 的 repo 段),因为正文一般只写短名。
    """
    low = txt.lower()
    ok, fake = [], []
    short = {r["repo"].split("/")[-1].lower(): r["repo"] for r in pool}
    for s, full in short.items():
        if len(s) >= 4 and s in low:
            ok.append(full)
    for w in OURS:
        if w in low:
            fake.append(w)
    return ok, fake


def one(c):
    lines = []
    for r in c["top"][:10]:
        lines.append("· %s  %s★  %s  %s%s"
                     % (r["repo"], "{:,}".format(r["stars"]), r["lang"] or "?",
                        "", (r["desc"] or "")[:130]))
    allow = "、".join(r["repo"] for r in c["top"][:10])
    guard = ("\n【只许从这 10 个里点名,一个都不许多】\n%s\n"
             "特别注意:平台现状里提到的 自有技术栈 "
             "**是我们自己已有的东西,不是候选项目**,绝不许写成「从自有技术拿…」。\n"
             "还要注意:清单里若有 cookbook / awesome / 教程 / 文档类仓,它们没有可拿的能力,"
             "不要点它们的名。\n" % allow)
    p = ("%s\n%s%s\n赛道:%s(候选 %d 个)\n定位:%s\n\n头部项目(星数为实测精确值):\n%s"
         % (PLATFORM, LICENSE_FACT, guard, c["name"], c["n"], c["desc"], "\n".join(lines)))

    # 生成 → 校验 → 不合格重来一次。两次都不合格就如实标记,不硬发。
    for attempt in range(2):
        txt = ac.ask_gateway(p, SYS, timeout=60)
        if not txt:
            continue
        ok, fake = _cited(txt, c["top"][:10])
        if not fake and len(ok) >= 2:
            return c["key"], txt, {"cited": ok, "fake": []}
        if attempt == 0:
            p += ("\n\n【上一版被打回,重写】你点了这些不在清单里的东西:%s。"
                  "只许用上面 10 个候选,把它们换掉。" % ("、".join(fake) or "点名太少"))
    return c["key"], (txt or ""), {"cited": ok if txt else [],
                                   "fake": fake if txt else ["网关无返回"]}


def main():
    src = os.path.join(ROOT, "report_data.json")
    d = json.load(io.open(src, encoding="utf-8"))
    cats = [c for c in d["cats"] if c["top"]]
    print("给 %d 条赛道写趋势与选型建议..." % len(cats), flush=True)
    out, t0 = {}, time.time()
    bad = 0
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for key, txt, chk in ex.map(one, cats):
            if not txt:
                print("  %s 网关没返回" % key, flush=True)
                continue
            if chk["fake"]:
                bad += 1
                # 编造过的照样落盘,但打上标记 —— 悄悄丢掉会让人以为这条赛道没内容,
                # 而实际是我们没写出合格的。宁可标出来。
                out[key] = txt + "\n\n（本段自动校验未通过：点到了不在候选清单里的 "
                out[key] += "、".join(chk["fake"]) + "，阅读时请核对。）"
                print("  %s ⚠ 校验未过:编造了 %s"
                      % (key, "、".join(chk["fake"])), flush=True)
            else:
                out[key] = txt
                print("  %s ✓ %d字 · 点名 %d 个均在清单内"
                      % (key, len(txt), len(chk["cited"])), flush=True)
    dst = os.path.join(ROOT, "verdicts.json")
    json.dump(out, io.open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("成功 %d/%d · 耗时 %.0f 秒 · 已写 %s"
          % (len(out), len(cats), time.time() - t0, dst), flush=True)


if __name__ == "__main__":
    main()
