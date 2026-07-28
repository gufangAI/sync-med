# -*- coding: utf-8 -*-
# CC-down: OCR worker - GitHub Actions + RapidOCR (onnxruntime PP-OCR, free cloud concurrency).
# R2 med-book images (book/{id}/page_NNNN.webp) -> RapidOCR -> text -> R2 _ocr/{id}/page_NNNN.txt (SueAI fuel).
# RapidOCR(onnxruntime) avoids paddle's AVX512 SIGILL on runners + ships models in the wheel (no baidu CDN).
import os, io, re, json, sys, boto3, requests, numpy as np
from PIL import Image
from botocore.exceptions import ClientError
from rapidocr_onnxruntime import RapidOCR
import ocr_quality
import ocr_reject_log   # 判退页台账:判退的是【哪几页】必须留下名单,不能只留一个计数(见该模块开头)
import pan_fetch   # OCR 质量闸:重复短语/整行复读/乱码率检测(纯规则),不合格标记不入库

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")
engine = RapidOCR()

# 2026-07-14: ALLOW_KEY 指向的 R2 对象已不存在(NoSuchKey),此前无 try/except 直接让全部 256 shard
# 秒失败、OCR 停摆 30+ 小时。改成容错(同 sync.py 对 ALLOW_KEY 的处理:找不到就不设白名单限制,
# 不是当致命错误崩掉);allow=None 时下面过滤条件不生效,处理 PAGES 清单里的全部书。
allow = None
try:
    allow = set(s3.get_object(Bucket=BUCKET, Key=os.environ["ALLOW_KEY"])["Body"].read().decode().split())
    print(f"allow-list loaded: {len(allow)} reqs", flush=True)
except Exception as e:
    print(f"WARN allow-list unavailable ({str(e)[:80]}) -> no whitelist filter this run, processing all of PAGES manifest", flush=True)


