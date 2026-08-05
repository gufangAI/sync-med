#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落地登记 —— 闭环的**最后一个箭头**,一条命令走完

立此因(2026-08-05 创始人定的闭环形状):
    鹰眼采集(自动) → 审核人改系统(本地) → 发布前端 → **回写台账**
            ↑                                            │
            └────── 已落地的下轮不再推荐 ←───────────────┘

前三个箭头已经通了(采集/判定/收件箱都在自动跑),**第四个从来没真走过一次** ——
「处理完写 adoption.txt」全靠人记得,而 adoption.txt 自己开头就写着这条产线的病:
  「the pipeline was fetch → filter → file an Issue → STOP.
    Nothing recorded, or counted, what got absorbed, so nothing could ever report that nothing was.」
**没有回写动作,环就永远差最后一格。** 这个脚本就是那一格。

它做三件事:
  ① 按 adoption.txt 的三条判据把关(真跑过 / 接进系统 / 有数字),缺一条就拒绝登记
  ② 写进 adoption.txt(唯一真源,手写格式,人能读)
  ③ 顺手把 D1 里那条候选标成 landed,下一轮判定的家底闸自动筛掉

用法:
  python scripts/evolve/land.py --repo BerriAI/litellm --module 免费模型池 \
         --note "Actions 里跑通,统一接入 16 家;探活通过率 13/45 → 待测"
  python scripts/evolve/land.py --repo X/Y --reject --note "试过,达不到,原因……"
"""
import os, sys, time, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "content_factory"))
ADOPTION = os.path.join(HERE, "..", "intel_radar", "adoption.txt")


def _d1():
    """D1 是可选的 —— 没凭据也要能写台账。台账是真源,D1 只是加速。"""
    try:
        from _ai import d1, q
        return d1, q
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--module", default="", help="接到我们哪个模块")
    ap.add_argument("--note", required=True, help="真跑过什么 / 接进哪里 / 数字是多少")
    ap.add_argument("--reject", action="store_true", help="试过但不采用,同样要留痕")
    a = ap.parse_args()

    note = a.note.strip()
    if not a.reject:
        # adoption.txt 自己定的三条判据 —— 缺一条只算候选,不算落地。
        # 这里做**确定性把关**:说不出数字的一律拒绝登记,免得台账变成许愿池。
        if not any(ch.isdigit() for ch in note):
            print("✗ 拒绝登记:落地判据第三条要求**有数字**(量/耗时/成功率/成本),"
                  "你的说明里一个数字都没有。\n"
                  "  台账一旦允许「已接入」这种没数字的话,它就退化成许愿池 —— "
                  "而这条产线当初正是因为「只开 Issue 从不记录落地」才建的这份台账。")
            return 2
        if len(note) < 12:
            print("✗ 拒绝登记:说明太短,写清「真跑过什么 / 接进哪里 / 数字多少」")
            return 2

    line = (f"{a.repo:<28} | {time.strftime('%Y-%m-%d')} | "
            f"{'DROPPED: ' if a.reject else ''}{a.module + ' — ' if a.module else ''}{note}\n")
    with open(ADOPTION, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    print(f"{'✗ 已记为不采用' if a.reject else '✓ 已登记落地'}:{a.repo}")
    print(f"  写入 {os.path.normpath(ADOPTION)}")

    d1, q = _d1()
    if d1:
        try:
            st = "dropped" if a.reject else "landed"
            d1(f"UPDATE evolve_candidates SET status={q(st)}, updated_at={int(time.time())}, "
               f"verdict_note=COALESCE(verdict_note,'') || {q(' | ' + st + ': ' + note[:120])} "
               f"WHERE title={q(a.repo)}")
            print(f"  D1 候选状态 → {st}")
        except SystemExit:
            # d1() 缺凭据时是 sys.exit(),**SystemExit 不是 Exception 子类**,
            #   只写 except Exception 抓不到,整个脚本会当场退出 —— 台账写了但用户看不到收尾。
            print("  (本地无 D1 凭据,只写了台账 —— 台账是真源,下轮家底闸照样读得到)")
        except Exception as e:
            print(f"  (D1 未更新,不影响台账:{str(e)[:60]})")
    else:
        print("  (本地无 D1 凭据,只写了台账 —— 台账是真源,下轮家底闸照样读得到)")

    print("\n下一轮判定的家底闸会自动把它筛掉,不再重复推荐 —— 环闭上了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
