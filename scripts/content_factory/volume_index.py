#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卷级语义索引 —— 给古籍每一卷生成「这一卷讲什么」

立此因(2026-08-02 创始人转来的 DeepTutor 分析):
  「综述走语义检索,读教材用 PageIndex 按页索引,分析图谱上 GraphRAG ——
    每类材料的检索策略不一样…大多数工具给你一种方式处理所有材料,它给了你五把不同的刀」。
  对照我们的实测家底:
    · 语义向量检索 ✅ 寻脉已在用(VEC_TCM → tcm-rag-clean-768)
    · 知识图谱     ⚠️ 只用了一跳关系,不是 GraphRAG
    · **按卷/按页的层级索引 ❌ 完全没有** —— 7589 卷的 vol_title 全是「卷 1」这种占位,
      零语义。AI 想找「这本书哪一卷讲不寐」,只能把全书打散成 chunk 撞运气。
  用户读古籍是**读整本**,不是问一句答一句;没有卷级导航,导读这个项目就没有骨架。
  这个脚本补的就是那根骨架。

做法(零 R2、零算力):
  素材直接取 D1 里**已有的** books_text_chunks.preview_120,不读 R2、不重新 OCR。
  每卷取前若干段预览 + 书目元数据 → 免费池生成 60-120 字概要 + 3-6 个关键词 → 写回卷表。

合规:概要是**文献性描述**,主语是书不是人;禁剂量/疗效承诺(与内容工厂同一套硬闸)。
"""
import os, sys, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import d1, q, ask_best, parse_json          # noqa: E402  公共底座,不再抄第二份

WORKERS    = int(os.environ.get("CF_WORKERS", "6"))
PROMPT_VER = "vi-v1-2026-08-02"

# 与内容工厂同一套合规硬闸 —— 概要里出现这些即整条不写
BANNED = [
    "克", "钱重", "水煎服", "每日三次", "每次服", "煎服", "顿服", "口服剂量",
    "疗效显著", "治愈率", "包治", "特效", "根治",
    "建议你服用", "你应该服用", "可以服用", "推荐剂量", "用法用量",
]

SYS = (
    "你是中医古籍编目助手。给你一部古籍某一卷的**开头若干段原文片段**,"
    "请概括这一卷讲什么,供读者在目录里快速定位。\n"
    "**硬性要求**:\n"
    "  ① 主语永远是文献本身(「本卷论述…」「本卷收录…」),**绝不写成对读者的医嘱**;\n"
    "  ② 绝不写剂量、用法用量、煎服法;绝不做疗效承诺;\n"
    "  ③ 只根据给到的片段概括,**片段没提到的不要凭空补**;\n"
    "  ④ 片段过少或全是残缺 OCR 无法判断时,输出 {\"unclear\": true} —— 这不算失败,"
    "     **宁可留空,也不要编一段像模像样的假概要**。\n"
    "只输出 JSON:{\"summary\":\"60-120字概要\",\"keywords\":[\"3-6个关键词\"]}"
)


def violates(obj):
    import json as _j
    blob = _j.dumps(obj, ensure_ascii=False)
    return [w for w in BANNED if w in blob]


def build_prompt(row, segs):
    head = f"书名:{row.get('clean_name') or ''}"
    if row.get("author"):
        head += f" · 作者:{row['author']}"
    if row.get("dynasty"):
        head += f" · 年代:{row['dynasty']}"
    if row.get("category"):
        head += f" · 类目:{row['category']}"
    body = "\n".join(f"[{i+1}] {s}" for i, s in enumerate(segs))
    return (f"{head}\n本卷:第 {row['vol_no']} 卷(原标题「{row.get('vol_title') or ''}」)\n"
            f"本卷开头片段:\n{body}\n\n请按要求输出 JSON。")


def one(row):
    """返回 (row, obj, err)。只做取材+生成,写库回主线程串行做。"""
    try:
        segs = [r["p"] for r in d1(
            "SELECT preview_120 AS p FROM books_text_chunks "
            f"WHERE text_id={q(row['text_id'])} AND vol_no={int(row['vol_no'])} "
            "AND preview_120 IS NOT NULL AND TRIM(preview_120)<>'' "
            "ORDER BY paragraph_no ASC LIMIT 12")]
        if len(segs) < 2:
            return row, {"unclear": True, "why": "可用片段不足 2 段"}, None
        txt, model = ask_best(SYS, build_prompt(row, segs), max_tokens=900, need="{")
        obj = parse_json(txt)
        obj["_model"] = model
        return row, obj, None
    except Exception as e:
        return row, None, e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60, help="本轮处理多少卷")
    args = ap.parse_args()

    rows = d1(
        "SELECT v.text_id, v.vol_no, v.vol_title, "
        "       t.clean_name, t.author, t.dynasty, t.category "
        "  FROM books_text_volumes v JOIN books_text t ON t.text_id = v.text_id "
        " WHERE (v.vol_summary IS NULL OR TRIM(v.vol_summary) = '') "
        "   AND t.frontend_visible = 1 "
        f" ORDER BY v.text_id, v.vol_no LIMIT {int(args.limit)}")
    total = d1("SELECT COUNT(*) AS n FROM books_text_volumes "
               "WHERE vol_summary IS NULL OR TRIM(vol_summary)=''")[0]["n"]
    print(f"[卷级索引] 待补 {total} 卷,本轮取 {len(rows)} 卷,并发 {WORKERS}\n", flush=True)
    if not rows:
        print("  没有待补的卷,收工")
        return

    ok = unclear = rej = fail = 0
    now = int(time.time())

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(one, r) for r in rows]):
            row, obj, err = fu.result()
            tag = f"{row.get('clean_name') or row['text_id']} 卷{row['vol_no']}"
            if err is not None:
                fail += 1
                print(f"  ✗ {tag} 失败: {type(err).__name__} {str(err)[:70]}", flush=True)
                continue
            if obj.get("unclear"):
                unclear += 1
                print(f"  ○ {tag} 片段不足以判断,留空({str(obj.get('why') or '')[:24]})", flush=True)
                continue
            bad = violates({k: v for k, v in obj.items() if not k.startswith("_")})
            if bad:
                rej += 1
                print(f"  ⛔ {tag} 触合规红线,丢弃 → {bad[:3]}", flush=True)
                continue
            summary = str(obj.get("summary") or "").strip()
            if len(summary) < 15:
                unclear += 1
                print(f"  ○ {tag} 概要过短,视为无效", flush=True)
                continue
            try:
                d1(f"UPDATE books_text_volumes SET vol_summary={q(summary)}, "
                   f"vol_keywords={q(obj.get('keywords') or [])}, "
                   f"summary_model={q(obj.get('_model'))}, summary_at={now} "
                   f"WHERE text_id={q(row['text_id'])} AND vol_no={int(row['vol_no'])}")
                ok += 1
                print(f"  ✓ {tag}:{summary[:34]}…", flush=True)
            except Exception as e:
                fail += 1
                print(f"  ✗ {tag} 写库失败: {str(e)[:80]}", flush=True)

    print(f"\n[完] 写入 {ok} · 片段不足留空 {unclear} · 合规拦下 {rej} · 失败 {fail}")
    print(f"     剩余待补 {max(total - ok, 0)} 卷;卷级导航接口 /api/text/outline 直接读这张表。")


if __name__ == "__main__":
    main()
