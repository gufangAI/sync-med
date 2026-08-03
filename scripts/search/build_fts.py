#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精确检索层 · FTS5 全文索引 + 术语精确表(混合检索的「关键词那一路」)

立此因(2026-08-03 实测):
  平台检索**只有向量一条路**,D1 里零 FTS 索引。问「《伤寒论》哪一条讲桂枝去芍药」
  这种带确定字面的查询,只能靠向量去撞 —— 撞不撞得上全看运气,而且撞不上时
  没有任何补救通道。补上 FTS5 之后才谈得上 RRF 融合(见 hybrid.js)。

两条实测结论决定了本脚本的形状(创始人已亲自在 D1 上验过,不是推测):
  · D1 支持 FTS5,`tokenize='trigram'` 可用,3 字查询「去芍药」能命中;
  · **trigram 查 2 字打不中**(trigram 需要 ≥3 字才有一个完整 gram),而中医术语
    大量是 2 字(桂枝/芍药/白术/黄芩)→ 纯 FTS 会漏掉一半查询。
  所以本脚本建**两张东西**:trigram FTS 表兜 ≥3 字的自由文本,
  `search_terms` 术语表兜 2 字术语的精确/前缀匹配。缺任何一张,2 字查询都是黑洞。

索引范围(**本轮只索引 preview_120,这是刻意的**):
  154,826 段的全文在 R2,D1 里只有 120 字预览(合计 1,851 万字符)。
  平台铁律「零 R2 移动 / 零 LIST」→ **不许为了建索引去批量读 R2**。
  全文回填是另一个待创始人决策的议题,本脚本不碰,也不留后门。

幂等(可验证,不是口号):
  · 索引状态记在 `books_fts_state`(chunk_id 主键),灌库按「源表 LEFT JOIN 状态表
    取未索引的」拉,重跑只补新增,**不重建全表**;
  · FTS 写入与状态写入放在**同一次 D1 调用**里(D1 的 /query 支持多语句并按 batch 执行,
    已实测返回 2 段结果)→ 不存在「写了 FTS 没写状态」的崩溃窗口;
  · 承诺不成立的话哪里会红:`--verify` 会把 三个数(源表可索引数 / FTS 行数 / 状态行数)
    并排打出来并在不一致时 exit 1。

铁律:
  · 只读 D1 + 写两张新表,零 R2、零 AI 调用、不改任何既有表的数据
  · 本机禁算力 → 设计为在 GitHub Actions 跑(.github/workflows/search-index.yml);
    本地只允许 `--limit` 小样本试

跑法:
  python scripts/search/build_fts.py --phase all              # Actions 全量
  python scripts/search/build_fts.py --phase fts --limit 300  # 本地小样本试
  python scripts/search/build_fts.py --verify                 # 只对账不写
