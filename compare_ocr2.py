# -*- coding: utf-8 -*-
"""竖排古籍 OCR 双路对比 · NDLOCR-Lite vs RapidOCR

立此文件因（2026-08-16）：
  创始人「多 OCR 比较，才是更好的；若是可以多个整合优点为一个，那就是我们自己的了」。

与既有 compare_ocr.py 的关系 —— **加法不是替换，原文件一字未动**：
  · 原文件比的是 NDLOCR-Lite vs 某 VLM 路线，需要该路线的密钥；
  · 本文件比的是 NDLOCR-Lite vs **RapidOCR（我们现役主线，实测 98.7% 成功率）**，
    不需要任何额外密钥 —— 这才回答真正的问题：**竖排古籍该用谁**。

为什么必须重跑而不是引用旧结论（原文件 32-34 行自己写着）：
  旧对比的 cjk_ratio 过滤用的是本地一份四段窄表，而生产线早已改成 21 段的共享实现，
  **两边过滤掉的块不是同一批**；该缺陷修好后对比再没跑过（唯一一次运行早于修复日期）。
  所以旧的 seq_sim/overlap 数字作废，本次从零取数。

判据（跑完必须能回答）：
  ① 两路各自的 CJK 字符产出量
  ② 两路一致率（序列相似度 seq_sim / 字符集重合 overlap）
  ③ **分歧集中在哪** —— 这一条决定后续融合规则怎么写，不能拍脑袋
"""
import os, sys, io, json, re, base64, difflib, subprocess, collections, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_quality import cjk_ratio          # 与生产同一份实现，不再自带窄表
import pan_fetch

N_PAGES  = int(os.environ.get("N_PAGES", "8"))
BOOKS    = [b.strip() for b in os.environ.get("BOOKS", "").split(",") if b.strip()]
OCR_SRC  = "ndlocr-lite/src"
CONF_MIN, CJK_MIN = 0.6, 0.3          # 与生产 ocr_ndl.py 同一套过滤

CF   = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TK = os.environ["D1_API_TOKEN"]
import requests


def d1(sql, params=None):
    r = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{CF}/d1/database/{DB}/query",
                      headers={"Authorization": f"Bearer {TK}", "Content-Type": "application/json"},
                      json={"sql": sql, "params": params or []}, timeout=90)
    return (r.json().get("result") or [{}])[0].get("results") or []


def norm(s):
    return re.sub(r"\s+", "", s or "")


# ── 路线 A：NDLOCR-Lite（竖排专训，逐块置信度）─────────────────────
#
# 2026-08-16 修正（首跑 100 页 NDL 侧全 0 字，靠本函数返回的 ndl_note 一次定位）：
#   ① 原调用写的是 **NDLOCR v2 的 CLI**（main.py infer <img> <out> -x -s json），
#      lite 版根本没有 main.py —— 报 "can't open file .../src/main.py"。
#      lite 的真实入口是 `ocr.py --sourceimg <图> --output <目录>`（README 实证）。
#   ② 就算路径改对，原解析也会崩：lite 的 JSON 是 `{"contents": [[行,行,…]]}`,
#      **contents 外面还套一层**，直接 for 会拿到内层 list，`.get` 抛 AttributeError。
#   权重（4 个 ONNX 共 ~157MB）随仓 clone 即带，非 LFS，不需另外下载。
def ndl_ocr(img_path, tag):
    out = os.path.abspath(f"/tmp/ndl_{tag}")
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(img_path))[0]
    r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", os.path.abspath(img_path),
                        "--output", out],
                       cwd=OCR_SRC, capture_output=True, text=True, timeout=900)
    jf = os.path.join(out, stem + ".json")
    if not os.path.exists(jf):
        tail = (r.stderr or r.stdout or "")[-200:]
        return None, f"ndl 无输出(rc={r.returncode}): {tail}"
    data = json.load(open(jf, encoding="utf-8"))
    # contents 是 [[行,行,…]]（单图路径）或 [[…],[…]]（多页路径）——一律拍平
    blocks = []
    for page in (data.get("contents") or []):
        blocks.extend(page if isinstance(page, list) else [page])
    kept = [b.get("text", "") for b in blocks
            if (b.get("confidence") or 0) >= CONF_MIN and cjk_ratio(b.get("text") or "") >= CJK_MIN]
    vert = sum(1 for b in blocks if str(b.get("isVertical")).lower() == "true")
    return "\n".join(kept), f"blocks={len(blocks)} kept={len(kept)} vertical={vert}"


