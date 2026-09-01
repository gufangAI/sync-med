#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""寻脉 embedding 缓存预热 —— 让热门查询"首次"也秒回。

立此因(2026-09-01):寻脉检索的 embedding 缓存已上线(重复查询 19.6s→0.47s·40倍),
但"首次"查一个词仍要等讯飞 embedding(唯一活 key·慢·20-30s)。预热=提前把最常见的
查询(高频方名 + 常见症状白话)灌一遍,填满缓存,用户第一次查热门词就命中缓存、秒回。

做法:POST 常见查询到生产 /api/retrieve(公开端点·无需密钥),每次触发 embedText→写 KV 缓存。
纯读+填缓存,零副作用;跑一次管一个月(缓存 TTL 30天)。走 Actions 云端,不占本机。
慢是正常的(每条首次要等讯飞),这正是预热要替用户提前承受的那一次慢。
"""
import os
import sys
import io
import json
import time
import urllib.request
import urllib.error

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ENDPOINT = os.environ.get("RETRIEVE_URL", "https://gufangai.com/api/retrieve")

# 常见症状/白话(用户最可能直接打的话)——寻脉入口高频词
SYMPTOMS = [
    "咳嗽痰多", "失眠多梦", "头痛", "怕冷发热", "往来寒热", "胸胁苦满",
    "口苦咽干", "食欲不振", "腹泻", "便秘", "心悸", "气短乏力",
    "自汗盗汗", "腰膝酸软", "月经不调", "眩晕", "耳鸣", "水肿",
    "胃痛", "呕吐", "黄疸", "咽喉肿痛", "关节疼痛", "小便不利",
]


def d1_top_formulas(k):
    """从 D1 取高频真方名(有密钥时);无密钥→空,只用症状词。"""
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        return []
    sql = ("SELECT name_norm FROM sue_formulas WHERE name_norm IS NOT NULL "
           "AND length(name_norm) BETWEEN 3 AND 8 AND is_formula=1 "
           "GROUP BY name_norm ORDER BY COUNT(*) DESC LIMIT %d" % k)
    url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (acc, db)
    req = urllib.request.Request(url, data=json.dumps({"sql": sql}).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
                                 method="POST")
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=45).read())
        return [r["name_norm"] for r in (j["result"][0]["results"] if j.get("success") else [])]
    except Exception as e:                                       # noqa: BLE001
        print("D1 取方名失败,只用症状词: %s" % str(e)[:100])
        return []


def warm(query):
    """POST 一次到 /api/retrieve → 触发 embedText → 写缓存。返回 (ok, cost_ms)。"""
    body = json.dumps({"query": query}).encode()
    # ⚠ 必带正常 UA:Cloudflare WAF 对 Python-urllib 默认 UA 直接 1010 封禁(实测),curl/浏览器 UA 才放行。
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }, method="POST")
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return True, r.get("cost_ms")
    except Exception as e:                                       # noqa: BLE001
        return False, str(e)[:60]


def main():
    top = int(os.environ.get("PREWARM_TOP", "40"))
    queries = list(dict.fromkeys(SYMPTOMS + d1_top_formulas(top)))   # 去重保序
    print("预热 %d 条(症状 %d + 方名 %d)→ %s" % (len(queries), len(SYMPTOMS), len(queries) - len(SYMPTOMS), ENDPOINT), flush=True)
    ok = fail = 0
    t0 = time.time()
    for i, q in enumerate(queries, 1):
        good, info = warm(q)
        if good:
            ok += 1
            print("  [%d/%d] %s · %sms" % (i, len(queries), q, info), flush=True)
        else:
            fail += 1
            print("  [%d/%d] %s · 失败:%s" % (i, len(queries), q, info), flush=True)
        time.sleep(0.5)   # 温和,别把唯一活 key 打爆
    print("预热完:成功 %d · 失败 %d · 耗时 %.0fs。这批查询现已在缓存,用户首查秒回(TTL 30天)。" % (ok, fail, time.time() - t0))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
