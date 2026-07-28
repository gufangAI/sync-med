# -*- coding: utf-8 -*-
# 判退页捞回 · 第 1 步定位 + 第 2 步空跑判级。**只读**:一个字节都不写回 R2。
#
# 背景:质量闸的 repeat_ngram / max_run / line_dup 三条判据原先没有长度下限,
#   短的方剂组成页会被当垃圾判退(桂枝湯组成 26 字 -> max_run=0.46 判退)。
#   判据已在 1824f50 修好(MIN_LEN_FOR_REPEAT = LONG_PAGE = 40),
#   但已经被误杀的页积在 R2 的 _ocr_rejected/ 下,当时的产线只记计数不记页码,
#   要找回来只能考古 —— 本脚本就是那次考古,顺带证明「为什么必须有 ocr_reject_log」。
#
# 怎么在零 LIST 的前提下把它们找出来(不许 list_objects 全扫桶,那是 Class A 账单):
#   ocr_xf.py 的候选集算法是【确定的】:同一条 D1 查询 -> pages.sort()
#   -> mine = [p for i,p in enumerate(pages) if i%TOTAL==SHARD][:PILOT]。
#   run log 实证(两次 run 的日志都印了同一个池子大小 1814139):
#     run 30108928088  TOTAL=4 PILOT=200 -> 四个 shard 的并集 = pages[0:800]
#     run 30108445286  TOTAL=2 PILOT=6   -> 并集 = pages[0:12](被前者包含)
#   所以重建排序后的前若干个 (book_id, page),逐个 head_object 探 _ocr_rejected/ 即可。
#   窗口取得比 800 宽是为了防 D1 增删导致排序前缀漂移。
#
# 自证判据:找到的对象数应等于 87。这个 87 不是听来的,有两条独立路径互证 ——
#   ① CTO 在 R2 上数出来 87;
#   ② run log 里的「质量闸拦截」逐 shard 相加:
#      run 30108445286 = 3+1 = 4;run 30108928088 = 20+22+25+16 = 83;合计 87。
#   数不够就自动加宽窗口重扫,直到够或撞上 MAX_WINDOW;仍不够则判失败退出非零,
#   把「漏捞」变成可观测的,而不是让它悄悄少几页。
import json
import os
import re
import time
import boto3
import requests
from concurrent.futures import ThreadPoolExecutor
from botocore.exceptions import ClientError

import ocr_quality as q

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]

WINDOW = int(os.environ.get("WINDOW", "2000"))         # 第一轮扫多少个候选(>=800 才覆盖得到已知候选集)
MAX_WINDOW = int(os.environ.get("MAX_WINDOW", "8000"))  # 数不够时自动加宽的上限
EXPECT = int(os.environ.get("EXPECT", "87"))            # 验收判据:应找到多少个判退对象
WORKERS = int(os.environ.get("WORKERS", "16"))
# run log 里两次 run 都印的候选池大小。今天重建出来的池子若与它不同,说明 D1 有增删,
# 排序前缀可能漂移过 —— 不致命(窗口留了余量),但必须在报告里说出来。
EXPECT_POOL = int(os.environ.get("EXPECT_POOL", "1814139"))
# 片段是给人读的:CTO 要靠它把捞回的页分成方剂组成页/目录页/药名著录页。
# 转义成 \uXXXX 就没人看得懂了,分类也就无从谈起。默认出原文,
# 想按本仓「不回吐原文进 public 日志」的老规矩走就设 SNIPPET_MODE=escaped。
SNIPPET_MODE = (os.environ.get("SNIPPET_MODE") or "plain").strip().lower()
SNIPPET_LEN = int(os.environ.get("SNIPPET_LEN", "40"))
# 历史 run 的 allow-list 是取不到的(两次 run log 都印了
# "WARN allow-list unavailable (NoSuchKey) -> no whitelist filter this run"),
# 候选集因此是【未过滤】的全量。今天若 ALLOW_KEY 又存在了,套上去会得到另一个池子、
# 另一套排序前缀,重建就错了。所以默认不套白名单,忠实复刻当时的候选集。
USE_ALLOW = (os.environ.get("USE_ALLOW") or "0").strip() == "1"

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")


