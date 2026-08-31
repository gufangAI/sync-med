#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版面分析质量探针 —— 回答"PP-DocLayout 在真实竖排古籍上到底出不出得了版面框"。

门序(2026-08-31 古籍OCR线):
  上一步已在 Actions 标准 CPU 真跑验证 PP-DocLayout 的 LayoutDetection **能跑**
  (装39s/跑一页8.8s/峰值979MB/合成图出框)。但"合成图能跑" != "真竖排古籍质量够用"。
  这个探针就补这一米:抓真实古籍页 → 跑 LayoutDetection → 报每页版面框数/类型分布/
  零框页占比。够好才值得建整条"版面分析 + 逐块免费池VL"线;不够好就换 huridocs 或退回。

取图复用 ocr.py 同款 pan_fetch(123 直链),零 R2 移动、零 LIST。
  · 有 PAN_CLIENT_ID/SECRET + D1 凭据(主仓 Actions) → 抽 K 本医书各前 M 页真跑
  · 没有(我 fork 无密钥)          → 退回合成样例,只验 detect 核心+报告格式跑得通

本地禁算力铁律:LayoutDetection 推理只在 Actions 跑,绝不本机。
"""
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

SAMPLE_BOOKS = int(os.environ.get("PROBE_BOOKS", "3"))   # 抽几本
SAMPLE_PAGES = int(os.environ.get("PROBE_PAGES", "4"))   # 每本前几页


def get_detector():
    from paddleocr import LayoutDetection
    return LayoutDetection()


def detect(model, img_bytes):
    """一页图 → [{label, bbox, score}]。任何异常返回 None(探针不因单页崩)。"""
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(img_bytes)
            path = f.name
        res = model.predict(path)
        blocks = []
        for r in res:
            # 抠框:LayoutDetection 结果对象结构随版本变,多路兜底。
            # getattr(r,'boxes') 是可行性探针实测能拿到框的那条路,优先。
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                j = getattr(r, "json", None)
                if callable(j):
                    j = j()
                if isinstance(j, dict):
                    boxes = j.get("boxes") or (j.get("res") or {}).get("boxes")
            if boxes is None and isinstance(r, dict):
                boxes = r.get("boxes")
            if os.environ.get("PROBE_DEBUG") and not blocks:
                print("  [debug] result type=%s attrs=%s" % (
                    type(r).__name__, [a for a in dir(r) if not a.startswith('_')][:12]), flush=True)
            for b in (boxes or []):
                lab = (b.get("label") if isinstance(b, dict) else getattr(b, "label", None)) or "?"
                sc = (b.get("score") if isinstance(b, dict) else getattr(b, "score", 0)) or 0
                blocks.append({"label": str(lab), "score": round(float(sc), 3)})
        return blocks
    except Exception as e:                                        # noqa: BLE001
        print("  ! detect fail: %s" % str(e)[:120], flush=True)
        return None
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def d1_medical_pdids(k):
    """从 D1 抽 k 本有 123 folder id 的医书(reuse ocr.py 的 books_assets_v2 口径)。"""
    import urllib.request
    acc = os.environ.get("CF_ACCOUNT_ID")
    db = os.environ.get("D1_DATABASE_ID")
    tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        return []
    sql = ("SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
           "WHERE pan_dir_id IS NOT NULL AND page_count>=%d LIMIT %d" % (SAMPLE_PAGES, k))
    url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (acc, db)
    req = urllib.request.Request(url, data=json.dumps({"sql": sql}).encode(),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"}, method="POST")
    j = json.loads(urllib.request.urlopen(req, timeout=45).read())
    if not j.get("success"):
        raise RuntimeError("D1: " + str(j.get("errors"))[:160])
    return j["result"][0].get("results", [])


def synthetic_page():
    """合成一页多块文档(fork 无密钥时验 detect 核心+报告链路跑得通)。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1000, 1400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([80, 60, 920, 120], outline="black", width=2)
    for col_x in (90, 520):
        y = 180
        for _ in range(18):
            d.line([col_x, y, col_x + 380, y], fill="black", width=3)
            y += 18
    d.rectangle([90, 560, 900, 760], outline="black", width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main():
    t0 = time.time()
    import pan_fetch
    model = get_detector()

    samples = []   # (tag, img_bytes)
    real = pan_fetch.available()
    if real:
        try:
            books = d1_medical_pdids(SAMPLE_BOOKS)
        except Exception as e:                                   # noqa: BLE001
            print("D1 抽书失败,退合成样例: %s" % str(e)[:120], flush=True)
            books, real = [], False
        for row in books:
            pdid = str(row.get("pan_dir_id") or "")
            bid = row.get("book_id")
            for pno in range(1, SAMPLE_PAGES + 1):
                b = pan_fetch.fetch_page(pdid, pno)
                if b:
                    samples.append(("%s/p%d" % (bid, pno), b))
    if not samples:
        real = False
        samples = [("synthetic", synthetic_page())]

    print("探针来源: %s · 样本 %d 页" % ("真实古籍(123)" if real else "合成样例(fork无密钥)", len(samples)), flush=True)

    per_page, labels, zero = [], {}, 0
    for tag, b in samples:
        blocks = detect(model, b)
        if blocks is None:
            continue
        per_page.append(len(blocks))
        if not blocks:
            zero += 1
        for bl in blocks:
            labels[bl["label"]] = labels.get(bl["label"], 0) + 1
        print("  %s: %d 框 %s" % (tag, len(blocks),
              ",".join("%s×%d" % (k, v) for k, v in
                       sorted({bl["label"]: 1 for bl in blocks}.items()))), flush=True)

    n = len(per_page)
    avg = round(sum(per_page) / n, 1) if n else 0
    lines = ["## 版面分析质量探针 · %s" % ("真实古籍" if real else "合成样例(需主仓密钥才能测真古籍)"), ""]
    lines.append("- 样本页数: **%d** · 平均每页版面框: **%s** · 零框页: **%d**" % (n, avg, zero))
    lines.append("- 版面框类型分布: %s" % (", ".join("%s×%d" % (k, v)
                 for k, v in sorted(labels.items(), key=lambda x: -x[1])) or "(无)"))
    lines.append("- 耗时: %.1fs" % (time.time() - t0))
    lines.append("")
    if not real:
        lines.append("> ⚠️ 这是合成样例,只证明 detect 核心+报告链路跑得通。")
        lines.append("> **真竖排古籍质量必须在主仓 Actions 跑**(有 PAN_CLIENT_ID/D1 密钥)才算数。")
    else:
        lines.append("> 判据:平均框数太低(≈1)或零框页多 = PP-DocLayout 接不住竖排古籍 → 换 huridocs/退回;")
        lines.append("> 框数合理且类型分得开 = 值得建「版面分析→逐块免费池VL」整条线。")
    io.open("ocr_layout_probe.md", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