def reqof(g):
    m = re.search(r"(\d{3})-?(\d{4})", g)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _rebuild_pages_from_d1():
    """Rebuild the page manifest from D1, the same self-heal sync.py uses.

    It must return the SHAPE sync.py writes -- {book_id: {"pc": pages, "pdid":
    123 folder id}} -- because the two scripts share one R2 manifest object.
    This function used to select page_count only and hand back bare ints, and
    the caller cached that back to R2. So every time the manifest went missing,
    the fallback silently rewrote the shared manifest WITHOUT folder ids, and
    every pan read afterwards had no folder to fetch from. That is a manifest
    downgrade, not a fallback."""
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise RuntimeError("PAGES_KEY unreadable and CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN missing -> cannot rebuild from D1")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    # Identical to sync.py's rebuild query: same columns, same filters, so the
    # two writers of this manifest can never disagree about its contents.
    sql = ("SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
           "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
    r = requests.post(url, headers={"Authorization": "Bearer " + tok}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 query failed: {str(j.get('errors', ''))[:200]}")
    rows = (j.get("result") or [{}])[0].get("results") or []
    return {row["book_id"]: {"pc": int(row["page_count"]), "pdid": row.get("pan_dir_id")}
            for row in rows if row.get("book_id") and row.get("page_count")}


def _is_new_fmt(p):
    """True when the manifest carries 123 folder ids (values are dicts).

    Same test sync.py applies before trusting the cached manifest. Reading an
    old int-only manifest does not fail loudly -- page counts are all still
    there, so the shard builds its full key list and then misses every single
    page for want of a folder id."""
    return bool(isinstance(p, dict) and p and isinstance(next(iter(p.values())), dict))


# Zero-LIST: read the page manifest and build keys directly; never list_objects
# over the whole bucket (full bucket scans were the cost spike).
PAGES_KEY = os.environ.get("PAGES_KEY", "_cc/med_pages.json")
PAGES = None
try:
    PAGES = json.loads(s3.get_object(Bucket=BUCKET, Key=PAGES_KEY)["Body"].read().decode("utf-8"))
except Exception as e:
    print(f"WARNING: PAGES_KEY {PAGES_KEY} unreadable ({e}) -> rebuilding from D1...", flush=True)
if not _is_new_fmt(PAGES):
    if PAGES is not None:
        print("PAGES manifest is the old int-only shape (no 123 folder ids) -> forcing a D1 rebuild", flush=True)
    PAGES = _rebuild_pages_from_d1()
    print(f"D1 rebuild ok: {len(PAGES)} books", flush=True)
    try:
        s3.put_object(Bucket=BUCKET, Key=PAGES_KEY, Body=json.dumps(PAGES, ensure_ascii=False).encode("utf-8"))
        print(f"cached back to R2: {PAGES_KEY}", flush=True)
    except Exception as e2:
        print(f"WARNING: cache-back failed ({e2}) -> next run will rebuild again, not fatal", flush=True)
_with_pdid = sum(1 for _v in PAGES.values() if isinstance(_v, dict) and _v.get("pdid"))
print(f"manifest {PAGES_KEY}: {len(PAGES)} books, {_with_pdid} of them carry a 123 folder id", flush=True)

def _pagecount(v):
    """Read a page count out of a manifest value, whichever shape it is in.

    The manifest used to map book_id -> page_count. On 2026-07-22 sync.py began
    writing book_id -> {"pc": count, "pdid": <123 folder id>} so pages could be
    fetched from the pan instead of R2 (see its rebuild query). This file kept
    reading the old shape and died on int(dict): 256 of 257 jobs, every run, for
    five days, with 168GB of scans waiting behind it. Nobody noticed because the
    fleet watch pointer had been moved to ocr_ndl.yml three days later.

    Accepting BOTH shapes rather than pinning the new one is deliberate -- the
    two writers still disagree. sync.py writes the dict, while the D1 fallback
    a few lines above writes bare ints, so a reader that only understood the
    newer shape would break again the first time that fallback ran."""
    if isinstance(v, dict):
        try:
            return int(v.get("pc") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# Shard by BOOK, not by page. Round-robin over pages (i % TOTAL == SHARD) was
# harmless while images came from R2: every key was an independent GET. Reading
# from the 123 pan is not like that -- a page is located by listing its book's
# folder, so a shard wants all of one book's pages back to back and lists that
# folder once. With 256 shards over books averaging ~176 pages, page-level
# striding put each consecutive page of a shard in a different book nearly
# every time, i.e. about one folder listing PER PAGE -- millions of listings
# for a full pass, against a 123 API the download line shares. Assigning whole
# books lets the per-folder cache in pan_fetch do what it was written for.
books = []
for bid, _pv in PAGES.items():
    pc = _pagecount(_pv)
    if (allow is not None and reqof(bid) not in allow) or pc <= 0:
        continue
    books.append((bid, pc))
books.sort()
pool_pages = sum(pc for _b, pc in books)
mine = []
for _i, (bid, pc) in enumerate(books):
    if _i % TOTAL == SHARD:
        mine += [f"book/{bid}/page_{n:04d}.webp" for n in range(1, pc + 1)]
_pilot = os.environ.get("PILOT", "").strip()
if _pilot:
    # Spread the trial across the shard rather than taking the first N pages.
    # A contiguous head made the 2026-07-27 pilots useless: with total=8 the
    # first six pages of every shard came out of the SAME (alphabetically
    # first) book, so all eight jobs reported the identical number and it
    # described one book instead of the pool.
    _n = max(1, int(_pilot))
    mine = mine[::max(1, len(mine) // _n)][:_n]
print(f"shard {SHARD}/{TOTAL} imgs {len(mine)}/{pool_pages} books {len(books)} pilot={_pilot or 'no'}", flush=True)

# 2026-07-19修复:原来每页一次 s3.head_object() 查重(每6小时对全量候选池重复扫一遍),
# 是R2 HeadObject成本异常的真凶之一(实测最近24小时guyaofang-lib桶243万次HeadObject)。
# 改用GitHub Actions cache本地ledger.json记账,跟今天已经验证过、跑通的ocr_ndl.py同一套方法。
LEDGER = "ledger.json"
ledger = set()
if os.path.exists(LEDGER):
    try:
        ledger = set(json.load(open(LEDGER, encoding="utf-8")))
    except Exception:
        ledger = set()
print(f"ledger已有 {len(ledger)} 条记录", flush=True)

done = 0; skipped = 0; rejected = 0
rejects = []   # 本轮判退明细,收尾时一次性合并进 _ledger/rejected_ocr_{SHARD}.json
USE_PAN = False
# Two failure modes that look identical in a "0 new" line but need opposite
# fixes: the book has no 123 folder id in the manifest (refresh the manifest),
# or the folder exists and the page is not in it (check the migration or the
# filename convention). Counting them apart is the whole point.
miss_nopdid = 0
miss_nopage = 0
_sampled = False
# 2026-07-22 止血: book/ 影像已于 2026-07-17 迁 123、R2 里已删空,直读会全部 NoSuchKey。
# 进逐页 OCR 循环前先 HEAD 探测本 shard 首个 key,不在(=R2 book/ 已空)则整 shard 跳过,
# 避免逐个 GET 全 404 烧 Class B(2026-07-21 sync+ocr 共刷 566万次≈$2.08)。
# 参照姊妹脚本 ocr_ndl.py 的做法:R2 直读已废弃,该改走 123,此处先止血跳过。
if mine:
    try:
        s3.head_object(Bucket=BUCKET, Key=mine[0])
    except ClientError as _he:
        if _he.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            print(f"=== shard {SHARD} R2 book/ 已空(已迁123),整 shard 跳过,不烧 Class B ===", flush=True)
            # 2026-07-27: R2 empty is the expected steady state now, not an error --
            # book/ moved to the 123 pan on 2026-07-17 and sync.py has been reading
            # it from there ever since. Exiting here was correct as a bleed stop on
            # 07-22, but five days of zero output is not a resting state for the OCR
            # line. Switch to the pan when credentials exist; keep the old exit when
            # they do not, so a missing secret can never re-open the Class B bleed.
            if pan_fetch.available():
                USE_PAN = True
                print(f"=== shard {SHARD} R2 book/ empty -> reading pages from 123 pan ===", flush=True)
            else:
                print(f"=== shard {SHARD} R2 empty and no pan credentials -> skip ===", flush=True)
                json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
                sys.exit(0)
        else:
            # Only re-raise codes we did NOT handle. This `raise` used to be
            # unconditional and was unreachable, because the NoSuchKey branch
            # always ended in sys.exit(0). Replacing that exit with "switch to
            # the pan" let control flow reach it for the first time and re-threw
            # an exception that had just been handled -- the pilot printed
            # "reading pages from 123 pan" and then died with exit 1 on the very
            # next line. Handling a case at the top of a block does not end the
            # block.
            raise
for k in mine:
    txtkey = "_ocr/" + k[len("book/"):].rsplit(".", 1)[0] + ".txt"
    if txtkey in ledger:
        skipped += 1; continue
    try:
        if USE_PAN:
            # k == book/{bid}/page_{NNNN}.webp
            _rest = k[len("book/"):]
            _bid, _fn = _rest.split("/", 1)
            _pno = int(_fn.rsplit(".", 1)[0].split("_")[-1])
            _pv = PAGES.get(_bid)
            _pdid = _pv.get("pdid") if isinstance(_pv, dict) else None
            if not _pdid:
                # No folder id for this book -- it is either not migrated to the
                # pan yet or pending a compliance decision. Nothing to fetch;
                # count it and move on rather than failing the shard.
                miss_nopdid += 1
                continue
            b = pan_fetch.fetch_page(_pdid, _pno)
            if not b:
                miss_nopage += 1
                if not _sampled:
                    # Once per shard, say what the folder actually holds. A bare
                    # miss count cannot tell "wrong credentials / wrong folder"
                    # (listing comes back empty) from "page genuinely absent"
                    # (listing is full, this name is not in it) from "filenames
                    # do not match page_NNNN.webp at all".
                    _sampled = True
                    _idx = pan_fetch.dir_index(_pdid)
                    print(f"pan-miss sample: book={_bid} folder={_pdid} page={_pno} -> "
                          f"folder lists {len(_idx)} files, first names {sorted(_idx)[:3]}", flush=True)
                continue
        else:
            b = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        im = np.array(Image.open(io.BytesIO(b)).convert("RGB"))[:, :, ::-1]  # PIL decodes webp->RGB; RapidOCR wants BGR (cv2)
        res, _ = engine(im)
        txt = "\n".join(l[1] for l in (res or []))   # res = [[box, text, score], ...]
        # OCR 质量闸:reject(短语刷屏/整行复读/乱码)不进 _ocr/ 燃料池,落 _ocr_rejected/ 标记
        # 用 analyze 而不是 verdict:verdict 只给 label,拿不到 reasons/ratio,
        # 台账就又退回成「只有一个计数」——那正是这次要治的病。判决逻辑完全一样
        # (verdict 内部就是 analyze()["label"]),不改行为。
        qa = ocr_quality.analyze(txt)
        if qa["label"] == "reject":
            rejkey = "_ocr_rejected/" + k[len("book/"):].rsplit(".", 1)[0] + ".txt"
            stored = True
            try:
                s3.put_object(Bucket=BUCKET, Key=rejkey, Body=txt.encode("utf-8"))
            except Exception:
                # PUT 失败被吞的页在 R2 里一个字节都不存在:既不在 _ocr/ 也不在
                # _ocr_rejected/。台账是它唯一的存在证明,所以照记,只是标 stored=False。
                stored = False
            ocr_reject_log.record(rejects, rejkey, qa, stored)
            ledger.add(txtkey); rejected += 1
            continue
        s3.put_object(Bucket=BUCKET, Key=txtkey, Body=txt.encode("utf-8"))
        done += 1
        ledger.add(txtkey)
        if done % 20 == 0:
            print(f"done {done}/{len(mine)}", flush=True)
    except Exception as e:
        print("ERR", k, str(e)[:50], flush=True)

json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
# 判退明细台账(每 shard 一次 GET + 一次 PUT,零 LIST)。
# 汇总台账里的 rej 只是个数字;要知道被判退的是【哪几页】只能靠它。
ocr_reject_log.flush(s3, BUCKET, "ocr", SHARD, rejects)
s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocr_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine), "ocrd": skipped + done,
                               "new": done, "rej": rejected,
                               "miss_nopdid": miss_nopdid, "miss_nopage": miss_nopage}).encode())
# The misses are printed with the counts: a shard that reads from the pan and
# produces nothing must say WHY. A bare "0 new" is exactly the silent zero that
# let this line sit dead for five days.
print(f"=== shard {SHARD} OCR {done} new, {skipped + done}/{len(mine)} done, "
      f"quality-gate rejected {rejected}, pan-miss no-pdid {miss_nopdid} / page-absent {miss_nopage} ===",
      flush=True)
