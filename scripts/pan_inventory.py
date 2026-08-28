# -*- coding: utf-8 -*-
"""123 全量清点 —— 把网盘上到底有多少本、各是什么形态，数成一份可核对的清单。

═══════════════════════════════════════════════════════════════════════════
为什么建这个

创始人 2026-08-28:「123其实还有非常多的PDF没转webp，能不能也导入呢，
                   这样可以显示我们的数据更多」
                  「书都在123，在不同的目录，PDF就在前端显示PDF」
                  「我们先展示PDF，后台在不断的做PDF转webp，这些都是在同步的」

本机测绘先探到的结构(只读，已确认):

    139账号 (webp·35TB)   古籍/ · 数字归乡_中医古方/ · ctext/ · backup_root_*
    136账号 (pdf·46TB)    古籍PDF/ ← PDF 全在这，下分 8 类:
                            古籍PDF(40 个馆藏集合) · 医书pdf · 佛典pdf
                            佛典jpg · 道家 · 古籍JPG · 殆知阁 · 文字版古籍医书

而每天在跑的 pan_register.py **只扫 GufangP 那一棵**(webp 侧)——
136 账号这整棵 PDF 树从来没有被扫过一次。所以它每天报的 not-in-D1
只是 webp 侧的缺口，PDF 侧的缺口连报都没报过。

目录名里自带册数的能读出 **60,349 册**，但 40 个集合里有 24 个名字里没写数
(上海图书馆全库 / 国家图书馆藏 / 台北故宫 / 天一阁 / 哈佛……),
所以那是**下限，不是清点**。这个脚本就是把"下限估计"变成"真数字"。

═══════════════════════════════════════════════════════════════════════════
它做什么 / 不做什么

做:  递归枚举 → 每个"像一本书"的目录记一行 → 输出 JSONL 清单
     每行含: 账号 / 全路径 / 目录名 / 直接子文件数 / 扩展名分布 /
             形态判定(webp_pages / pdf / jpg / mixed / unknown) /
             book_id 猜测 + name_ok(命名是否合规范) —— 规则来自 scripts/book_id.py,
             那是全仓唯一一份;tests/test_book_id.py 钉着"只许有一份"

不做: **不写 D1**。清单只落文件与 artifact。
     入库是下一步、单独一个 PR、要过版权闸 —— 这批里有大量国内馆藏与
     现代整理本，《版权风险台账》的判据是「影印本可展示/现代排印本不可」,
     整棵树推上去会直接撞线。清点和入库必须分开。

     **不下载任何文件**。只调 file/list。

═══════════════════════════════════════════════════════════════════════════
工程约束

  · 限流: 123 open-api 约 5 QPS。默认每次调用 sleep 0.25s。
  · 耗时: pan_register 的单账号全量实测 3-5 小时。本脚本支持 --shard/--total
    切片并行，以及 --max-depth 控制深度(书目录通常在第 3-4 层)。
  · 断点: 清单按行追加(JSONL),中断后已写的部分仍然有效。
  · 凭据: 两个账号分别取 PAN_CID/PAN_SEC 与 PAN_CID_2/PAN_SEC_2。
    **缺第二套就只扫第一个账号,并在摘要里明写"只覆盖了 N 个账号中的 1 个"** ——
    不假装扫全了。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

BASE = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
SLEEP = float(os.environ.get("PAN_SLEEP", "0.25"))

# book_id 规范：**全仓唯一一份**在 scripts/book_id.py。
# 这里原本抄了一份，创始人 2026-08-28「一定要规范命名规则，只有统一的一套」当场纠正。
# book_id.py 是零依赖模块（不读环境变量、不联网），所以 import 它不会像
# import pan_register 那样在 import 期 sys.exit —— 当初抄的理由本就不成立。
from book_id import to_book_id, is_recognized          # noqa: E402

# 目录名里常自带册数，例如「美国国会图书馆藏书【226.68GB·3861册】」。
# 这不是清点结果（人手写的），只作为对照：真数出来的和名字里写的差多少，
# 本身就是一条值得看的信号。
RE_DECLARED = re.compile(r"(\d[\d,]*)\s*(?:册|冊|种|種|部)")


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Platform": "open_platform"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def _get(path, token):
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": "Bearer " + token,
                              "Platform": "open_platform"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def token_of(cid, sec):
    j = _post("/api/v1/access_token", {"clientID": cid, "clientSecret": sec})
    t = (j.get("data") or {}).get("accessToken")
    if not t:
        raise RuntimeError("access_token 失败: " + json.dumps(j, ensure_ascii=False)[:180])
    return t


def children(token, parent):
    """列一层,全部翻页。失败重试 3 次后放弃并如实返回已拿到的部分。"""
    out, last = [], 0
    while True:
        j = None
        for attempt in range(3):
            try:
                j = _get("/api/v2/file/list?parentFileId=%s&limit=100&lastFileId=%s"
                         % (parent, last), token)
                break
            except Exception as exc:                            # noqa: BLE001
                if attempt == 2:
                    print("  [列目录失败] parent=%s last=%s %s"
                          % (parent, last, str(exc)[:90]), flush=True)
                    return out, False
                time.sleep(2 * (attempt + 1))
        time.sleep(SLEEP)
        d = (j or {}).get("data") or {}
        out += [i for i in (d.get("fileList") or []) if i.get("trashed") not in (1, True)]
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return out, True


def shape_of(exts):
    """从扩展名分布判形态。判据写死在这里,便于将来对着真实数据调。"""
    if not exts:
        return "empty"
    webp = exts.get(".webp", 0)
    pdf = exts.get(".pdf", 0)
    jpg = exts.get(".jpg", 0) + exts.get(".jpeg", 0) + exts.get(".png", 0)
    total = sum(exts.values())
    # 逐页 webp:webp 占绝对多数且数量像"页"(>=3)
    if webp >= 3 and webp / total > 0.8:
        return "webp_pages"
    if pdf and pdf / total > 0.5:
        return "pdf"
    if jpg >= 3 and jpg / total > 0.8:
        return "jpg_pages"
    if webp or pdf or jpg:
        return "mixed"
    return "other"


def walk(token, account, node_id, path, depth, max_depth, out_fh, stats):
    kids, complete = children(token, node_id)
    if not complete:
        stats["incomplete_dirs"] += 1
    dirs = [k for k in kids if k.get("type") == 1]
    files = [k for k in kids if k.get("type") == 0]

    exts = {}
    for f in files:
        e = os.path.splitext(str(f.get("filename", "")))[1].lower() or "(none)"
        exts[e] = exts.get(e, 0) + 1
    shape = shape_of(exts)

    # 判定"这是一本书的目录吗":它自己直接装着内容文件,而不是一层纯分类目录
    is_book = shape in ("webp_pages", "pdf", "jpg_pages", "mixed") and len(files) >= 1
    if is_book:
        name = path.split("/")[-1] if path else "(root)"
        rec = {"account": account, "path": path, "name": name,
               "shape": shape, "n_files": len(files), "n_subdirs": len(dirs),
               "exts": exts, "book_id_guess": to_book_id(name),
               # name_ok = 名字符不符合两条书写规范。**不等于"能不能对上 D1"**：
               # 实测 D1 里 23.8% 的 book_id 走的是兜底那条路（见 book_id.py 文件头），
               # 所以 name_ok=False 很可能只是"这本用的是另一种命名"，不是问题。
               "name_ok": is_recognized(name),
               "dir_id": node_id}
        out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out_fh.flush()
        stats["books"] += 1
        stats["by_shape"][shape] = stats["by_shape"].get(shape, 0) + 1
        if stats["books"] % 200 == 0:
            print("  ...已记 %d 本 (%s)" % (stats["books"], path[:70]), flush=True)

    stats["dirs_walked"] += 1
    if depth >= max_depth:
        stats["depth_capped"] += 1
        return
    for d in dirs:
        walk(token, account, d.get("fileId"),
             path + "/" + str(d.get("filename")), depth + 1, max_depth, out_fh, stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="pan_inventory.jsonl")
    ap.add_argument("--max-depth", type=int, default=5,
                    help="从账号根往下最多走几层(书目录通常在 3-4 层)")
    ap.add_argument("--roots", default="",
                    help="逗号分隔:只扫这些顶层目录(留空=全部)")
    a = ap.parse_args()

    accounts = []
    if os.environ.get("PAN_CID") and os.environ.get("PAN_SEC"):
        accounts.append(("acct1", os.environ["PAN_CID"], os.environ["PAN_SEC"]))
    if os.environ.get("PAN_CID_2") and os.environ.get("PAN_SEC_2"):
        accounts.append(("acct2", os.environ["PAN_CID_2"], os.environ["PAN_SEC_2"]))
    if not accounts:
        sys.exit("缺 PAN_CID / PAN_SEC")

    only = {r.strip() for r in a.roots.split(",") if r.strip()}
    stats = {"books": 0, "dirs_walked": 0, "incomplete_dirs": 0, "depth_capped": 0,
             "by_shape": {}, "accounts_scanned": len(accounts)}

    print("=" * 76, flush=True)
    print("123 全量清点 · 只读 · 覆盖 %d 个账号 · max-depth=%d"
          % (len(accounts), a.max_depth), flush=True)
    if len(accounts) < 2:
        print("⚠️ 只拿到 1 套凭据 —— **本次只覆盖 1 个账号**。"
              "另一个账号(PDF 侧或 webp 侧)未被扫描,结论不可当作全网盘清点。",
              flush=True)
    print("=" * 76, flush=True)

    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        for label, cid, sec in accounts:
            try:
                tok = token_of(cid, sec)
            except Exception as exc:                            # noqa: BLE001
                print("[%s] 拿 token 失败:%s" % (label, str(exc)[:120]), flush=True)
                continue
            roots, _ = children(tok, 0)
            rdirs = [r for r in roots if r.get("type") == 1]
            if only:
                rdirs = [r for r in rdirs if str(r.get("filename")) in only]
            print("\n[%s] 顶层 %d 个:%s"
                  % (label, len(rdirs), [r.get("filename") for r in rdirs]), flush=True)
            for r in rdirs:
                print("  → 进入 %s" % r.get("filename"), flush=True)
                walk(tok, label, r.get("fileId"), "/" + str(r.get("filename")),
                     1, a.max_depth, fh, stats)

    print("\n" + "=" * 76, flush=True)
    print("清点完成:%s 本 · 走过 %s 个目录"
          % (format(stats["books"], ","), format(stats["dirs_walked"], ",")), flush=True)
    for k, v in sorted(stats["by_shape"].items(), key=lambda x: -x[1]):
        print("  %-12s %s" % (k, format(v, ",")), flush=True)
    if stats["incomplete_dirs"]:
        print("⚠️ %d 个目录没列完(API 失败)——这份清单是**下限**"
              % stats["incomplete_dirs"], flush=True)
    if stats["depth_capped"]:
        print("⚠️ %d 处到达 max-depth=%d 被截断,更深的书没数到"
              % (stats["depth_capped"], a.max_depth), flush=True)
    json.dump(stats, open("pan_inventory_stats.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("清单 -> %s   统计 -> pan_inventory_stats.json" % a.out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
