#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识图谱 · 社群检测(GraphRAG Phase 4)

立此因(2026-08-03 调研实证):
  我们的 `sue_graph_nodes` 有 69,555 节点 / 259,686 边,但**从来没被聚过类** ——
  没有 community_id、没有层级。这一个缺口同时造成两个可见问题:
    ① 前台星图是一团毛线球(2,372 个节点纯力导向铺开,中位度数只有 1)
    ② 知识图谱只能做「一跳邻居」,没法参与真正的检索(GraphRAG 的 global search
       靠的是「对社群摘要做 map-reduce」,没有社群就没有那一层)
  对照 microsoft/graphrag:我们已完成的 migration 043(实体抽取 + provenance_tier)
  相当于它的 Phase 3;**Phase 4(社群检测)与 Phase 5(社群摘要)完全空缺**。
  本脚本补 Phase 4。

算法选择(诚实交代取舍):
  GraphRAG 原版用 **Leiden**(`graspologic_native`,Rust 编译扩展)。
  这里先用 **networkx 内置的 Louvain**(`louvain_communities`,纯 Python,networkx≥3.0):
    · 理由:零本地编译、Actions 里 pip 一条命令装好,失败面小
    · 代价:Leiden 在「社群内部连通性」上有理论保证,Louvain 偶尔会切出内部不连通的社群
    · 判据:先跑通拿到真实聚类结构与模块度;**若模块度明显偏低再换 Leiden**,不预先上重依赖
  这个取舍写在这里,是为了以后有人问「为什么不是 Leiden」时有据可查。

铁律:
  · 只读 D1 图表 + 写回 community_id,零 R2、零 AI 调用(纯图算法)
  · 本机禁算力 → 本脚本设计为在 GitHub Actions 跑;本地只允许小样本试算
"""
import os, sys, time, argparse
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content_factory"))
from _ai import d1, q          # noqa: E402  复用公共底座,不再抄一份 D1 访问


def fetch_graph(edge_limit):
    """分页拉全图。D1 单次返回有上限,必须分页,不能一条 SELECT 梭哈。"""
    edges, off, page = [], 0, 5000
    while True:
        rows = d1("SELECT src_node_id AS s, dst_node_id AS t, weight AS w FROM sue_graph_edges "
                  f"LIMIT {page} OFFSET {off}")
        if not rows:
            break
        edges.extend((r["s"], r["t"], float(r.get("w") or 1.0)) for r in rows)
        off += page
        if edge_limit and len(edges) >= edge_limit:
            edges = edges[:edge_limit]
            break
        print(f"    …已拉 {len(edges)} 条边", flush=True)
    return edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-limit", type=int, default=0, help="只取前 N 条边(本地试算用,0=全量)")
    ap.add_argument("--resolution", type=float, default=1.0, help="分辨率:越大社群越碎")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    args = ap.parse_args()

    try:
        import networkx as nx
    except ImportError:
        sys.exit("缺 networkx —— pip install networkx>=3.0")

    t0 = time.time()
    print("[社群检测] 拉图中…", flush=True)
    edges = fetch_graph(args.edge_limit)
    print(f"  边 {len(edges)} 条,耗时 {time.time()-t0:.1f}s", flush=True)
    if not edges:
        sys.exit("图是空的,先确认 sue_graph_edges 有数据")

    G = nx.Graph()
    for s, t, w in edges:
        if s and t and s != t:
            # 同一对节点多条边 → 权重累加(重复关系说明联系更强)
            if G.has_edge(s, t):
                G[s][t]["weight"] += w
            else:
                G.add_edge(s, t, weight=w)
    print(f"  去自环/合并重边后:节点 {G.number_of_nodes()} · 边 {G.number_of_edges()}", flush=True)

    print("[社群检测] 跑 Louvain…", flush=True)
    t1 = time.time()
    comms = nx.community.louvain_communities(G, weight="weight", resolution=args.resolution, seed=42)
    mod = nx.community.modularity(G, comms, weight="weight")
    print(f"  社群 {len(comms)} 个 · 模块度 {mod:.4f} · 耗时 {time.time()-t1:.1f}s", flush=True)
    # 模块度判读:>0.3 说明社群结构真实存在;<0.2 基本等于没结构,别拿去着色骗自己
    verdict = "结构显著" if mod >= 0.3 else ("结构偏弱" if mod >= 0.2 else "几乎无结构")
    print(f"  判读:{verdict}(>0.30 显著 / 0.20-0.30 偏弱 / <0.20 无结构)", flush=True)

    sizes = sorted((len(c) for c in comms), reverse=True)
    print(f"  规模:最大 {sizes[0]} · 中位 {sizes[len(sizes)//2]} · 单点社群 {sum(1 for s in sizes if s==1)}", flush=True)

    if args.dry_run:
        print("\n[dry-run] 不写库。前 8 个社群的代表节点:")
        id2label = {}
        for r in d1("SELECT id, label FROM sue_graph_nodes LIMIT 8000"):
            id2label[r["id"]] = r["label"]
        for i, c in enumerate(sorted(comms, key=len, reverse=True)[:8]):
            names = [id2label.get(n) for n in list(c)[:40] if id2label.get(n)][:8]
            print(f"   社群{i}({len(c)}节点): {' · '.join(names)}")
        return

    now = int(time.time())
    print("[社群检测] 写回 D1…", flush=True)
    written = 0
    for cid, c in enumerate(sorted(comms, key=len, reverse=True)):
        ids = [n for n in c]
        for i in range(0, len(ids), 400):          # D1 单条 SQL 有长度上限,分批 UPDATE
            chunk = ids[i:i + 400]
            inlist = ",".join(q(x) for x in chunk)
            d1(f"UPDATE sue_graph_nodes SET community_id={cid}, community_level=0, "
               f"community_at={now} WHERE id IN ({inlist})")
            written += len(chunk)
        if cid % 20 == 0:
            print(f"    …已写 {written} 个节点", flush=True)

    print(f"\n[完] 社群 {len(comms)} 个 · 写回 {written} 个节点 · 模块度 {mod:.4f}({verdict})")
    print(f"     总耗时 {time.time()-t0:.1f}s")
    print("     下一步(Phase 5):给每个社群生成摘要,才能支撑 GraphRAG 的 global search")


if __name__ == "__main__":
    main()
