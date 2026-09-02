#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给军火候选补一段**详细中文说明**,让创始人能判断"对我们有没有用"。

立此因(创始人 2026-09-02):
  「功能介绍详细一点,要不然容易被忽略」
  「好像我在今日头条看到的,就非常的详细,那我才可以思考对于我们有没有用」

问题:`candidates.json` 的 capability 字段常常只有五六个字(「Skill 展示柜」),
GitHub 原始 description 也只有一句英文。**信息量不够做判断**,好东西被一眼划过。

做法:抓每个候选仓的 README(前 4000 字),交给内部免费池网关写一段结构化中文详解:
  · 它到底做什么(具体到功能点,不是一句概括)
  · 怎么用(装/调/部署)
  · **对古方 AI 星图这个中医古籍平台有没有用、用在哪一步**  ← 这条最要紧
  · 有什么坑或前提(要 GPU?要付费 API?许可传染?)

红线:AI 调用**只走内部免费池网关**(与平台其它调用同一条链,零按量计费);
抓 README 只用 GitHub 公开 API;失败静默跳过,绝不阻断军火榜生成。
"""
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ARSENAL = os.path.join(ROOT, "reports", "arsenal")
CAND = os.path.join(ARSENAL, "candidates.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
GATEWAY = os.environ.get("GATEWAY_URL", "https://gufangai.com/api/gateway/chat")
GW_KEY = os.environ.get("GW_KEY", "")
MAX_ENRICH = int(os.environ.get("ENRICH_MAX", "30"))

PLATFORM = """古方 AI 星图 = 中医古籍 AI 平台。现有产线:
1) RAG检索:2100+医案 + 7700+古籍向量检索,讯飞embedding(绑死不可换)
2) 古籍OCR:几万本扫描书页,RapidOCR跑GitHub Actions,竖排/夹注识别差
3) 视频产线:Remotion+edge-tts 每日出专家口播视频,但**发布分发=0代码**
4) 图文产线:satori 渲方剂卡,缺自动发布
5) AI免费池:自建网关,9家免费模型容错链,零按量计费
硬约束:无服务器、本地禁算力、批量走 GitHub Actions、AI 只走内部免费池、
绝不换 embedding 供应商、GPL/AGPL 代码不能进闭源生产。"""

SYS = """你是古方 AI 星图平台的技术选型顾问。给你一个 GitHub 项目的 README,
写一段**详细的中文说明**,让平台负责人能判断值不值得用。

必须包含这四段(每段用「」标题开头,段与段之间换行):
「做什么」具体功能点,列 2-4 个,不要只写一句概括
「怎么用」装法/调法(pip? docker? 要不要服务器?)
「对我们有没有用」结合平台现状明确说:能用在哪条产线的哪一步,或者明确说"用不上,因为..."
「坑」前提与风险(要GPU? 要付费API? 许可传染? 依赖重?)

规则:总长 150-300 字;只依据 README 写,读不出来的就说"README 未说明",
**绝不编造**;判断要直接,该说"用不上"就说"用不上"。"""


def gh_readme(repo):
    """取仓库 README 正文。

    创始人 2026-09-02:「要用 GitHub 开源的爬虫啊,他们才是专业的」—— 说得对。
    两条路,优先走专业件:
      ① GitHub 官方 raw API 拿 README(结构化、最准,本来就是给机器读的接口)
      ② 拿不到时,用 **trafilatura**(6.7k★ Apache,专业正文提取)去抓仓库主页,
         把导航/侧栏/页脚剥掉只留正文 —— 比裸 urllib 抓整页 HTML 干净得多。
    (crawl4ai 也在本机,但它要起浏览器、重;README 这种静态文本用不上那么重的家伙。
     真需要动态渲染的站再上 crawl4ai。)
    """
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/readme" % repo,
        headers={"Accept": "application/vnd.github.raw", "User-Agent": UA,
                 **({"Authorization": "Bearer " + os.environ["GH_TOKEN"]}
                    if os.environ.get("GH_TOKEN") else {})})
    try:
        t = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "replace")
        if t.strip():
            return t[:4000]
    except Exception:                                            # noqa: BLE001
        pass
    # 退回专业正文提取
    try:
        import trafilatura
        page = urllib.request.urlopen(
            urllib.request.Request("https://github.com/" + repo,
                                   headers={"User-Agent": UA}), timeout=40).read().decode("utf-8", "replace")
        txt = trafilatura.extract(page, include_comments=False, favor_precision=True) or ""
        return txt[:4000]
    except Exception:                                            # noqa: BLE001
        return ""


def ask(prompt):
    """走内部免费池网关(红线:绝不用按量计费源)。"""
    body = json.dumps({
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": prompt}],
        "max_tokens": 900, "temperature": 0.3, "source": "arsenal_enrich",
    }).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": UA}
    if GW_KEY:
        hdrs["X-Gateway-Key"] = GW_KEY
    req = urllib.request.Request(GATEWAY, data=body, headers=hdrs, method="POST")
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=120).read())
        # 内部网关返回的是 {"ok":true,"text":...,"supplier":...},不是 OpenAI 的
        # choices[0].message.content —— 实测确认(2026-09-02),两种都兼容一下。
        c = j.get("text") or ""
        if not c:
            c = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return c.strip()
    except Exception as e:                                       # noqa: BLE001
        return "__ERR__" + str(e)[:80]


def main():
    if not os.path.isfile(CAND):
        print("arsenal_enrich: 找不到 candidates.json")
        return 1
    d = json.load(io.open(CAND, encoding="utf-8"))
    cands = d.get("candidates") or []
    todo = [c for c in cands if not c.get("detail_cn")][:MAX_ENRICH]
    print("arsenal_enrich: %d 个候选,本轮补 %d 个详解" % (len(cands), len(todo)))
    if not todo:
        print("  都已有详解,跳过")
        return 0

    ok = fail = 0
    for i, c in enumerate(todo, 1):
        rm = gh_readme(c["repo"])
        if not rm:
            print("  [%d/%d] %s README 抓不到,跳过" % (i, len(todo), c["repo"]))
            fail += 1
            continue
        prompt = ("%s\n\n项目:%s(%s★)\n描述:%s\n\nREADME:\n%s"
                  % (PLATFORM, c["repo"], c.get("stars"), c.get("description") or "", rm))
        out = ask(prompt)
        if out.startswith("__ERR__"):
            print("  [%d/%d] %s 网关失败:%s" % (i, len(todo), c["repo"], out[7:]))
            fail += 1
        else:
            c["detail_cn"] = out
            ok += 1
            print("  [%d/%d] %s ✓ %d字" % (i, len(todo), c["repo"], len(out)))
        time.sleep(1.0)          # 温和,别打爆免费池

    json.dump(d, io.open(CAND, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("arsenal_enrich: 成功 %d · 失败 %d,已写回 candidates.json" % (ok, fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
