#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识图谱 · 社群摘要(GraphRAG Phase 5)

立此因:Phase 4(社群检测)已跑通 —— 68,869 节点切成 371 个社群、模块度 0.5485。
  但**光有社群编号还不能检索**。GraphRAG 的 global search 靠的是
  「对每个社群的摘要做 map-reduce」:先读几百份社群摘要判断哪些相关,再下钻。
  没有摘要,社群就只是个染色用的数字。这一步把每个社群变成一段可读、可检索的文字。

做法(零 R2、零本地算力):
  取该社群里**度数最高的若干节点标签 + 它们之间的边类型** → 免费池生成
  {name, summary, keywords} → 写入 sue_graph_communities。
  只用标签和关系类型,不读原文,所以极便宜。

合规:摘要是**结构性描述**(「本簇以…为核心,关联…」),不是诊疗结论;
  与内容工厂同一套 BANNED 硬闸。判不出就写 unclear 留空,**宁可留空不编**。
"""
import os, sys, time, json, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content_factory"))
from _ai import d1, q, ask_best, parse_json          # noqa: E402  公共底座,不再抄一份

WORKERS = int(os.environ.get("CF_WORKERS", "6"))
PROMPT_VER = "gc-v1-2026-08-03"

BANNED = [
    "克", "钱重", "水煎服", "每日三次", "每次服", "煎服", "顿服", "口服剂量",
    "疗效显著", "治愈率", "包治", "特效", "根治",
    "建议你服用", "你应该服用", "可以服用", "推荐剂量", "用法用量",
]

SYS = (
    "你是中医知识图谱的编目助手。给你一个**图谱社群**(经常一起出现的一簇节点)的成员名单与关系类型,"
    "请概括这一簇在讲什么,供检索时快速判断「这一簇和用户的问题相不相关」。\n"
    "**硬性要求**:\n"
    "  ① 只做**结构性描述**(「本簇以…为核心,关联…」),**绝不写成诊疗建议**;\n"
    "  ② 绝不写剂量、用法、疗效承诺;\n"
    "  ③ 只根据给到的名单概括,**名单里没有的不要凭空补**;\n"
    "  ④ 名单杂乱看不出主题时,输出 {\"unclear\": true} —— 这不算失败,"
    "     **宁可留空,也不要编一个像模像样的假主题**。\n"
    "只输出 JSON:{\"name\":\"6-14字簇名\",\"summary\":\"60-140字概括\",\"keywords\":[\"3-6个关键词\"]}"
)


def violates(obj):
    blob = json.dumps(obj, ensure_ascii=False)
    return [w for w in BANNED if w in blob]


def one(cid):
    """取该社群的高度数节点与关系类型 → 生成摘要。返回 (cid, obj, err)。"""
    try:
        nodes = d1(
            "SELECT n.label, n.node_kind, COUNT(e.id) AS deg "
            "  FROM sue_graph_nodes n "
            "  LEFT JOIN sue_graph_edges e ON e.src_node_id = n.id OR e.dst_node_id = n.id "
            f" WHERE n.community_id = {int(cid)} "
            " GROUP BY n.id ORDER BY deg DESC LIMIT 40")
        if len(nodes) < 3:
            return cid, {"unclear": True, "why": "成员不足 3 个"}, None
        rels = d1(
            "SELECT e.edge_kind AS k, COUNT(*) AS n FROM sue_graph_edges e "
            "  JOIN sue_graph_nodes a ON a.id = e.src_node_id "
            "  JOIN sue_graph_nodes b ON b.id = e.dst_node_id "
            f" WHERE a.community_id = {int(cid)} AND b.community_id = {int(cid)} "
            " GROUP BY e.edge_kind ORDER BY n DESC LIMIT 6")
        members = "、".join(f"{r['label']}({r['node_kind']})" for r in nodes[:30])
        relstr = "、".join(f"{r['k']}×{r['n']}" for r in rels) or "(无内部关系)"
        prompt = (f"社群成员(按连接数从高到低,共 {len(nodes)} 个取前 30):\n{members}\n"
                  f"簇内关系类型:{relstr}\n\n请按要求输出 JSON。")
        txt, model = ask_best(SYS, prompt, max_tokens=900, need="{")
        obj = parse_json(txt)
        obj["_model"] = model
        obj["_size"] = len(nodes)
        return cid, obj, None
    except Exception as e:
        return cid, None, e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    d1("""CREATE TABLE IF NOT EXISTS sue_graph_communities (
            community_id  INTEGER PRIMARY KEY,
            name          TEXT,
            summary       TEXT,
            keywords      TEXT,
            member_count  INTEGER,
            gen_model     TEXT,
            prompt_ver    TEXT,
            status        TEXT DEFAULT 'draft',
            created_at    INTEGER,
            updated_at    INTEGER)""")

    todo = d1(
        "SELECT n.community_id AS cid, COUNT(*) AS sz FROM sue_graph_nodes n "
        " WHERE n.community_id IS NOT NULL "
        "   AND n.community_id NOT IN (SELECT community_id FROM sue_graph_communities WHERE summary IS NOT NULL) "
        " GROUP BY n.community_id HAVING sz >= 3 "
        f" ORDER BY sz DESC LIMIT {int(args.limit)}")
    total = d1("SELECT COUNT(DISTINCT community_id) AS n FROM sue_graph_nodes WHERE community_id IS NOT NULL")[0]["n"]
    done = d1("SELECT COUNT(*) AS n FROM sue_graph_communities WHERE summary IS NOT NULL")[0]["n"]
    print(f"[社群摘要] 社群总数 {total} · 已有摘要 {done} · 本轮取 {len(todo)} 个(按规模从大到小)\n", flush=True)
    if not todo:
        print("  没有待生成的社群,收工")
        return

    ok = unclear = rej = fail = 0
    now = int(time.time())
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(one, r["cid"]) for r in todo]):
            cid, obj, err = fu.result()
            if err is not None:
                fail += 1
                print(f"  ✗ 社群{cid} 失败: {type(err).__name__} {str(err)[:70]}", flush=True)
                continue
            if obj.get("unclear"):
                unclear += 1
                print(f"  ○ 社群{cid} 看不出主题,留空({str(obj.get('why') or '')[:24]})", flush=True)
                continue
            bad = violates({k: v for k, v in obj.items() if not k.startswith("_")})
            if bad:
                rej += 1
                print(f"  ⛔ 社群{cid} 触合规红线 → {bad[:3]}", flush=True)
                continue
            name = str(obj.get("name") or "").strip()[:40]
            summ = str(obj.get("summary") or "").strip()
            if len(summ) < 15:
                unclear += 1
                print(f"  ○ 社群{cid} 概要过短,视为无效", flush=True)
                continue
            try:
                d1("INSERT OR REPLACE INTO sue_graph_communities "
                   "(community_id,name,summary,keywords,member_count,gen_model,prompt_ver,status,created_at,updated_at) "
                   f"VALUES ({int(cid)},{q(name)},{q(summ)},{q(obj.get('keywords') or [])},"
                   f"{int(obj.get('_size') or 0)},{q(obj.get('_model'))},{q(PROMPT_VER)},'draft',{now},{now})")
                ok += 1
                print(f"  ✓ 社群{cid}「{name}」{summ[:38]}…", flush=True)
            except Exception as e:
                fail += 1
                print(f"  ✗ 社群{cid} 写库失败: {str(e)[:80]}", flush=True)

    print(f"\n[完] 写入 {ok} · 看不出主题留空 {unclear} · 合规拦下 {rej} · 失败 {fail}")
    print(f"     累计已有摘要 {done + ok}/{total} 个社群 —— 这是 GraphRAG global search 的原料")


if __name__ == "__main__":
    main()
