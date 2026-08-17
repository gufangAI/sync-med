# -*- coding: utf-8 -*-
"""
eagle_fetch —— SueAI 鹰眼统一采集引擎 v1(创始人 2026-08-17:「把这几个爬虫变成1个,变成我们自己的」)

合成自四家原理(方法无版权;代码只搬 Apache-2.0 系):
  · Crawl4AI(Apache-2.0):双引擎按需升档 + antibot 三层特征判定(特征正则直接移植,见下注)
  · Jina Reader(Apache-2.0):auto 引擎切换思想 + r.jina.ai 作最外层兜底(白嫖其 IP 信誉)
  · Firecrawl(AGPL,零代码搬运):只学"先发现后抓取"与失败降级链的协议设计,全部自写
  · trafilatura(Apache-2.0):正文抽取核心(import 使用),readability 思想做兜底

四段解耦(三家共同的架构智慧):FETCH → CLEAN → EXTRACT → 产出计数
  FETCH 三级降级:tier1 httpx 轻档(零浏览器,Actions 秒级)
              → tier2 Playwright 重档(仅 Actions 内,本地禁算力铁律)
              → tier3 r.jina.ai 外部兜底(硬站白嫖 IP 信誉;实测 2026-08-17 过知乎 403)
  每级结果过 antibot 判定,命中特征才升档——宁误判不漏判(误判有下一级兜底,漏判=垃圾入库)。

哨兵基因(2026-08-16 绿勾零产出血案内置):fetch_page 返回结构永远带
  {tier, blocked_by, chars} —— 调用方零成本落产出计数,绿勾装死无处藏身。

antibot 特征库移植声明:_TIER1/_TIER2 正则源自 crawl4ai/antibot_detector.py
  (Apache-2.0 © unclecode & contributors,允许复制修改,保留本声明)。
"""
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import urllib.request
import urllib.error

try:
    import trafilatura
except ImportError:                       # Actions 里 pip install trafilatura>=1.8
    trafilatura = None

