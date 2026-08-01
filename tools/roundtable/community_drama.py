#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百家论道·评论区吵架引擎 v1(2026-08-01)
给研讨帖生成大白话多角色评论 + 回复链对喷。角色 = 学派分身 × 社区人设。
用法: python community_drama.py <post_id> "<议题>"  (不带参数=对 post/91 试跑)
插 community_comments(带 parent_id 回复链)+ bump comment_count。全大白话、禁文言/脏话/剂量。
"""
import sys, os, re, time, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from roundtable import gateway_chat, PANELS_ALL, sql_escape, wrangler_file, OUT_DIR, _run_wrangler

# 社区人设(随机套在学派分身上):(标签, 气质)
ROLES = [
    ("较真辩手", "站你本派立场认真讲道理,摆本派的逻辑硬刚,不服就辩,但讲的是理不是喊口号"),
    ("经验分享", "结合临床或读书遇到的实际情况聊,'我碰到过类似的病''书上还真有这么一说',朴实"),
    ("真诚提问", "虚心请教,'那请问XX情况下咋办''这个跟XX有啥区别呢',问得实在不抬杠"),
    ("冷静分析", "不站队,理性掰扯双方各自的道理和适用边界,像个想明白的人"),
    ("经方务实", "只认疗效和实证,'管它啥派,治好病才是真的''有是证用是药别绕'"),
    ("挑事拱火", "点一下火'某派看了要跳脚''这话谁听了不服',但别太频繁"),
    ("吃瓜起哄", "偶尔看个热闹起个哄,但别老是'前排搬板凳蹲后续'那一套"),
    ("杠精", "抬杠要证据要真实医案,'光讲道理谁不会,拿病历来看看'"),
    ("和事佬", "打圆场,'各有各的理,还得看具体病人具体情况'"),
]
DABAIHUA = ("用现代大白话、口语,像真实网友刷评论,70-130字。**风格自然、别套路化**:该好好讲理就讲理、"
            "该分享经验就朴实说、该提问就实在问——**别一开口就'前排吃瓜/搬板凳/蹲后续',所有人一个味就假了**。"
            "绝不文言掉书袋(不许'吾辈/此乃/也/矣/之乎者')。禁脏话、禁人身攻击、不开药方不写剂量、不做诊疗建议。"
            "只输出评论这一句,无前缀、无引号、无分析说明。")

def gen_top(panel, role, topic):
    sysp = (f"你在中医论坛研讨帖评论区。你是「{panel['school']}」分身,人设:{panel['persona'][:60]}。"
            f"你此刻的评论气质:{role[1]}。\n{DABAIHUA}")
    txt, _ = gateway_chat([{"role": "system", "content": sysp},
                           {"role": "user", "content": f"帖子在吵:{topic}\n发一条你的评论:"}],
                          temperature=0.92, max_tokens=280)
    return (txt or "").strip()

def gen_reply(panel, role, topic, parent_who, parent_text):
    sysp = (f"你在中医论坛评论区。你是「{panel['school']}」分身,人设:{panel['persona'][:60]}。"
            f"你此刻的气质:{role[1]}。楼上有人评论了,你**回复怼他/接他的话/拱火**。\n{DABAIHUA}")
    txt, _ = gateway_chat([{"role": "system", "content": sysp},
                           {"role": "user", "content": f"议题:{topic}\n【{parent_who}】说:{parent_text}\n你回复他,发一条:"}],
                          temperature=0.95, max_tokens=280)
    return (txt or "").strip()

def insert_comment(post_id, sign, content, parent_id=None):
    token = os.environ.get("CF_D1_TOKEN")
    if token:  # Actions:CF D1 REST 参数化 + RETURNING 拿 comment_id(中文/引号无坑)
        import urllib.request, json as _json
        from roundtable import ACCOUNT_ID, DB_ID
        sql = ("INSERT INTO community_comments (post_id, author_id, author_name, content, status, parent_id, created_at) "
               "VALUES (?,?,?,?,'published',?,strftime('%s','now')) RETURNING comment_id")
        params = [int(post_id), 'ai_persona', f"{sign}(AI学派分身)", content, (int(parent_id) if parent_id else None)]
        url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
        body = _json.dumps({"sql": sql, "params": params}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                d = _json.loads(resp.read().decode("utf-8"))
            rows = (d.get("result") or [{}])[0].get("results") or []
            return rows[0].get("comment_id") if rows else True
        except Exception as e:
            print("  ✗ 评论REST异常:", str(e)[:120]); return None
    pid_sql = str(int(parent_id)) if parent_id else "NULL"
    sql = ("INSERT INTO community_comments (post_id, author_id, author_name, content, status, parent_id, created_at) "
           f"VALUES ({int(post_id)}, 'ai_persona', '" + sql_escape(f"{sign}(AI学派分身)") + "', '"
           + sql_escape(content) + f"', 'published', {pid_sql}, strftime('%s','now'));")
    p = os.path.join(OUT_DIR, "_dc.sql")
    open(p, "w", encoding="utf-8").write(sql)
    ok, _ = wrangler_file(p)
    if not ok:
        return None
    # wrangler --file 不返回 RETURNING,单独查全表 MAX(comment_id)=刚插入的(单进程顺序执行,可靠)
    r = _run_wrangler('--command "SELECT MAX(comment_id) AS id FROM community_comments"')
    m = re.search(r'"id":\s*(\d+)', (r.stdout or "") + (r.stderr or ""))
    return int(m.group(1)) if m else True

def bump(post_id, n):
    token = os.environ.get("CF_D1_TOKEN")
    if token:  # Actions:REST
        import urllib.request, json as _json
        from roundtable import ACCOUNT_ID, DB_ID
        sql = "UPDATE community_posts SET comment_count=comment_count+?, updated_at=strftime('%s','now') WHERE post_id=?"
        url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
        body = _json.dumps({"sql": sql, "params": [int(n), int(post_id)]}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:
            print("  bump REST异常:", str(e)[:100])
        return
    p = os.path.join(OUT_DIR, "_dcbump.sql")
    open(p, "w", encoding="utf-8").write(
        f"UPDATE community_posts SET comment_count=comment_count+{int(n)}, updated_at=strftime('%s','now') WHERE post_id={int(post_id)};")
    wrangler_file(p)

def sign_of(panel):
    return panel["sign"].split(" · ")[0]

def run(post_id, topic):
    pool = list(PANELS_ALL)
    random.shuffle(pool)
    n_top = random.randint(4, 6)
    tops = pool[:n_top]
    posted = []
    print(f"=== 帖 {post_id} · {topic[:30]} · 目标 {n_top} 条顶楼 ===")
    for panel in tops:
        role = random.choice(ROLES)
        txt = gen_top(panel, role, topic)
        if not txt:
            continue
        cid = insert_comment(post_id, sign_of(panel), txt)
        if cid:
            posted.append((cid, sign_of(panel), txt))
            print(f"  楼 {sign_of(panel)}[{role[0]}] {len(txt)}字 (id={cid})")
        time.sleep(0.4)
    n_reply = min(random.randint(2, 3), len(posted))
    repliers = pool[n_top:n_top + n_reply + 2]
    done_r = 0
    for i in range(n_reply):
        if not posted or i >= len(repliers):
            break
        target = random.choice(posted)
        panel = repliers[i]
        if sign_of(panel) == target[1]:
            continue
        role = random.choice(ROLES)
        txt = gen_reply(panel, role, topic, target[1], target[2])
        if not txt:
            continue
        cid = insert_comment(post_id, sign_of(panel), txt, parent_id=target[0])
        if cid:
            done_r += 1
            print(f"    ↳回复@{target[1]} ← {sign_of(panel)}[{role[0]}] {len(txt)}字")
        time.sleep(0.4)
    total = len(posted) + done_r
    if total:
        bump(post_id, total)
    print(f"=== 完成:{len(posted)} 顶楼 + {done_r} 回复 = {total} 条 ===")

def auto_pick_and_run(limit=3):
    # Actions 自动:挑评论少的已发布研讨帖,给它们生成大白话吵架评论
    token = os.environ.get("CF_D1_TOKEN")
    from roundtable import ACCOUNT_ID, DB_ID
    import urllib.request, json as _json
    sql = ("SELECT post_id, title FROM community_posts WHERE author_id='ai_roundtable' "
           "AND status='published' AND comment_count < 4 ORDER BY RANDOM() LIMIT ?")
    url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
    body = _json.dumps({"sql": sql, "params": [int(limit)]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        d = _json.loads(resp.read().decode("utf-8"))
    rows = (d.get("result") or [{}])[0].get("results") or []
    print(f"=== 自动模式:挑到 {len(rows)} 帖待评论 ===")
    for row in rows:
        topic = (row.get("title") or "").replace("百家论道 · ", "")
        run(int(row["post_id"]), topic)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(int(sys.argv[1]), sys.argv[2])                # 手动指定帖
    elif os.environ.get("CF_D1_TOKEN"):
        auto_pick_and_run()                                # Actions:自动挑评论少的帖生成吵架
    else:
        run(91, "外感高热不退,该先解表、先清里,还是表里双解?(火神派主张真寒假热用姜附,寒凉派主张清里热用石膏)")  # 本地试跑