# ── 路线 B：RapidOCR（我们现役主线）────────────────────────────
_rapid = None
def rapid_ocr(img_path):
    global _rapid
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    res, _ = _rapid(img_path)
    if not res:
        return "", "blocks=0"
    kept = [t for _, t, sc in res if (sc or 0) >= CONF_MIN and cjk_ratio(t or "") >= CJK_MIN]
    return "\n".join(kept), f"blocks={len(res)} kept={len(kept)}"


# ── 选书：优先取竖排古籍（日本馆藏汉籍）──────────────────────────
if BOOKS:
    ph = ",".join(["?"] * len(BOOKS))
    rows = d1(f"SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 WHERE book_id IN ({ph})", BOOKS)
else:
    rows = d1("""SELECT book_id, page_count, pan_dir_id FROM books_assets_v2
                 WHERE pan_dir_id IS NOT NULL AND pan_dir_id<>'' AND page_count>=20
                 ORDER BY book_id LIMIT 2""")
print(f"对比书目 {len(rows)} 本: " + " | ".join(f"{r['book_id']}({r['page_count']}p)" for r in rows))

recs = []
for r in rows:
    bid, pdid, pc = r["book_id"], r["pan_dir_id"], r["page_count"] or 0
    step = max(1, pc // (N_PAGES + 1))
    pages = [1 + i * step for i in range(N_PAGES) if 1 + i * step <= pc]
    for pno in pages:
        content = pan_fetch.fetch_page(pdid, pno)
        if not content:
            print(f"  {bid} p{pno} 取图失败"); continue
        img = f"/tmp/{bid}_{pno:04d}.webp"
        open(img, "wb").write(content)
        t0 = time.time(); a, a_note = ndl_ocr(img, f"{bid}_{pno}"); ta = time.time() - t0
        t0 = time.time(); b, b_note = rapid_ocr(img);               tb = time.time() - t0
        if not a and not b:
            print(f"  {bid} p{pno} 两路皆空"); continue
        na, nb = norm(a or ""), norm(b or "")
        seq = difflib.SequenceMatcher(None, na, nb).ratio() if (na and nb) else 0.0
        ca, cb = collections.Counter(na), collections.Counter(nb)
        ovl = sum((ca & cb).values()) / max(len(na), len(nb)) if (na or nb) else 0.0
        # 分歧字：只在一路出现的字符（决定融合规则怎么写）
        only_a = sorted((ca - cb).elements()); only_b = sorted((cb - ca).elements())
        recs.append({"book": bid, "page": pno,
                     "ndl_chars": len(na), "rapid_chars": len(nb),
                     "ndl_cjk": round(cjk_ratio(a or ""), 3), "rapid_cjk": round(cjk_ratio(b or ""), 3),
                     "ndl_sec": round(ta, 1), "rapid_sec": round(tb, 1),
                     "seq_sim": round(seq, 3), "overlap": round(ovl, 3),
                     "only_ndl": "".join(only_a[:40]), "only_rapid": "".join(only_b[:40]),
                     "ndl_note": a_note, "rapid_note": b_note,
                     "ndl_text": (a or "")[:300], "rapid_text": (b or "")[:300]})
        print(f"  {bid} p{pno}: ndl={len(na)}字/{ta:.1f}s rapid={len(nb)}字/{tb:.1f}s "
              f"seq={seq:.3f} ovl={ovl:.3f}")

# ── 汇总 ────────────────────────────────────────────────────
if recs:
    avg = lambda k: sum(x[k] for x in recs) / len(recs)
    agree_hi = sum(1 for x in recs if x["seq_sim"] >= 0.8)
    print("\n" + "=" * 56)
    print(f"页数 {len(recs)}")
    print(f"  NDL   平均 {avg('ndl_chars'):.0f} 字/页 · CJK {avg('ndl_cjk'):.3f} · {avg('ndl_sec'):.1f} 秒/页")
    print(f"  Rapid 平均 {avg('rapid_chars'):.0f} 字/页 · CJK {avg('rapid_cjk'):.3f} · {avg('rapid_sec'):.1f} 秒/页")
    print(f"  一致率 seq_sim 均值 {avg('seq_sim'):.3f} · overlap 均值 {avg('overlap'):.3f}")
    print(f"  高度一致(seq≥0.8)的页: {agree_hi}/{len(recs)}")
    # 分歧字频次 —— 融合规则的原料
    dis = collections.Counter()
    for x in recs:
        dis.update(x["only_ndl"]); dis.update(x["only_rapid"])
    print(f"  分歧字 TOP20: {''.join(c for c, _ in dis.most_common(20))}")
    json.dump(recs, open("compare2_result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已写 compare2_result.json")
else:
    print("零有效页 —— 不下任何结论，先查取图与依赖")
