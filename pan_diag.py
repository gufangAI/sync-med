# -*- coding: utf-8 -*-
"""取图链路诊断 · 只打印，不做任何 OCR

立此因（2026-08-16）：同一时刻本地调 123 全部 code=0，云端 runner 全部
`401 tokens number has exceeded the limit`。两次归因（有效期写死 / 缓存未复用）
都被自己推翻，第三次不许再猜——把每一步的真实状态打出来。
"""
import os, sys, io, time, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 60)
print("① 环境变量到位情况（只看有没有，不打印值）")
for k in ("PAN_CLIENT_ID","PAN_CLIENT_SECRET","S_EP","S_AK","S_SK","S_BUCKET"):
    v = os.environ.get(k)
    print(f"   {k:20s} {'有' if v else '❌ 缺'}  len={len(v) if v else 0}")

import pan_fetch
print("\n② R2 共享缓存通不通")
c, bk = pan_fetch._s3()
print(f"   _s3(): client={'有' if c else '❌ None'}  bucket={bk!r}  TOK_KEY={pan_fetch.TOK_KEY}")
if c:
    try:
        o = c.get_object(Bucket=bk, Key=pan_fetch.TOK_KEY)
        d = json.loads(o["Body"].read().decode())
        left = (float(d.get("exp", 0)) - time.time()) / 86400
        print(f"   缓存对象存在: token 尾4=...{str(d.get('v',''))[-4:]}  剩 {left:.1f} 天")
    except Exception as e:
        print(f"   ❌ 读缓存对象失败: {str(e)[:120]}")
    got = pan_fetch._shared_token_get()
    print(f"   _shared_token_get(): {'命中 ...'+got[0][-4:] if got else '❌ 未命中'}")

print("\n③ token() 走的是哪条路")
t0 = time.time()
tok = pan_fetch.token()
print(f"   拿到 token 尾4=...{tok[-4:]}  耗时 {time.time()-t0:.2f}s")
print(f"   （耗时 <0.5s 基本是缓存；>1s 多半是真去申请了）")

print("\n④ 用它调业务接口——这才是 401 暴露的地方")
import requests
h = {"Platform": "open_platform", "Authorization": "Bearer " + tok}
for name, fid in (("根目录", 0), ("上次失败的目录", 30660140)):
    try:
        r = requests.get(pan_fetch.PAN + "/api/v2/file/list",
                         params={"parentFileId": fid, "limit": 2}, headers=h, timeout=60).json()
        print(f"   {name:16s} code={r.get('code')} msg={str(r.get('message'))[:60]}")
    except Exception as e:
        print(f"   {name:16s} 异常 {str(e)[:80]}")

print("\n⑤ 出口 IP（判断是不是环境差异）")
try:
    print("   ", requests.get("https://api.ipify.org?format=json", timeout=20).json())
except Exception as e:
    print("   取不到:", str(e)[:60])
print("=" * 60)
