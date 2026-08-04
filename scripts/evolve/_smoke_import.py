#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 import 冒烟 —— **py_compile 只查语法,查不出 NameError**。

立此因(2026-08-04 当场吃亏):我把 `PROMPT_ABSORBABLE = {... SCOPE_MODULES ...}`
  写在了 SCOPE_MODULES 定义之前。`py_compile` 全绿,推上云端**第一行就崩**,
  白等一轮 Actions。语法对 ≠ 能跑,模块级代码必须真执行一遍才知道。
CI 里排在所有 evolve 步骤最前面,几百毫秒,换掉一轮白跑。
"""
import sys, os, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
MODS = ["improve", "evaluate", "trending", "ranks", "adopt", "harvest"]

bad = 0
for m in MODS:
    p = os.path.join(HERE, m + ".py")
    if not os.path.exists(p):
        continue
    spec = importlib.util.spec_from_file_location(m, p)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        print(f"  ✓ {m}")
    except SystemExit:
        print(f"  ✓ {m}(main 守卫)")
    except Exception as e:
        print(f"  ✗ {m}: {type(e).__name__}: {e}")
        bad += 1
print(f"\n冒烟 import:{len(MODS)-bad} 过 / {bad} 败")
sys.exit(1 if bad else 0)
