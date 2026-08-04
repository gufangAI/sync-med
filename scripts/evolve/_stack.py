#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""现有技术栈扫描 —— 判定器必须先知道**我们已经有什么**

立此因(2026-08-04 创始人当场骂):
  「这判断谁做的这么愚蠢的」「**是头猪都知道 PaddleOCR 这好用**」

查证后我认账,而且比他说的更难看:
  · 判定器把 PaddleOCR / tesseract / MinerU 判成「改进 OCR 产线」——
    而 `pip install paddlepaddle paddleocr` **就在我们自己的 ocr_race.yml 里**,
    我们早就跑过 OCR 引擎对赛,**手上已经有它的实测数据**;
  · RapidOCR 更是 adoption.txt 里白纸黑字的第二 OCR 线主力;
  · firecrawl / browser-use / crawl4ai / Scrapling 四个爬虫全判「改进采集下载线」——
    四个同类工具堆在同一个模块上,这不是判断,是**同义词联想**。

根因不是模型笨,是**判定器从来没被告知我们已有什么**。
  一个不知道自家家底的评审员,只能做「这个领域最有名的工具是什么」的联想 ——
  而候选又是**按星数排序**喂进去的,于是必然吐回一堆人尽皆知的名字。
  高星 + 无知 = 常识复读机。

所以这里**不靠我的记忆**(记忆本身就错了:它说「PaddleOCR 已弃」,
实际还装在 ocr_race.yml 里),而是**从真实文件扫**:
  · workflows 里的 pip install / npm install —— 真在跑的依赖
  · adoption.txt —— 已落地台账(2026-07-29 建,判据是"真跑过+接进系统+有数字")
  · package.json —— 前端依赖
扫出来的名字进两处:① 硬跳过(零 AI 调用) ② 喂给判定器当"家底"。
"""
import os, re, json, glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# 通用词不算技术栈 —— 否则 "requests" 会让所有带 request 的仓都被跳过
_TOO_GENERIC = {"on", "only", "the", "so", "purpose", "council", "uses", "stdlib",
                "urllib", "requests", "pillow", "numpy", "boto3", "pytest", "install"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def scan_stack():
    """返回 (名字集合, 人读清单)。名字用于硬跳过,清单喂给判定器。"""
    names, lines = set(), []

    # ① workflows 里真装的依赖
    deps = set()
    for f in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in re.finditer(r"pip install([^\n&|]*)", txt):
            for tok in re.split(r"[\s\"']+", m.group(1)):
                tok = re.split(r"[=<>\[]", tok)[0].strip().strip("-")
                if tok and not tok.startswith("-") and len(tok) > 2 and tok.lower() not in _TOO_GENERIC:
                    deps.add(tok)
    if deps:
        lines.append("【云端产线真装的依赖】" + " / ".join(sorted(deps)))
        names |= {_norm(d) for d in deps}

    # ② adoption.txt —— 已落地台账(判据:真跑过 + 接进系统 + 有数字)
    ad = os.path.join(HERE, "..", "intel_radar", "adoption.txt")
    landed = []
    if os.path.exists(ad):
        for ln in open(ad, encoding="utf-8", errors="ignore"):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            repo = ln.split("|")[0].strip()
            if repo:
                landed.append(repo)
                names.add(_norm(repo.split("/")[-1]))
                names.add(_norm(repo))
    if landed:
        lines.append("【已落地并在用(adoption 台账)】" + " / ".join(landed))

    # ③ 前端依赖
    pj = os.path.join(ROOT, "..", "guyaofang-web", "package.json")
    if os.path.exists(pj):
        try:
            d = json.load(open(pj, encoding="utf-8"))
            fe = list((d.get("dependencies") or {}).keys())
            if fe:
                lines.append("【前端依赖】" + " / ".join(fe[:30]))
                names |= {_norm(x.split("/")[-1]) for x in fe}
        except Exception:
            pass

    # ④ 部署形态 —— 不是"栈"但同样决定能不能用,写死
    lines.append("【运行形态】Cloudflare Serverless(Workers/Pages/D1/R2/Vectorize)"
                 "+ GitHub Actions,**零常驻主机、零月费**")
    return names, lines


def already_have(repo_name, stack_names):
    """候选是不是我们已经有的。**零 AI 调用**,判定之前先筛掉。"""
    tail = _norm(str(repo_name).split("/")[-1])
    if not tail or len(tail) < 4:
        return False
    if tail in stack_names:
        return True
    # 子串命中:paddleocr ⊂ {paddleocr}、rapidocr ⊂ {rapidocronnxruntime}
    for n in stack_names:
        if len(n) >= 5 and (tail in n or n in tail):
            return True
    return False


if __name__ == "__main__":
    names, lines = scan_stack()
    print("扫出技术栈名字 %d 个\n" % len(names))
    for ln in lines:
        print("  " + ln[:300])
    print("\n硬跳过自测:")
    for t in ("PaddlePaddle/PaddleOCR", "RapidAI/RapidOCR", "tesseract-ocr/tesseract",
              "opendatalab/MinerU", "firecrawl/firecrawl", "unclecode/crawl4ai"):
        print(f"  {'已有→跳过' if already_have(t, names) else '新东西 → 进判定'}  {t}")
