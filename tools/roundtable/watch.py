#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百家论道内容工厂 · 产帖健康哨兵(2026-08-01 平台CTO)
查最近 N 小时 AI 研讨帖新增数,停滞则输出 stalled=true(GitHub Actions 据此写 Issue)。
铁律:自动产线的关键缺口数字必须上升成哨兵/Issue,不能只躺 log 没人看。
Actions 里跑,走 CF D1 REST(env CF_D1_TOKEN)。
"""
import os, sys, io, json, urllib.request

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
DB_ID = "2db89d3b-e988-4577-a9e3-fb7c563af72f"
WINDOW_HOURS = int(os.environ.get("WATCH_WINDOW_HOURS", "3"))
MIN_EXPECTED = int(os.environ.get("WATCH_MIN_EXPECTED", "2"))   # 窗口内至少应新增几篇

def d1_query(sql):
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
    body = json.dumps({"sql": sql}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['CF_D1_TOKEN']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return d["result"][0]["results"]

def main():
    win = WINDOW_HOURS * 3600
    recent = d1_query(
        f"SELECT COUNT(*) n FROM community_posts WHERE author_id='ai_roundtable' "
        f"AND created_at > strftime('%s','now') - {win}")[0]["n"]
    total = d1_query(
        "SELECT COUNT(*) n FROM community_posts WHERE author_id='ai_roundtable' AND status='published'")[0]["n"]
    comments = d1_query("SELECT COUNT(*) n FROM community_comments")[0]["n"]

    print(f"[哨兵] 近 {WINDOW_HOURS}h 新增 {recent} 篇 · 累计 {total} 帖 · {comments} 评论")

    out = os.environ.get("GITHUB_OUTPUT")
    if recent < MIN_EXPECTED:
        print(f"::warning::内容工厂产帖停滞——近 {WINDOW_HOURS}h 仅新增 {recent} 篇(期望≥{MIN_EXPECTED})")
        if out:
            with open(out, "a") as f:
                f.write("stalled=true\n")
                f.write(f"recent={recent}\ntotal={total}\n")
    else:
        print("[哨兵] 产帖健康 ✓")
        if out:
            with open(out, "a") as f:
                f.write("stalled=false\n")

if __name__ == "__main__":
    main()