JINA_BASE = "https://r.jina.ai/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ── antibot 三层特征(移植自 crawl4ai,Apache-2.0)────────────────────────────
_TIER1 = [
    (re.compile(r"Reference\s*#\s*[\d]+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I), "Akamai"),
    (re.compile(r"Pardon\s+Our\s+Interruption", re.I), "Akamai challenge"),
    (re.compile(r"challenge-form.*?__cf_chl_f_tk=", re.I | re.S), "Cloudflare challenge"),
    (re.compile(r'<span\s+class="cf-error-code">\d{4}</span>', re.I), "Cloudflare firewall"),
    (re.compile(r"/cdn-cgi/challenge-platform/\S+orchestrate", re.I), "Cloudflare JS challenge"),
    (re.compile(r"window\._pxAppId\s*=", re.I), "PerimeterX"),
    (re.compile(r"captcha\.px-cdn\.net", re.I), "PerimeterX captcha"),
    (re.compile(r"captcha-delivery\.com", re.I), "DataDome captcha"),
    (re.compile(r"geo\.captcha-delivery\.com", re.I), "DataDome"),
    (re.compile(r"_Incapsula_Resource", re.I), "Imperva Incapsula"),
    (re.compile(r"kasada\.io|kpsdk", re.I), "Kasada"),
]
_TIER2 = [
    (re.compile(r"verify\s+you\s+are\s+(a\s+)?human", re.I), "generic human check"),
    (re.compile(r"access\s+denied", re.I), "generic access denied"),
    (re.compile(r"unusual\s+traffic", re.I), "generic unusual traffic"),
    (re.compile(r"请完成安全验证|安全验证|访问验证|滑动验证", re.I), "cn captcha"),
    (re.compile(r"系统检测到.{0,12}异常", re.I), "cn anomaly check"),
]


def detect_block(status: int, html: str) -> Optional[str]:
    """返回拦截方名称;None=未被拦。哲学:误判便宜(有下一级),漏判致命。"""
    if status in (403, 503) and html:
        for pat, name in _TIER1 + _TIER2:
            if pat.search(html):
                return name
        return f"HTTP {status}"
    if not html or len(html) < 512:
        for pat, name in _TIER1 + _TIER2:
            if html and pat.search(html):
                return name
        if status >= 400:
            return f"HTTP {status} thin page"
        return None
    for pat, name in _TIER1:                       # 大页也查结构性标记(静默拦截)
        if pat.search(html):
            return name
    return None


@dataclass
class FetchResult:
    url: str
    ok: bool
    tier: str            # http / browser / jina / none
    status: int
    html: str = ""
    text: str = ""       # trafilatura 清洗后的正文
    blocked_by: str = ""
    chars: int = 0       # 正文字数=产出计数(哨兵基因)
    elapsed_ms: int = 0
    notes: list = field(default_factory=list)


def _clean(html: str, url: str) -> str:
    if not html:
        return ""
    if trafilatura is not None:
        out = trafilatura.extract(html, url=url, include_comments=False,
                                  include_tables=True, favor_recall=True)
        if out and len(out) > 120:
            return out
    # 兜底:剥标签的朴素抽取(readability 思想的最简替身,宁可粗不可空)
    body = re.sub(r"(?is)<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return body if len(body) > 120 else ""


def _fetch_http(url: str, timeout: float) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try: body = e.read().decode("utf-8", errors="replace")
        except Exception: body = ""
        return e.code, body


def _fetch_jina(url: str, timeout: float, api_key: str = "") -> tuple:
    headers = {"User-Agent": "eagle-fetch/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(JINA_BASE + url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def fetch_page(url: str, *, timeout: float = 30.0, jina_key: str = "",
               browser_fetcher=None) -> FetchResult:
    """三级降级取数。browser_fetcher: 可注入的 tier2 回调(仅 Actions 内传入,本地禁算力)。
    返回结构自带产出计数字段——调用方必须把 chars 落台账(哨兵铁律)。"""
    t0 = time.time()
    res = FetchResult(url=url, ok=False, tier="none", status=0)

    # tier1 轻档
    try:
        status, html = _fetch_http(url, timeout)
        blocked = detect_block(status, html)
        if not blocked:
            text = _clean(html, url)
            if text:
                res.ok, res.tier, res.status, res.html, res.text = True, "http", status, html, text
                res.chars = len(text)
                res.elapsed_ms = int((time.time() - t0) * 1000)
                return res
            res.notes.append("http: fetched but empty after clean")
        else:
            res.notes.append(f"http blocked: {blocked}")
            res.blocked_by = blocked
    except Exception as e:
        res.notes.append(f"http error: {type(e).__name__}")

    # tier2 重档(仅当调用方注入了浏览器取数器——Actions 内才有)
    if browser_fetcher is not None:
        try:
            status, html = browser_fetcher(url, timeout)
            blocked = detect_block(status, html)
            if not blocked:
                text = _clean(html, url)
                if text:
                    res.ok, res.tier, res.status, res.html, res.text = True, "browser", status, html, text
                    res.chars = len(text)
                    res.elapsed_ms = int((time.time() - t0) * 1000)
                    return res
            else:
                res.notes.append(f"browser blocked: {blocked}")
                res.blocked_by = blocked
        except Exception as e:
            res.notes.append(f"browser error: {type(e).__name__}")

    # tier3 外部兜底(r.jina.ai:硬站吃它的IP信誉;返回已是markdown,不再走clean)
    try:
        status, md = _fetch_jina(url, timeout + 30, jina_key)
        if status == 200 and md and len(md) > 200:
            res.ok, res.tier, res.status, res.text = True, "jina", status, md.strip()
            res.chars = len(res.text)
            res.elapsed_ms = int((time.time() - t0) * 1000)
            return res
        res.notes.append(f"jina: HTTP {status} len={len(md or '')}")
    except Exception as e:
        res.notes.append(f"jina error: {type(e).__name__}")

    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


if __name__ == "__main__":
    import io, json, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    for u in sys.argv[1:]:
        r = fetch_page(u)
        print(json.dumps({"url": r.url, "ok": r.ok, "tier": r.tier, "status": r.status,
                          "chars": r.chars, "blocked_by": r.blocked_by, "ms": r.elapsed_ms,
                          "notes": r.notes, "head": r.text[:120]}, ensure_ascii=False))
