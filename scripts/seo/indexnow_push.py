#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IndexNow 主动推送 —— 让 Bing/Yandex/Seznam 几分钟内收录,不用等爬虫排队。

立此因(2026-09-01 实测):平台 SEO/GEO 基础其实很扎实——bot-ssr 真生效(Googlebot/
百度/GPTBot/ClaudeBot/PerplexityBot 都拿到完整 SSR)、JSON-LD 有、llms.txt 有、
sitemap 82,598 个 URL。**技术层唯一缺口 = 没有任何主动推送**:8 万页全靠爬虫自己
慢慢排队爬,新站权重低时可能几个月都爬不完。

IndexNow 是免费开放协议(Bing/Yandex/Seznam/Naver 共用一个提交池,提交一次全收):
POST 一批 URL → 几分钟内被抓。无需注册、无需 API key 审批,只要域名根目录能访问
<key>.txt 且内容等于 key 本身即可(已放 guyaofang-web/public/<key>.txt)。

Google 不支持 IndexNow(它只认 Search Console),但 Bing 系 + 国内部分聚合仍值得推。

每次推一批(协议建议单次 ≤10000 条),从 sitemap 取 URL。默认只推核心内容页
(方剂/阅读器/全文),不推列表页——列表页爬虫自己会来,名额留给长尾内容页。
"""
import os
import sys
import io
import json
import re
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HOST = os.environ.get("INDEXNOW_HOST", "www.gufangai.com")
KEY = os.environ.get("INDEXNOW_KEY", "")
SITEMAP = os.environ.get("SITEMAP_URL", "https://www.gufangai.com/sitemap.xml")
BATCH = int(os.environ.get("INDEXNOW_BATCH", "5000"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
# 只推内容页(长尾价值高);列表/首页爬虫自己会来
CONTENT_PAT = re.compile(r"/(fangji|reader|text-reader|daodu)/")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def collect_urls():
    """从 sitemap(索引→子图)收集内容页 URL。"""
    root = fetch(SITEMAP)
    subs = re.findall(r"<loc>([^<]+sitemap-\d+\.xml)</loc>", root)
    if not subs:
        subs = [SITEMAP]
    out = []
    for s in subs:
        try:
            body = fetch(s)
        except Exception as e:                                   # noqa: BLE001
            print("  ! 子图取失败 %s: %s" % (s, str(e)[:80]))
            continue
        locs = re.findall(r"<loc>([^<]+)</loc>", body)
        picked = [u for u in locs if CONTENT_PAT.search(u)]
        print("  %s: %d 个 URL,其中内容页 %d" % (s.rsplit("/", 1)[-1], len(locs), len(picked)))
        out.extend(picked)
    return out


def push(urls):
    """一批推给 IndexNow(api.indexnow.org 会分发给所有参与引擎)。"""
    payload = json.dumps({
        "host": HOST,
        "key": KEY,
        "keyLocation": "https://%s/%s.txt" % (HOST, KEY),
        "urlList": urls,
    }).encode()
    req = urllib.request.Request("https://api.indexnow.org/IndexNow", data=payload,
                                 headers={"Content-Type": "application/json; charset=utf-8",
                                          "User-Agent": UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:200] if e.fp else "")
    except Exception as e:                                       # noqa: BLE001
        return 0, str(e)[:200]


def main():
    if not KEY:
        print("indexnow_push: 缺 INDEXNOW_KEY(需与 https://%s/<key>.txt 内容一致)→ 跳过" % HOST)
        return 0
    # 先自证 key 文件可访问(协议硬要求;不通就别浪费提交)
    try:
        got = fetch("https://%s/%s.txt" % (HOST, KEY)).strip()
        if got != KEY:
            print("indexnow_push: key 文件内容不等于 key(拿到前40字: %r)→ 先部署 public/<key>.txt" % got[:40])
            return 1
        print("key 文件校验通过: https://%s/%s.txt" % (HOST, KEY))
    except Exception as e:                                       # noqa: BLE001
        print("indexnow_push: key 文件不可访问(%s)→ 先部署" % str(e)[:80])
        return 1

    urls = collect_urls()
    print("内容页共 %d 个,按 %d 一批推送" % (len(urls), BATCH))
    if not urls:
        print("无内容页可推(sitemap 里没有 /fangji/ 等内容页?)")
        return 0
    sent = ok = 0
    for i in range(0, len(urls), BATCH):
        chunk = urls[i:i + BATCH]
        code, msg = push(chunk)
        sent += len(chunk)
        if code in (200, 202):
            ok += len(chunk)
            print("  [批%d] %d 条 → HTTP %d ✓" % (i // BATCH + 1, len(chunk), code))
        else:
            print("  [批%d] %d 条 → HTTP %d %s" % (i // BATCH + 1, len(chunk), code, msg[:120]))
    print("IndexNow 推送完:提交 %d / 成功 %d 条。Bing/Yandex 通常几分钟到几小时内抓取。" % (sent, ok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
