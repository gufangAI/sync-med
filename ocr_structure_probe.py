#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PP-StructureV3 对打探针 —— 同一个包里那个更强的，是不是比我们现用的强。

立此因（2026-09-02 深夜）：
  今晚为给古籍分块补版面边界，一路扒到 MinerU，判定"逻辑层可移植、模型层要 GPU
  搬不过来"。判断本身没错，但**方向绕了远路**：我们 Actions 里早装着
  `paddleocr>=3,<4`，同一个包里除了在用的 LayoutDetection，还有 PPStructureV3
  （版面 + 表格 + 阅读顺序）和 PaddleOCR-VL（0.9B 文档 VLM，OmniDocBench 96.34）
  —— **同生态、零新依赖、Apache-2.0 无营收条款**，比引 MinerU（torch 栈 +
  超 20M 月营收需商业授权）成本低得多。

  这条是对照另一份同题报告时发现的：那份在 OCR 一节列了 23 个项目，每个都写了
  「注意」（Tesseract 已停止模型迭代、olmOCR 2026-03 后无提交、Marker v2 权重有
  营收门槛、MinerU 超 20M 营收需商授）。我扒得比它深（读源码、验铁律、算成本），
  但**扫得远不如它全**，于是眼前同生态的路没看见。这个探针补那一课。

判据（与 ocr_layout_probe 同口径，可直接对比 run 33649135911 的数字）：
  · 平均框/页：太低(≈1) = 接不住
  · 零框页数：越低越好
  · **类型数**：本探针重点。LayoutDetection 只分出 text/paragraph_title/image
    三类，而夹注判据要立足于"能不能把结构分开"，类型分不开就无从下手。
  · 秒/页：决定几万页规模能否承受（现用 LayoutDetection 是 11.4 秒/页）

取图完全复用 ocr_layout_probe 的 pan_fetch + d1_medical_pdids + synthetic_page，
不写第二份（平台铁律：同一份逻辑只许有一份实现）。零 R2 移动、零 LIST。
本地禁算力：推理只在 Actions 跑。
"""
import io
import os
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SAMPLE_BOOKS = int(os.environ.get("PROBE_BOOKS", "3"))
SAMPLE_PAGES = int(os.environ.get("PROBE_PAGES", "4"))


def collect_samples():
    """取样本页。函数名与流程照抄 ocr_layout_probe.main() 的前半段。

    第一版我凭空猜了 collect_pages/get_pages/fetch_pages 四个名字，一个都不存在
    —— 那样在云端会直接 SystemExit，白跑一轮 runner。教训：调别人的函数前先
    grep 一遍真实函数名，别按"应该叫这个"去写。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pan_fetch
    import ocr_layout_probe as base

    samples = []
    real = pan_fetch.available()
    if real:
        try:
            books = base.d1_medical_pdids(SAMPLE_BOOKS)
        except Exception as e:                                   # noqa: BLE001
            print("D1 抽书失败，退合成样例: %s" % str(e)[:120], flush=True)
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
        samples = [("synthetic", base.synthetic_page())]
    print("探针来源: %s · 样本 %d 页"
          % ("真实古籍(123)" if real else "合成样例(无密钥)", len(samples)), flush=True)
    return samples, real


def blocks_of(page):
    """把 PPStructureV3 的一页结果摊平成 [(label, ...)]。

    它的返回结构随版本变过，所以按几个已知键依次找，全找不到就返回空并让上层
    记成零框 —— **不猜、不静默补默认值**。零框如果是解析没对上而不是真没框，
    会在"类型数=0"上立刻暴露，不会伪装成结论。
    """
    d = page if isinstance(page, dict) else (getattr(page, "json", None) or {})
    if isinstance(d, dict) and "res" in d and isinstance(d["res"], dict):
        d = d["res"]
    for key in ("parsing_res_list", "boxes", "layout_det_res"):
        v = d.get(key) if isinstance(d, dict) else None
        if isinstance(v, dict):
            v = v.get("boxes")
        if isinstance(v, list) and v:
            return v
    return []


def label_of(b):
    if not isinstance(b, dict):
        return "?"
    for k in ("block_label", "label", "type", "cls_id"):
        if b.get(k) is not None:
            return str(b[k])
    return "?"


def main():
    samples, real = collect_samples()
    from paddleocr import PPStructureV3
    print("载入 PPStructureV3…", flush=True)
    model = PPStructureV3()

    per_page, labels, zero, sec = [], {}, 0, 0.0
    for tag, img in samples:
        t0 = time.time()
        try:
            res = model.predict(img)
        except Exception as e:                                   # noqa: BLE001
            print("  %s 预测失败: %s" % (tag, str(e)[:140]), flush=True)
            traceback.print_exc()
            continue
        sec += time.time() - t0
        n = 0
        for page in (res if isinstance(res, list) else [res]):
            for b in blocks_of(page):
                labels[label_of(b)] = labels.get(label_of(b), 0) + 1
                n += 1
        per_page.append(n)
        if n == 0:
            zero += 1
        print("  %s: %d 框" % (tag, n), flush=True)

    if not per_page:
        raise SystemExit("零页跑通，不出报告 —— 空跑的数字会被误读成结论")

    pages = len(per_page)
    avg = sum(per_page) / pages
    spp = sec / pages
    tdist = ", ".join("%s x%d" % kv for kv in sorted(labels.items(), key=lambda x: -x[1]))

    L = [
        "## PP-StructureV3 对打探针 · %s" % ("真实古籍" if real else "合成样例"),
        "",
        "- 样本页数: **%d** · 平均每页版面框: **%.1f** · 零框页: **%d**" % (pages, avg, zero),
        "- 类型分布: %s" % (tdist or "(零类型 —— 可能是结果结构没解析对，需人工核)"),
        "- 耗时: %.1fs（**%.1f 秒/页**）" % (sec, spp),
        "",
        "### 与现用 LayoutDetection 对照（run 33649135911）",
        "",
        "| 指标 | LayoutDetection（现用） | PPStructureV3（本次） |",
        "|---|---|---|",
        "| 平均框/页 | 4.4 | %.1f |" % avg,
        "| 零框页 | 0 | %d |" % zero,
        "| 类型数 | 3 | %d |" % len(labels),
        "| 秒/页 | 11.4 | %.1f |" % spp,
        "",
        "> **类型数是重点**：夹注判据要立足于能不能把结构分开，分不开就无从下手。",
        "> 秒/页决定几万页规模能否承受（1 万页 x 11.4 秒 = 31.8 小时，需分 6 个 job）。",
    ]
    io.open("ocr_structure_probe.md", "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n" + "\n".join(L), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
