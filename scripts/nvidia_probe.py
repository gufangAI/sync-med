#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NVIDIA 全量模型探针 —— 把「列出的」和「真能调的」分开

立此因(2026-08-02 创始人):「NVIDIA 有 140+ 模型,你接入 1 个就认为你很伟大了」。
查真实记录:一把 key 的 /models 列 **102 个**,我们 2026-07-31 只**逐个实打了 14 个**,
5 个真能返回,然后就停了 —— **剩下 88 个从来没人测过**。这不是"NVIDIA 只有 5 个能用",
是"我们只测了 14%"。本脚本把 102 个全测一遍,拿真数字说话。

它做什么:
  1. GET /v1/models 拿全量清单
  2. 每个模型打同一道中医题(可判对错,不是"能返回就算通")
  3. 记录:HTTP 状态 / 延迟 / 是否答对 / 错误原因
  4. --write-d1 时把**实测通过的**写进 model_registry(task=batch)
     ★ task=batch 而非 chat —— 因为已实测:NVIDIA 经 CF Worker 出网走不通
       (49s 超时,CDN 边缘按共享 IP 拒),只在本地/Actions 生效。
       写进 batch 池供离线编译/赛马用,**绝不进 web 容错链**。

铁律:
  · 只用免费 credits,不开任何付费档
  · 不动 chat 链,不影响线上任何用户路径
  · 探测失败如实记录,不美化

用法(Actions 里):
  NVIDIA_API_KEY=... python scripts/nvidia_probe.py --write-d1
本地:
  NVIDIA_API_KEY=... python scripts/nvidia_probe.py --limit 20
"""
import os, sys, json, time, uuid, argparse, urllib.request, urllib.error

BASE = "https://integrate.api.nvidia.com/v1"
KEY = (os.environ.get("NVIDIA_API_KEY")
       or os.environ.get("NVIDIA_NIM_API_KEY")
       or (os.environ.get("NVIDIA_KEYS", "").split(",")[0].strip())
       or (os.environ.get("NVIDIA_API_KEYS", "").split(",")[0].strip()))

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB = os.environ.get("D1_DATABASE_ID", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
D1_TOKEN = os.environ.get("D1_API_TOKEN", "")

# 判对判错的题:少阳证提纲,答案要点明确、不易蒙对
PROBE_Q = "《伤寒论》少阳病的提纲证有哪三个主要症状?只列举,不要解释。"
EXPECT = ["口苦", "咽干", "目眩"]


def http(url, method="GET", body=None, timeout=45):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None,
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def d1(sql):
    if not D1_TOKEN:
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"sql": sql}).encode("utf-8"),
        headers={"Authorization": "Bearer " + D1_TOKEN, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=90).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:200])
    return (j.get("result") or [{}])[0].get("results") or []


def probe(model_id):
    """返回 (ok, 延迟ms, 命中数, 错误)"""
    t0 = time.time()
    try:
        r = http(f"{BASE}/chat/completions", "POST", {
            "model": model_id,
            "messages": [{"role": "user", "content": PROBE_Q}],
            "max_tokens": 120, "temperature": 0.1,
        })
        ms = int((time.time() - t0) * 1000)
        txt = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
        hit = sum(1 for k in EXPECT if k in txt)
        return True, ms, hit, None
    except urllib.error.HTTPError as e:
        return False, int((time.time() - t0) * 1000), 0, f"HTTP{e.code}"
    except Exception as e:
        return False, int((time.time() - t0) * 1000), 0, type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=999)
    ap.add_argument("--write-d1", action="store_true")
    args = ap.parse_args()

    if not KEY:
        sys.exit("缺 NVIDIA_API_KEY / NVIDIA_NIM_API_KEY / NVIDIA_KEYS")

    print("[1/3] 拉全量模型清单 ...", flush=True)
    try:
        lst = http(f"{BASE}/models").get("data") or []
    except Exception as e:
        sys.exit(f"拉清单失败: {e}")
    ids = [m.get("id") for m in lst if m.get("id")]
    print(f"      /models 列出 {len(ids)} 个\n", flush=True)

    ids = ids[:args.limit]
    print(f"[2/3] 逐个实打(题:少阳提纲证,要点 {EXPECT}) —— 共 {len(ids)} 个\n", flush=True)

    ok_rows, bad_rows = [], []
    for i, mid in enumerate(ids, 1):
        ok, ms, hit, err = probe(mid)
        if ok:
            mark = "✓答对" if hit >= 2 else ("△能调但答偏" if hit >= 1 else "○能调答错")
            print(f"  {i:>3}/{len(ids)} {mid:<58} {ms:>6}ms  {mark}", flush=True)
            ok_rows.append({"id": mid, "ms": ms, "hit": hit})
        else:
            print(f"  {i:>3}/{len(ids)} {mid:<58} {ms:>6}ms  ✗{err}", flush=True)
            bad_rows.append({"id": mid, "err": err})

    ok_rows.sort(key=lambda x: (-x["hit"], x["ms"]))
    good = [r for r in ok_rows if r["hit"] >= 2]

    print("\n" + "=" * 76)
    print(f"清单 {len(ids)}  ·  能调 {len(ok_rows)}  ·  能调且答对 {len(good)}  ·  调不通 {len(bad_rows)}")
    print("=" * 76)
    print("\n【能调且答对】按质量+速度排:")
    for r in good[:40]:
        print(f"  {r['hit']}/3  {r['ms']:>6}ms  {r['id']}")

    from collections import Counter
    if bad_rows:
        print("\n【调不通的原因分布】")
        for e, n in Counter(r["err"] for r in bad_rows).most_common():
            print(f"  {e:<12} {n} 个")

    out = {"probed_at": int(time.time()), "listed": len(ids),
           "callable": len(ok_rows), "correct": len(good),
           "ok": ok_rows, "bad": bad_rows}
    with open("nvidia_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n结果已存 nvidia_probe_result.json")

    if args.write_d1 and good:
        if not D1_TOKEN:
            print("⚠️ 缺 D1_API_TOKEN,跳过写库")
            return
        now = int(time.time())
        vals = []
        for r in good:
            mk = "batch/nvidia:" + r["id"].replace("'", "")[:80]
            mid = r["id"].replace("'", "")
            vals.append(f"('{mk}','nvidia','{mid}','{mid}','candidate','batch',1,1,"
                        f"'探针实测 {r['hit']}/3 · {r['ms']}ms',{now},{now})")
        d1("INSERT OR REPLACE INTO model_registry (model_key,provider,model_id,display_name,"
           "role,task,is_free,enabled,notes,created_at,updated_at) VALUES " + ",".join(vals))
        n = d1("SELECT COUNT(*) c FROM model_registry WHERE task='batch'")
        print(f"[3/3] 已写入 batch 池 {len(good)} 个 → 库内 batch 模型累计 {n[0]['c']} 个")
        print("      ★ 只进 batch(离线编译/赛马),不进 web 容错链 —— NVIDIA 经 CF Worker 走不通是实测结论")


if __name__ == "__main__":
    main()
