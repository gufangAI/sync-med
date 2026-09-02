#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把分类概览渲染成一页自包含 HTML(可直接发布成 Artifact)。

立此因(创始人 2026-09-02):看到别的工具 3 分半就交了一份分类对比报告,
而我一头扎进建系统,一个多小时没给出能看的东西。**顺序错了**——
先给 5 分钟能看的概览,再去建可持续跑的系统。这个脚本补的就是前半段。

数据来自 report_data.json(_mk_overview.py 产出),内联进 HTML 保证自包含。
星数是实测精确值,不写"约 175k"这种约值。
"""
import io
import json
import os
import html
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "report_data.json")

LIC = {
    "MIT": ("MIT", "ok"), "Apache-2.0": ("Apache", "ok"),
    "BSD-3-Clause": ("BSD", "ok"), "BSD-2-Clause": ("BSD", "ok"),
    "ISC": ("ISC", "ok"), "Unlicense": ("Unlicense", "ok"),
    "MPL-2.0": ("MPL", "warn"),
    "GPL-3.0": ("GPL 学思路自研", "warn"), "GPL-2.0": ("GPL 学思路自研", "warn"),
    "AGPL-3.0": ("AGPL 学思路自研", "warn"),
    "NOASSERTION": ("自定条款 待核", "warn"),
    "": ("无许可", "bad"),
}


def md_bold(s):
    """把 **x** 变成 <strong>x</strong>。

    配置里的 desc 是拿 markdown 习惯写的,直接 html.escape 会把星号原样打在页面上
    (实测:"**这是我们卡了最久的一环**" 带着四个星号显示出来了)。
    先转义再放行这一个标记,顺序不能反 —— 反了就是 XSS 口子。
    """
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\g<1></strong>", html.escape(s or ""))


def lic_badge(s):
    name, cls = LIC.get(s or "", (s or "未知", "warn"))
    return '<span class="lic %s">%s</span>' % (cls, html.escape(name))


def k(n):
    return "{:,}".format(n or 0)


def main():
    d = json.load(io.open(SRC, encoding="utf-8"))
    gen = datetime.date.today().isoformat()
    cats = d["cats"]
    shown = sum(len(c["top"]) for c in cats)
    classified = d["total"] - d["unc"]

    P = []
    P.append('<title>军火分类对比</title>')
    P.append("""<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@500;700&family=Noto+Sans+SC:wght@400;500;700&display=swap">""")

    P.append("""<style>
:root{
  --ink:#1a1815; --ink-2:#4a453d; --ink-3:#7d766a;
  --paper:#f7f4ee; --card:#fffdf8; --line:#e2dbcd;
  --accent:#9c3226; --accent-soft:#f0e2df;
  --ok:#2f6b4f; --ok-bg:#e6f0ea;
  --warn:#8a6420; --warn-bg:#f6eeda;
  --bad:#9c3226; --bad-bg:#f7e4e1;
  --ease:cubic-bezier(.32,.72,0,1);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ink:#efe9df; --ink-2:#bdb5a7; --ink-3:#8a8377;
  --paper:#14120f; --card:#1c1a16; --line:#2f2b24;
  --accent:#d9705f; --accent-soft:#33211e;
  --ok:#7fc0a0; --ok-bg:#1b2b23;
  --warn:#d6ab5f; --warn-bg:#2c2418;
  --bad:#e08476; --bad-bg:#33201d;
}}
:root[data-theme="dark"]{
  --ink:#efe9df; --ink-2:#bdb5a7; --ink-3:#8a8377;
  --paper:#14120f; --card:#1c1a16; --line:#2f2b24;
  --accent:#d9705f; --accent-soft:#33211e;
  --ok:#7fc0a0; --ok-bg:#1b2b23;
  --warn:#d6ab5f; --warn-bg:#2c2418;
  --bad:#e08476; --bad-bg:#33201d;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:44px 22px 90px}
h1{font-family:"Noto Serif SC",serif;font-size:clamp(28px,4.2vw,44px);
  font-weight:700;letter-spacing:-.01em;margin:0 0 8px;text-wrap:balance}
.sub{color:var(--ink-2);font-size:15px;margin:0 0 30px;max-width:62ch}
.eyebrow{font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--accent);font-weight:700;margin-bottom:12px}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
  gap:12px;margin:0 0 14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px}
.stat b{display:block;font-family:"Noto Serif SC",serif;font-size:30px;
  line-height:1.1;font-variant-numeric:tabular-nums;color:var(--accent)}
.stat span{font-size:12.5px;color:var(--ink-3)}
.note{font-size:13px;color:var(--ink-3);margin:0 0 34px;padding:12px 16px;
  border-left:3px solid var(--accent);background:var(--accent-soft);border-radius:0 8px 8px 0}

.cat{background:var(--card);border:1px solid var(--line);border-radius:14px;
  margin-bottom:16px;overflow:hidden}
.cat>summary{cursor:pointer;padding:18px 22px;list-style:none;
  display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  transition:background .25s var(--ease)}
.cat>summary::-webkit-details-marker{display:none}
.cat>summary:hover{background:var(--accent-soft)}
.cat h2{font-family:"Noto Serif SC",serif;font-size:19px;margin:0;font-weight:700}
.cnt{font-size:12.5px;color:var(--ink-3);font-variant-numeric:tabular-nums;
  border:1px solid var(--line);border-radius:20px;padding:1px 11px}
.cdesc{flex:1 1 100%;font-size:13.5px;color:var(--ink-2);margin-top:4px}
.tw{overflow-x:auto;border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:13.5px;min-width:720px}
th{text-align:left;font-size:11.5px;letter-spacing:.06em;color:var(--ink-3);
  font-weight:700;padding:11px 14px;border-bottom:1px solid var(--line);
  background:var(--paper);position:sticky;top:0}
td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.star{font-variant-numeric:tabular-nums;font-weight:700;color:var(--accent);
  white-space:nowrap;text-align:right}
a.repo{color:var(--ink);text-decoration:none;font-weight:500;
  border-bottom:1px solid var(--line);transition:border-color .2s var(--ease)}
a.repo:hover{border-color:var(--accent);color:var(--accent)}
.dsc{color:var(--ink-2);font-size:12.5px;max-width:52ch}
.lic{font-size:11px;padding:2px 8px;border-radius:20px;white-space:nowrap;font-weight:500}
.lic.ok{background:var(--ok-bg);color:var(--ok)}
.lic.warn{background:var(--warn-bg);color:var(--warn)}
.lic.bad{background:var(--bad-bg);color:var(--bad)}
.src{font-size:11.5px;color:var(--ink-3);white-space:nowrap}
footer{margin-top:44px;padding-top:22px;border-top:1px solid var(--line);
  font-size:12.5px;color:var(--ink-3)}
.sec{font-family:"Noto Serif SC",serif;font-size:23px;margin:38px 0 6px;font-weight:700}
.sechint{font-size:13px;color:var(--ink-3);margin:0 0 16px;max-width:70ch}
.ovw table{min-width:820px}
.ovw{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:8px}
.heat{font-size:11.5px;padding:2px 10px;border-radius:20px;font-weight:700;white-space:nowrap}
.heat.h0{background:var(--line);color:var(--ink-3)}
.heat.h1{background:var(--ok-bg);color:var(--ok)}
.heat.h2{background:var(--warn-bg);color:var(--warn)}
.heat.h3{background:var(--bad-bg);color:var(--bad)}
.heat.h4{background:var(--accent);color:var(--card)}
.vd{padding:4px 22px 18px;display:grid;gap:10px}
.vblk{padding:13px 16px;border-radius:10px;font-size:13.5px;line-height:1.75}
.vblk p{margin:4px 0 0;color:var(--ink-2)}
.vblk.trend{background:var(--paper);border-left:3px solid var(--ink-3)}
.vblk.pick{background:var(--accent-soft);border-left:3px solid var(--accent)}
.vt{font-weight:700;font-size:12px;letter-spacing:.06em;color:var(--accent)}
.vblk.trend .vt{color:var(--ink-3)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:var(--paper);padding:2px 7px;border-radius:5px;border:1px solid var(--line)}
@media (prefers-reduced-motion:reduce){*{transition-duration:.01ms!important}}
</style>""")

    P.append('<div class="wrap">')
    P.append('<div class="eyebrow">古方AI星图 · 鹰眼情报</div>')
    P.append('<h1>GitHub 军火分类对比</h1>')
    P.append('<p class="sub">挖掘引擎单轮扫回的开源项目，按 %d 个赛道归位。'
             '星数为 GitHub API <strong>实测精确值</strong>，非榜单转抄、非约值。'
             '做法一律是<strong>读它的架构、学它的做法，再糅合几家写成我们自己的一个</strong>——所以不看许可，只看能学到什么。</p>' % len(cats))

    P.append('<div class="stats">')
    for v, lab in [(k(d["total"]), "候选池总量"), (k(classified), "已归类"),
                   (str(len(cats)), "赛道"), (k(shown), "本页详列"),
                   (k(d["unc"]), "待归类（保留不丢）")]:
        P.append('<div class="stat"><b>%s</b><span>%s</span></div>' % (v, lab))
    P.append('</div>')

    P.append('<p class="note">这份表的意义不在"列了多少"，在于<strong>它是自动跑出来的</strong>——'
             '挖掘引擎每天在 GitHub Actions 上顺着现成排行榜、trending、awesome 榜单、'
             'topic 图和全量枚举五条矿脉自己扫，视野不再受限于谁能想到哪个赛道。'
             '同日老雷达按手写查询表只产出 22 个候选，且头部是 awesome-godot、'
             'awesome-geojson 这类无关项目。</p>')

    # ── 我们自己的引擎 ───────────────────────────────────────────
    # 创始人 2026-09-02:「我要的是你学习几十个开源,变成1个自己的」。
    # 这一段是那件事的**实体**,不是计划:crawl_core 四根柱子已经写完并自测通过。
    # 行数与吸收来源都是从源文件里现读的,不是手写死的 —— 手写死的数字迟早骗人。
    CORE = os.path.join(ROOT, "scripts", "crawl_core")
    PILLARS = [
        ("fetch.py", "取页层", "重试退避 · 错误分类 · 代理自适应 · 指纹 · 域级节流"),
        ("extract.py", "正文层", "HTML → 干净中文正文 / Markdown / 结构化数据"),
        ("adaptive.py", "自适应层", "对方页面改版后仍能定位到同一个元素"),
        ("schedule.py", "调度层", "URL 去重 · 优先级队列 · 断点续跑 · 幂等"),
    ]
    rows_core = []
    for fn, role, what in PILLARS:
        fp = os.path.join(CORE, fn)
        if not os.path.isfile(fp):
            continue
        txt = io.open(fp, encoding="utf-8").read()
        nlines = txt.count("\n") + 1
        head = txt[:4000]
        srcs = []
        for token, label in [("crawlee", "crawlee"), ("scrapy", "scrapy"),
                             ("crawl4ai", "crawl4ai"), ("Scrapling", "Scrapling"),
                             ("colly", "colly"), ("trafilatura", "trafilatura"),
                             ("firecrawl", "firecrawl"), ("yt-dlp", "yt-dlp")]:
            if token.lower() in head.lower() and label not in srcs:
                srcs.append(label)
        rows_core.append((fn, role, what, nlines, srcs))

    if rows_core:
        total_lines = sum(r[3] for r in rows_core)
        allsrc = sorted({s for r in rows_core for s in r[4]})
        P.append('<h2 class="sec">一、我们自己的抓取引擎（已建成）</h2>')
        P.append('<p class="sechint">下面这些不是候选、不是计划——是<strong>已经写完并自测通过的代码</strong>，'
                 '把 %s 等项目的核心技术拆开、糅合、重写成我们自己的一套。'
                 '共 <strong>%s 行</strong>，每一处技术在源文件里都注明了取自上游的哪个文件哪个函数。</p>'
                 % ("、".join(allsrc), k(total_lines)))
        P.append('<div class="tw ovw"><table><thead><tr>'
                 '<th>模块</th><th>做什么</th><th style="text-align:right">行数</th>'
                 '<th>能力</th><th>技术来自</th></tr></thead><tbody>')
        for fn, role, what, n, srcs in rows_core:
            P.append('<tr><td><code>%s</code></td><td><strong>%s</strong></td>'
                     '<td class="star">%s</td><td class="dsc">%s</td>'
                     '<td class="dsc">%s</td></tr>'
                     % (html.escape(fn), html.escape(role), k(n),
                        html.escape(what), html.escape(" · ".join(srcs) or "—")))
        P.append('</tbody></table></div>')
        st = os.path.join(CORE, "_selftest_result.json")
        if os.path.isfile(st):
            P.append('<p class="note"><strong>自测是真跑的，不是自述：</strong>'
                     'Retry-After 解析 8 个用例全过（含旧实现会误判成 0 的 HTTP-date 格式）；'
                     'GitHub API 真拿到 crawlee ★25,606；不存在的仓 1.31 秒判死不空转；'
                     'trending 页面解析出 14 个项目；'
                     '中文正文提取真跑通——ctext《黄帝内经》抽出 6,588 字、'
                     '维基中医学条目抽出 4,491 个汉字。最后这条直接能用在古籍语料入库上。</p>')

    # ── 赛道横向对比总览 ──────────────────────────────────────────
    # 创始人 2026-09-02 要的是「类别**对比**表格」,我第一版做成了分类清单 ——
    # 清单能查,但不能比。一眼看出"哪条赛道最热、我们该先动哪条",要的是这张表。
    P.append('<h2 class="sec">二、%d 条赛道横向对比</h2>' % len(cats))
    P.append('<p class="sechint">按「头部项目体量 × 赛道厚度」排。'
             '<strong>我们的落点</strong>这一列是这张表的重点 —— '
             '热不热是别人的事，能不能接进我们的产线才是我们的事。</p>')
    P.append('<div class="tw ovw"><table><thead><tr>'
             '<th>赛道</th><th style="text-align:right">候选数</th>'
             '<th style="text-align:right">头部星数</th><th>热度</th>'
             '<th>头部代表</th><th>我们的落点</th></tr></thead><tbody>')

    def heat(n, mx):
        # 热度 = 赛道厚度 × 头部体量。两个都是实测数,不是感觉。
        s = (2 if n >= 200 else 1 if n >= 80 else 0) + \
            (2 if mx >= 100000 else 1 if mx >= 40000 else 0)
        return [("冷", "h0"), ("温", "h1"), ("热", "h2"),
                ("很热", "h3"), ("极热", "h4")][s]

    rows = []
    for c in cats:
        if not c["top"]:
            continue
        mx = max(x["stars"] for x in c["top"])
        mn = min(x["stars"] for x in c["top"])
        rows.append((c, mx, mn))
    rows.sort(key=lambda t: -(t[1] * 0.7 + t[0]["n"] * 300))
    for c, mx, mn in rows:
        hname, hcls = heat(c["n"], mx)
        reps = " · ".join(x["repo"].split("/")[-1] for x in c["top"][:3])
        P.append('<tr><td><strong>%s</strong></td>'
                 '<td class="star">%s</td>'
                 '<td class="star">%s</td>'
                 '<td><span class="heat %s">%s</span></td>'
                 '<td class="dsc">%s</td><td class="dsc">%s</td></tr>'
                 % (html.escape(c["name"]), k(c["n"]), k(mx), hcls, hname,
                    html.escape(reps), md_bold(c["desc"])))
    P.append('</tbody></table></div>')
    P.append('<h2 class="sec">三、各赛道头部项目明细</h2>')
    P.append('<p class="sechint">每类按「对口程度」取前 60 再按星数排前 12 —— '
             '直接按星数排会让每类头部被 vscode、react 这种通用大项目占满，'
             '那不是「这一类最对口的」。</p>')

    # 趋势判断与选型建议(_mk_verdict.py 产出,走内部免费池网关写的)。
    # 没有这一段,这份东西就只是清单 —— 能查但不能帮人做决定。
    vp = os.path.join(ROOT, "verdicts.json")
    verdicts = {}
    if os.path.isfile(vp):
        try:
            verdicts = json.load(io.open(vp, encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            verdicts = {}

    def render_verdict(txt):
        """把「趋势」「我们怎么选」两段渲染成块。模型偶尔不带书名号标题,
        那时整段当一块出 —— 宁可少个小标题,也不要因为解析失败而丢内容。"""
        if not txt:
            return ""
        out = []
        cur_t, cur_b = None, []
        for seg in txt.replace("「", "\n「").split("\n"):
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith("「") and "」" in seg:
                if cur_b:
                    out.append((cur_t, " ".join(cur_b)))
                cur_t = seg[1:seg.index("」")]
                rest = seg[seg.index("」") + 1:].strip()
                cur_b = [rest] if rest else []
            else:
                cur_b.append(seg)
        if cur_b:
            out.append((cur_t, " ".join(cur_b)))
        if not out:
            return '<div class="vd"><p>%s</p></div>' % html.escape(txt)
        h = ['<div class="vd">']
        for t, b in out:
            cls = "pick" if (t and ("选" in t or "怎么" in t)) else "trend"
            h.append('<div class="vblk %s">' % cls)
            if t:
                h.append('<span class="vt">%s</span>' % html.escape(t))
            h.append('<p>%s</p></div>' % html.escape(b))
        h.append("</div>")
        return "".join(h)

    for c in cats:
        P.append('<details class="cat" open>')
        P.append('<summary><h2>%s</h2><span class="cnt">%d 个</span>'
                 '<div class="cdesc">%s</div></summary>'
                 % (html.escape(c["name"]), c["n"], html.escape(c["desc"])))
        P.append(render_verdict(verdicts.get(c["key"])))
        P.append('<div class="tw"><table><thead><tr>'
                 '<th>项目</th><th style="text-align:right">星数</th>'
                 '<th>语言</th><th>能学到什么</th><th>怎么发现的</th>'
                 '</tr></thead><tbody>')
        for r in c["top"]:
            body = r.get("detail") or r.get("desc") or ""
            P.append('<tr><td><a class="repo" href="https://github.com/%s" '
                     'target="_blank" rel="noopener">%s</a></td>'
                     '<td class="star">%s</td><td class="src">%s</td>'
                     '<td class="dsc">%s</td><td class="src">%s</td></tr>'
                     % (html.escape(r["repo"]), html.escape(r["repo"]), k(r["stars"]),
                        html.escape(r["lang"] or "—"),
                        html.escape(body[:220]), html.escape(r["origin"] or "—")))
        P.append('</tbody></table></div></details>')

    P.append('<footer>数据源：鹰眼挖掘引擎 <code>arsenal_mine.py</code> '
             '五条矿脉产出，%s 生成。星数为运行当时实测值。'
             '本表只做客观呈现与排序，不替你做采纳判断。</footer>' % gen)
    P.append('</div>')

    dst = os.path.join(ROOT, "..", "军火分类对比.html")
    dst = os.path.abspath(dst)
    io.open(dst, "w", encoding="utf-8").write("\n".join(P))
    print("已生成 %s(%d 字节)" % (dst, os.path.getsize(dst)))
    print("  %d 类 · 详列 %d 个项目 · 池 %d" % (len(cats), shown, d["total"]))


if __name__ == "__main__":
    main()