def d1_query(sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 query failed: {str(j.get('errors', ''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []


def reqof(g):
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def build_pages():
    """逐字复刻 ocr_xf.py 的候选集构造:同一条 SQL、同一套过滤、同一个 sort()。
    差一个条件就是另一个排序前缀,后面 HEAD 全打空。"""
    allow = None
    if USE_ALLOW:
        try:
            allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())
            print(f"allow-list loaded: {len(allow)} reqs", flush=True)
        except Exception as e:
            print(f"WARN allow-list unavailable ({str(e)[:80]}) -> no whitelist filter", flush=True)
    else:
        print("allow-list 按历史 run 的实况【不加载】(当时 NoSuchKey,候选集未过滤)", flush=True)

    rows = d1_query(
        "SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
        "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0 "
        "AND webp_prefix LIKE 'book/%' AND pan_dir_id IS NOT NULL"
    )
    pages = []
    for r in rows:
        bid, pc, pdid = r.get("book_id"), int(r.get("page_count") or 0), r.get("pan_dir_id")
        if not (bid and pc and pdid):
            continue
        if allow is not None and reqof(bid) not in allow:
            continue
        pages += [(bid, n, pdid) for n in range(1, pc + 1)]
    pages.sort()
    return pages


def rejkey(bid, n):
    return f"_ocr_rejected/{bid}/page_{str(n).zfill(4)}.txt"


def head_one(item):
    """(idx, bid, page) -> idx 或 None。只 head_object,永不 list。"""
    idx, bid, n = item
    k = rejkey(bid, n)
    for attempt in range(3):
        try:
            s3.head_object(Bucket=BUCKET, Key=k)
            return idx
        except ClientError as e:
            code = str((e.response.get("Error") or {}).get("Code") or "")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            time.sleep(1 + attempt)
        except Exception:
            time.sleep(1 + attempt)
    print(f"WARN HEAD 三次都没拿到确定答案: {k}(计为未命中,可能少捞)", flush=True)
    return None


# ── 人工判断辅助:粗分类 ──────────────────────────────────────────────
# 纯启发式,只用来给 CTO 排序,【判断以片段为准】。
# 这些是通用中医词汇(剂量单位、方名后缀、书名),不是语料内容,按本仓惯例直接写字面量
# (ocr_quality.py 的注释里同样直接写了「桂枝湯」「成無己《註解傷寒論》」);
# opsec 要挡的是把 OCR 出来的【原文】回吐进 public 仓,不是挡词典词。
_MARKERS = {
    # 方剂组成页:剂量单位 + 方名后缀 + 主治套语
    "formula": ["兩", "錢", "銖", "升", "枚",
                "湯", "散", "丸", "膏", "去皮", "炙", "主之"],
    # 目录/卷次页
    "toc": ["目錄", "目录", "卷之", "卷第", "篇第", "上卷", "下卷"],
    # 本草药名著录页(桂枝湯之外的另一大类正货:药名 + 出处著录)
    "materia": ["本經", "本经", "別錄", "別录", "名醫",
                "名医", "一名", "經集", "上品", "中品", "下品"],
}


def classify(text):
    """返回 (类名, {类名: 命中标记数})。命中数并列或全 0 -> other。"""
    hits = {g: sum(1 for m in ms if m in text) for g, ms in _MARKERS.items()}
    best = max(hits.values()) if hits else 0
    if best == 0:
        return "other", hits
    top = [g for g, v in hits.items() if v == best]
    if len(top) > 1:
        return "mixed", hits
    return top[0], hits


def snippet(text):
    t = re.sub(r"\s+", " ", text or "").strip()[:SNIPPET_LEN]
    if SNIPPET_MODE == "escaped":
        return t.encode("unicode_escape").decode("ascii")
    return t.replace("|", "\\|")   # markdown 表格里 | 会断列


def old_gate_view(a):
    """这一页在【修复前】的闸门下会不会被判死,以及是被哪一类判据判的。

    不复制阈值数字,直接引用 ocr_quality 的模块常量 —— 本仓吃过「同一判据留两份
    各自漂移」的亏(ocr_degeneracy 那次合并),这里绝不再造第二份。
    这里的「修复前」特指 1824f50 之前:三条重复类判据当年【没有】长度下限。

    注意本函数【不是】当前闸门的镜像,也不该改成镜像:报告里「旧闸」这一列问的是
    "当年会不会误杀它",答案必须停在当年那一版判据上。2026-07-28 又给短页补了
    abs_repeat / model_meta 两条,当前闸门的答案请看同一行的 `改后判级`(来自 q.analyze),
    两列本来就该不一样 —— 一样了才说明有人把考古改成了自证。
    """
    rep_fire = a["repeat_ngram"] >= q.REPEAT_NGRAM_REJECT
    run_fire = a["max_run"] >= q.MAX_RUN_REJECT
    dup_fire = a["n_lines"] >= q.LINE_DUP_MIN_LINES and a["line_dup"] >= q.LINE_DUP_REJECT
    other_fire = (a["len"] >= q.MIN_LEN_FOR_RATIO and
                  (a["garbage_ratio"] >= q.GARBAGE_REJECT or a["single_char"] >= q.SINGLE_CHAR_REJECT))
    which = []
    if rep_fire:
        which.append("repeat_ngram")
    if run_fire:
        which.append("max_run")
    if dup_fire:
        which.append("line_dup")
    if other_fire:
        which.append("garbage/single_char")
    return (rep_fire or run_fire or dup_fire or other_fire), which


def main():
    t0 = time.time()
    pages = build_pages()
    pool = len(pages)
    print(f"候选池重建完成:{pool} 页(run log 当时印的是 {EXPECT_POOL})", flush=True)
    if pool != EXPECT_POOL:
        print(f"NOTE 池子大小与历史 run 不同(差 {pool - EXPECT_POOL}):D1 有过增删,"
              f"排序前缀可能漂移,窗口留的余量就是为这个准备的", flush=True)

    # ── 第 1 步:定位(零 LIST,只 HEAD)──
    hits = []
    scanned = 0
    window = min(WINDOW, pool)
    while True:
        batch = [(i, pages[i][0], pages[i][1]) for i in range(scanned, window)]
        if batch:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for idx in ex.map(head_one, batch):
                    if idx is not None:
                        hits.append(idx)
        scanned = window
        print(f"HEAD 已扫 {scanned} 个候选,命中 {len(hits)}(目标 {EXPECT})", flush=True)
        if len(hits) >= EXPECT or scanned >= min(MAX_WINDOW, pool):
            break
        window = min(scanned * 2, MAX_WINDOW, pool)
        print(f"数不够 -> 自动加宽窗口到 {window} 重扫增量段", flush=True)

    hits.sort()
    last_hit = hits[-1] if hits else -1
    print(f"=== 第1步 定位:HEAD {scanned} 次,命中 {len(hits)} 个判退对象,"
          f"最后一个命中在候选序号 {last_hit} ===", flush=True)

    # ── 第 2 步:GET 命中对象,用【改后的】判据重判(只读,不写回)──
    rows = []
    for idx in hits:
        bid, n, _pdid = pages[idx]
        k = rejkey(bid, n)
        try:
            txt = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read().decode("utf-8", "replace")
        except Exception as e:
            print(f"WARN GET 失败 {k}: {str(e)[:80]}", flush=True)
            continue
        a = q.analyze(txt)
        old_rej, old_which = old_gate_view(a)
        cls, marks = classify(txt)
        rows.append({"idx": idx, "key": k, "book_id": bid, "page": n,
                     "label": a["label"], "reasons": a["reasons"], "score": a["score"],
                     "len": a["len"], "repeat_ngram": a["repeat_ngram"], "repeat_n": a["repeat_n"],
                     "max_run": a["max_run"], "run_period": a["run_period"],
                     "line_dup": a["line_dup"], "n_lines": a["n_lines"],
                     "garbage_ratio": a["garbage_ratio"], "single_char": a["single_char"],
                     "cjk_ratio": a["cjk_ratio"],
                     "old_reject": old_rej, "old_reasons": old_which,
                     "klass": cls, "markers": marks, "snippet": snippet(txt)})

    by_label = {"ok": 0, "suspect": 0, "reject": 0, "empty": 0}
    for r in rows:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    turned_ok = [r for r in rows if r["label"] == "ok"]
    by_class = {}
    for r in turned_ok:
        by_class[r["klass"]] = by_class.get(r["klass"], 0) + 1

    print(f"=== 第2步 空跑判级(改后判据):ok={by_label['ok']} suspect={by_label['suspect']} "
          f"reject={by_label['reject']} empty={by_label['empty']} / 共 {len(rows)} 页 ===", flush=True)
    print(f"    翻成 ok 的成分(启发式粗分类):{sorted(by_class.items(), key=lambda x: -x[1])}", flush=True)

    # found 传 len(hits) 而不是 len(rows):GET 失败的对象仍然是「找到了」,
    # 用 rows 计数会把一次读取失败伪装成「本来就没这么多页」,那正是要防的漏捞。
    _write_report(rows, scanned, pool, last_hit, by_label, by_class, len(hits))
    with open("ocr_recover_hits.json", "w", encoding="utf-8") as f:
        json.dump({"scanned": scanned, "pool": pool, "expect": EXPECT,
                   "found": len(hits), "last_hit_idx": last_hit,
                   "elapsed_s": round(time.time() - t0, 1), "rows": rows},
                  f, ensure_ascii=False, indent=1)
    print("明细已写 ocr_recover_hits.json(供 artifact 上传)", flush=True)

    # ── 验收判据 ──
    if len(hits) == EXPECT:
        print(f"PASS 判据:找到 {len(hits)} 个 == 期望 {EXPECT}", flush=True)
        return 0
    print(f"FAIL 判据:找到 {len(hits)} 个 != 期望 {EXPECT}(已扫到候选序号 {scanned},"
          f"上限 {MAX_WINDOW})。少了 = 窗口不够或来源不止 ocr_xf 一条线;"
          f"多了 = 候选集里混进了别的 run 判退的页,需人工解释。", flush=True)
    return 1


def _write_report(rows, scanned, pool, last_hit, by_label, by_class, found):
    L = []
    L.append("# OCR 判退页捞回 · 第 1-2 步报告(只读,未写回任何 R2 对象)")
    L.append("")
    L.append(f"- 生成时间(UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
             f" / run_id `{os.environ.get('GITHUB_RUN_ID', '')}`")
    L.append(f"- 候选池重建: **{pool}** 页(历史 run log 印的是 {EXPECT_POOL})")
    L.append(f"- 第1步 定位: HEAD **{scanned}** 次(零 LIST),命中 **{found}** 个 "
             f"`_ocr_rejected/` 对象,最后一个命中在候选序号 **{last_hit}**")
    L.append(f"- 验收判据 EXPECT={EXPECT} -> **{'PASS' if found == EXPECT else 'FAIL'}**")
    if found != len(rows):
        L.append(f"- ⚠ 命中 {found} 个但只成功 GET 到 {len(rows)} 个,差 {found - len(rows)} 个读取失败(见 run log)")
    L.append(f"- 第2步 改后判级: ok **{by_label['ok']}** / suspect **{by_label['suspect']}** / "
             f"reject **{by_label['reject']}** / empty **{by_label['empty']}**")
    L.append("")
    L.append("## 翻成 ok 的成分(启发式粗分类,判断以片段为准)")
    L.append("")
    L.append("| 分类 | 条数 | 含义 |")
    L.append("|---|---|---|")
    _desc = {"formula": "方剂组成页(剂量单位+方名后缀)", "toc": "目录/卷次页",
             "materia": "本草药名著录页", "mixed": "多类标记并列", "other": "以上都不像"}
    for kls, cnt in sorted(by_class.items(), key=lambda x: -x[1]):
        L.append(f"| {kls} | {cnt} | {_desc.get(kls, '')} |")
    L.append("")
    L.append("## 逐条明细")
    L.append("")
    L.append(f"片段模式 `{SNIPPET_MODE}`(前 {SNIPPET_LEN} 字)。`旧闸` = 修复前会不会被判死、被哪条判据判的。")
    L.append("")
    L.append("| # | key | 改后判级 | 命中判据 | len | rep | run(p) | ldup(L) | garb | single | cjk | 旧闸 | 分类 | 片段 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x["label"] != "ok", x["idx"])):
        L.append("| {i} | `{k}` | **{lb}** | {rs} | {ln} | {rp} | {ru}(p={rpd}) | {ld}({nl}L) | {ga} | {si} | {cj} | {ov} | {cl} | {sn} |".format(
            i=r["idx"], k=r["key"], lb=r["label"], rs="; ".join(r["reasons"]),
            ln=r["len"], rp=r["repeat_ngram"], ru=r["max_run"], rpd=r["run_period"],
            ld=r["line_dup"], nl=r["n_lines"], ga=r["garbage_ratio"], si=r["single_char"],
            cj=r["cjk_ratio"],
            ov=("+".join(r["old_reasons"]) if r["old_reject"] else "-"),
            cl=r["klass"], sn=r["snippet"]))
    L.append("")
    L.append("---")
    L.append("*本报告只读:未 PUT、未 COPY、未 DELETE 任何 R2 对象;定位全程 head_object,零 list_objects。*")
    L.append("*第 3 步(把 ok 的页放回 `_ocr/`)不在本次范围内,等 CTO 看过本表再定。*")
    md = "\n".join(L)
    with open("ocr_recover_report.md", "w", encoding="utf-8") as f:
        f.write(md)
    print("报告已写 ocr_recover_report.md(供 artifact 上传)", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
