#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出分类概览数据(给 HTML 报告用)。

独立成文件而不是内联执行 —— arsenal_category 在模块级重绑了 sys.stdout,
被 exec_module 载入时会把调用方已包装的 stdout 关掉(实测 ValueError:
I/O operation on closed file)。这类模块级副作用只能靠"别在同进程里二次包装"避开。
"""
import io
import json
import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

spec = importlib.util.spec_from_file_location(
    "ac", os.path.join(HERE, "arsenal_category.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)          # 它会自己接管 stdout,之后一律用 print

cfg = m.load_cfg()
cats = cfg["categories"]
pool = m.load_pool()

groups = {c["key"]: [] for c in cats}
unc = []
for row in pool.values():
    h = m.classify(row, cats)
    if h:
        row["_hits"] = h[3]
        row["_cscore"] = h[2]
        groups[h[0]].append(row)
    else:
        unc.append(row)

out = {"cats": [], "total": len(pool), "unc": len(unc)}
for c in cats:
    g = sorted(groups[c["key"]],
               key=lambda x: (-(x.get("_cscore") or 0), -(x.get("stars") or 0)))
    # 类内先按"对口程度"取前 60,再在这 60 个里按星数排 —— 直接按星数排会让
    # 每类头部被通用大项目占满(vscode/react 这种),那不是"这一类最对口的"。
    top = sorted(g[:60], key=lambda x: -(x.get("stars") or 0))[:12]
    out["cats"].append({
        "key": c["key"], "name": c["name"], "desc": c.get("desc", ""),
        "n": len(g),
        "top": [{"repo": r["repo"], "stars": r.get("stars") or 0,
                 "lang": r.get("lang") or "", "lic": r.get("license") or "",
                 "desc": (r.get("desc") or "")[:200],
                 "detail": (r.get("detail_cn") or r.get("capability") or "")[:600],
                 "origin": (r.get("origin") or [""])[0] if r.get("origin") else ""}
                for r in top],
    })

dst = os.path.join(ROOT, "report_data.json")
with io.open(dst, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)

for c in out["cats"]:
    print("%-16s %4d  %s" % (c["name"], c["n"],
                             ", ".join(x["repo"].split("/")[-1] for x in c["top"][:3])))
print("池 %d · 未分类 %d · 已写 %s" % (out["total"], out["unc"], dst))