"""
import os, sys, time, argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "content_factory"))
from _ai import d1, q          # noqa: E402  复用公共底座,不再抄一份 D1 访问

def d1r(sql, tries=4):
    """d1() 外面套一层重试 —— **不重写访问代码**,只处理瞬时故障。

    立此因(2026-08-03 本地实测):术语灌库跑到第 40 批时 `TimeoutError: read operation
    timed out`,整轮 exit 1。这种偶发在 Actions 里会把一个跑了半小时的 job 直接打掉,
    而重跑是安全的(全部写入都是 INSERT OR IGNORE / 反连接取增量),所以就地退避重试。
    重试仍失败才抛 —— 不吞异常、不假装成功。
    """
    for i in range(tries):
        try:
            return d1(sql)
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 2 ** i
            print(f"  [重试 {i+1}/{tries-1}] {type(e).__name__}: {str(e)[:80]} → {wait}s 后再来", flush=True)
            time.sleep(wait)


FTS_TABLE   = "books_fts"
STATE_TABLE = "books_fts_state"
TERM_TABLE  = "search_terms"

# D1 单条 SQL 有长度上限(约 100KB)。preview_120 是 120 个汉字 ≈ 360 字节,
# 加 chunk_id/text_id 等约 450 字节/行 → 150 行 ≈ 70KB,留足余量。
DEFAULT_BATCH = 150

# sue_graph_nodes.node_kind → 术语表的 kind(中文)。实测分布:
#   herb 41434 / formula 26303 / syndrome 994 / book 783 / concept 30 / dynasty 6 / person 5
KIND_MAP = {
    "herb": "本草", "formula": "方剂", "syndrome": "证候",
    "book": "书名", "concept": "概念", "person": "人物", "dynasty": "朝代",
}
# 朝代(清/明)全是 1 字、书名不是"术语"意义上的检索词 —— 收进来只会污染 2 字兜底。
# 这里明确只收三类真正的中医术语,其余留给 FTS 那一路。
TERM_KINDS = ("herb", "formula", "syndrome")


# ────────────────────────────── 建表 ──────────────────────────────

def ensure_tables():
    """建表一律 IF NOT EXISTS —— 重跑不重建,也绝不 DROP 任何东西。"""
    d1r(f"""CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
             chunk_id UNINDEXED,
             text_id  UNINDEXED,
             vol_no   UNINDEXED,
             body,
             tokenize='trigram')""")
    d1r(f"""CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
             chunk_id   TEXT PRIMARY KEY,
             text_id    TEXT,
             indexed_at INTEGER)""")
    d1r(f"""CREATE TABLE IF NOT EXISTS {TERM_TABLE} (
             term       TEXT NOT NULL,
             kind       TEXT NOT NULL,
             ref_type   TEXT NOT NULL,
             ref_id     TEXT NOT NULL,
             term_len   INTEGER NOT NULL,
             created_at INTEGER,
             PRIMARY KEY (term, ref_type, ref_id))""")
    # 前缀匹配 `term LIKE '桂枝%'` 走得到这个索引(默认 BINARY collation)
    d1r(f"CREATE INDEX IF NOT EXISTS idx_{TERM_TABLE}_term ON {TERM_TABLE}(term)")
    d1r(f"CREATE INDEX IF NOT EXISTS idx_{TERM_TABLE}_kind ON {TERM_TABLE}(kind, term_len)")


# ─────────────────────────── Phase 1 · FTS ───────────────────────────

def fetch_pending(batch):
    """拉「还没索引」的 chunk。反连接走状态表主键,不做全表扫。"""
    return d1r(f"""SELECT c.chunk_id AS cid, c.text_id AS tid, c.vol_no AS vol, c.preview_120 AS body
                    FROM books_text_chunks c
                    LEFT JOIN {STATE_TABLE} s ON s.chunk_id = c.chunk_id
                   WHERE s.chunk_id IS NULL
                     AND c.preview_120 IS NOT NULL AND TRIM(c.preview_120) <> ''
                   ORDER BY c.chunk_id
                   LIMIT {int(batch)}""")


def write_batch(rows):
    """FTS 与状态在同一次调用里写 —— 两条语句要么都进要么都不进,不留崩溃窗口。"""
    now = int(time.time())
    fts_vals = ", ".join(
        f"({q(r['cid'])}, {q(r['tid'])}, {q(r.get('vol'))}, {q(r['body'])})" for r in rows)
    st_vals = ", ".join(f"({q(r['cid'])}, {q(r['tid'])}, {now})" for r in rows)
    sql = (f"INSERT INTO {FTS_TABLE}(chunk_id, text_id, vol_no, body) VALUES {fts_vals}; "
           f"INSERT OR IGNORE INTO {STATE_TABLE}(chunk_id, text_id, indexed_at) VALUES {st_vals}")
    d1r(sql)
    return len(sql)


def phase_fts(batch, limit):
    total_src = d1r("SELECT COUNT(*) AS n FROM books_text_chunks "
                   "WHERE preview_120 IS NOT NULL AND TRIM(preview_120) <> ''")[0]["n"]
    done0 = d1r(f"SELECT COUNT(*) AS n FROM {STATE_TABLE}")[0]["n"]
    print(f"[FTS] 源表可索引 {total_src} 段,已索引 {done0} 段,待补 {total_src - done0} 段", flush=True)

    t0, n, nb, max_sql = time.time(), 0, 0, 0
    while True:
        if limit and n >= limit:
            print(f"[FTS] 达到 --limit {limit},停(本地小样本模式)", flush=True)
            break
        take = min(batch, limit - n) if limit else batch
        rows = fetch_pending(take)
        if not rows:
            print("[FTS] 没有待索引的 chunk 了", flush=True)
            break
        max_sql = max(max_sql, write_batch(rows))
        n += len(rows)
        nb += 1
        el = time.time() - t0
        rate = n / el if el > 0 else 0
        left = (total_src - done0 - n) / rate if rate > 0 else 0
        print(f"  批 {nb:>5} · 本批 {len(rows)} · 累计 {n} · {el:.1f}s · "
              f"{rate:.1f} 段/秒 · 预计剩余 {left/60:.1f} 分", flush=True)

    el = time.time() - t0
    print(f"[FTS] 本轮写入 {n} 段,耗时 {el:.1f}s,{n/el if el>0 else 0:.1f} 段/秒,"
          f"最大单条 SQL {max_sql} 字节", flush=True)
    return n, el


# ─────────────────────── Phase 2 · 术语精确表 ───────────────────────

def _clean(term):
    """术语规整:去空白;只收 2 字及以上、不超过 40 字的。

    为什么是 2:trigram 打不中 2 字,这张表存在的全部意义就是兜住它们;
    为什么设 40 上限:label 里混着整句描述,收进来会把前缀匹配拖成噪音。
    """
    t = (term or "").strip().replace("　", "")
    if len(t) < 2 or len(t) > 40:
        return None
    return t


def _push(bucket, term, kind, ref_type, ref_id):
    t = _clean(term)
    if t and ref_id:
        bucket[(t, ref_type, str(ref_id))] = (kind, len(t))


def collect_terms():
    """三个数据源汇总。列名全部按 pragma_table_info 实测,不按习惯猜。"""
    bucket = {}

    off, page = 0, 2000
    while True:
        rows = d1r("SELECT id, node_kind, label FROM sue_graph_nodes "
                  f"WHERE node_kind IN ({', '.join(q(k) for k in TERM_KINDS)}) "
                  f"ORDER BY id LIMIT {page} OFFSET {off}")
        if not rows:
            break
        for r in rows:
            _push(bucket, r.get("label"), KIND_MAP.get(r.get("node_kind"), "其他"),
                  "graph_node", r.get("id"))
        off += page
        print(f"    …sue_graph_nodes 已扫 {off} 行,累计术语 {len(bucket)}", flush=True)

    for r in d1r("SELECT entry_id, kind, name_cn FROM biocomp_entries"):
        _push(bucket, r.get("name_cn"), r.get("kind") or "生物成分", "biocomp", r.get("entry_id"))
    for r in d1r("SELECT herb_id, name_cn FROM herb_compare"):
        _push(bucket, r.get("name_cn"), "本草", "herb_compare", r.get("herb_id"))

    return bucket


def phase_terms(batch):
    t0 = time.time()
    before = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
    bucket = collect_terms()
    print(f"[术语] 三源汇总去重后 {len(bucket)} 条(表内已有 {before} 条)", flush=True)

    now, items, n = int(time.time()), list(bucket.items()), 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        vals = ", ".join(
            f"({q(t)}, {q(kind)}, {q(rt)}, {q(rid)}, {ln}, {now})"
            for (t, rt, rid), (kind, ln) in chunk)
        # OR IGNORE + 复合主键 = 重跑只补新增,既有行一个字节都不动
        d1r(f"INSERT OR IGNORE INTO {TERM_TABLE}(term, kind, ref_type, ref_id, term_len, created_at) "
           f"VALUES {vals}")
        n += len(chunk)
        print(f"  术语批 {i//batch + 1} · 累计提交 {n}/{len(items)} · {time.time()-t0:.1f}s", flush=True)

    after = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
    el = time.time() - t0
    print(f"[术语] 新增 {after - before} 条,表内共 {after} 条,耗时 {el:.1f}s", flush=True)
    return after - before, after, el


# ───────────────────────────── 对账 ─────────────────────────────

def verify():
    """三个数并排打。不一致就 exit 1 —— 「幂等」这句话在这里兑现或者穿帮。"""
    src = d1r("SELECT COUNT(*) AS n FROM books_text_chunks "
             "WHERE preview_120 IS NOT NULL AND TRIM(preview_120) <> ''")[0]["n"]
    try:
        fts = d1r(f"SELECT COUNT(*) AS n FROM {FTS_TABLE}")[0]["n"]
        st = d1r(f"SELECT COUNT(*) AS n FROM {STATE_TABLE}")[0]["n"]
        terms = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE}")[0]["n"]
        t2 = d1r(f"SELECT COUNT(*) AS n FROM {TERM_TABLE} WHERE term_len = 2")[0]["n"]
    except Exception as e:
        print(f"[对账] 表还没建或读不到:{e}")
        return 1
    print(f"[对账] 源表可索引 {src} · FTS 行 {fts} · 状态行 {st} · "
          f"术语 {terms}(其中 2 字 {t2})", flush=True)
    if fts != st:
        print(f"[对账] ✗ FTS({fts}) 与状态({st}) 不一致,差 {fts - st} —— 幂等承诺穿帮,查最近一次中断")
        return 1
    print(f"[对账] ✓ FTS 与状态一致;覆盖率 {st}/{src} = {st/src*100:.2f}%" if src else "")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["fts", "terms", "all"], default="all")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="FTS 每批行数(D1 单 SQL 长度上限约 100KB)")
    # 术语行只有 term+两个 id ≈ 80 字节,比 preview_120 短一个量级,批可以大得多
    ap.add_argument("--term-batch", type=int, default=800, help="术语表每批行数")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多索引多少段(本地小样本试,0=全量)")
    ap.add_argument("--verify", action="store_true", help="只对账不写")
    args = ap.parse_args()

    if args.verify:
        sys.exit(verify())

    ensure_tables()
    print("[建表] 三张表就位(IF NOT EXISTS,重跑不重建)", flush=True)

    if args.phase in ("fts", "all"):
        phase_fts(args.batch, args.limit)
    if args.phase in ("terms", "all"):
        phase_terms(args.term_batch)

    sys.exit(verify() if not args.limit else 0)


if __name__ == "__main__":
    main()
