# coding: utf-8
"""
Formula-library mining worker (GitHub Actions matrix shard).

Ports the pilot algorithm (regex candidate windows -> free-pool LLM
structuring -> 3-gate hard substring verification -> per-book name-cap
dedup) verbatim. All CJK-bearing constants / regex / gate / parse / prompt
content in this file is machine-extracted (inspect.getsource() and compiled
.pattern strings) from the validated docs/new pilot script and then
machine-escaped to \\uXXXX -- not hand-retyped -- so there is zero
transcription drift from the 3-book real run that validated the algorithm,
and the committed source stays ASCII (this repo's convention for CJK-bearing
extraction prompts, see tcm_judge.py's base64 PROMPT for precedent).

Only the plumbing is new for production scale, same spirit as align_full.py
in this repo:
  - env/secrets instead of a local .env file + local pool-call module
  - operates on a *segment* of a pre-ranked worklist (formula_worklist.json,
    an ordered array of text_id, committed alongside this script), sliced
    by OFFSET/LIMIT; the segment is then expanded to *volume* work-units and
    those are dealt round-robin across SHARD/TOTAL. Sharding at volume rather
    than book granularity is what makes the big compendia tractable: \u666e\u6d4e\u65b9
    is 375 volumes / 27620 windows on its own, i.e. ~18h and a blown 6h job
    ceiling if one shard had to swallow the whole book.
  - SOURCE=d1 (default) reads the compliance-gated corpus via D1; SOURCE=
    r2text reads a committed {book,key} worklist for the flat one-txt-per-
    book mirror. Either way R2 keys come from D1 rows or a committed file --
    never from a bucket listing.
  - full candidate-window set, no artificial per-book cap (the pilot's
    600-window budget cap lived in its caller, not in stage1_book); the only
    throttle left is PER_NAME_CAP, keyed on (text_id, name)
  - this shard's own output JSONL doubles as the resume ledger: every
    attempted window (any status) is one line, so a resumed run (cache
    restored to the same path) skips what is already there
  - 3-provider LLM chain -- agnes first, xunfei second (multi-key pool),
    zhipu last -- with 429/412 backoff and provider fail-over, replacing
    the pilot's local free-pool module (not available on Actions runners)
  - the 3 hard gates are unchanged: extracted name / quote / every herb
    name must be an exact substring of the source window, in a CJK-only
    normalized space (OCR line-break / spacing / punctuation noise is
    normalized away; character order and wording are never relaxed)

Read-only against D1/R2: D1 only SELECT, R2 only get_object against a key
read from books_text_volumes.r2_key -- never list_objects/get_paginator
(enforced repo-wide by .github/workflows/guard_no_list.yml). Writes
nothing to any production table/bucket; only output is a JSONL file
uploaded by the workflow as a build artifact.

Modes:
  (default)          -- shard worker, reads OFFSET/LIMIT/SHARD/TOTAL env,
                         writes formula_mine_result_shard{SHARD}.jsonl
  --summarize DIR     -- merges every shard's *.jsonl under DIR, keeps only
                         status=="ok" rows, translates the LLM record's
                         Chinese keys into the ASCII field names the import
                         script and sue_formulas use, welds on the provenance
                         stamp (book/text_id/vol/quote/gate/batch_id), dedups
                         by (text_id, name, herb-set) -- the same triple as
                         the D1 unique index -- and writes
                         formula_mine_final.jsonl + formula_mine_stats.json
"""
import os
import sys
import re
import json
import time
import base64
import random
import glob
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RUN_ID = os.environ.get("GITHUB_RUN_ID", "local")
T0 = time.time()

HAN = '\u4e00-\u9fff'
TAILS = '\u6c64\u6e6f\u4e38\u5713\u6563\u818f\u996e\u98f2\u4e39\u714e\u997c\u9905\u9152\u9732\u952d\u9320'


def norm(s):
    """\u89c4\u8303\u5316=\u53ea\u4fdd\u7559\u6c49\u5b57\u3002\u4e09\u95f8\u5728\u6b64\u7a7a\u95f4\u505a\u4e25\u683c substring(\u62b9\u5e73\u65ad\u884c/\u7a7a\u767d/\u6807\u70b9,\u5b57\u5e8f\u7528\u5b57\u96f6\u5bb9\u5fcd)\u3002"""
    return re.sub(rf"[^{HAN}]", "", s or "")


# ---------------------------------------------------------------------------
# --summarize mode: pure local merge, no network/D1/R2 needed
# ---------------------------------------------------------------------------
if len(sys.argv) >= 2 and sys.argv[1] == "--summarize":
    src_dir = sys.argv[2] if len(sys.argv) >= 3 else "."
    files = sorted(glob.glob(os.path.join(src_dir, "*.jsonl")))
    print(f"[summarize] run {RUN_ID}: merging {len(files)} shard file(s) from {src_dir}", flush=True)
    rows = []
    for fp in files:
        n = 0
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                    n += 1
                except Exception:
                    pass
        print(f"  + {fp}: {n} rows", flush=True)

    per_book_status = {}
    status_total = {}
    for r in rows:
        st = per_book_status.setdefault(r.get("book", "?"), {})
        st[r.get("status", "?")] = st.get(r.get("status", "?"), 0) + 1
        status_total[r.get("status", "?")] = status_total.get(r.get("status", "?"), 0) + 1

    # The LLM record ("rec") carries the pilot's *Chinese* JSON keys
    # (name / composition / indication / source-quote). The import script and
    # the sue_formulas table use ASCII field names, so the merge is where the
    # translation happens -- and where each row gets its provenance stamp
    # (book / text_id / vol / quote / gate / batch_id) welded on. Getting this
    # mapping wrong is not cosmetic: reading rec["name"] on a Chinese-keyed
    # record yields "" for every row, which collapses the whole dedup key and
    # silently reduces a whole run to a single entry. (That is exactly how
    # run 30153667173's summarize job died -- KeyError: 'book'.)
    K_NAME = "\u65b9\u540d"           # formula name
    K_COMP = "\u7ec4\u6210"           # composition (herb list)
    K_INDI = "\u4e3b\u6cbb"           # indication
    K_QUOTE = "\u539f\u6587\u5f15\u6587"  # verbatim source quote
    GATE_PASS = "\u4e09\u95f8\u5168\u8fc7"   # "all three gates passed"

    final, seen = [], set()
    dup = 0
    for r in rows:
        if r.get("status") != "ok" or "rec" not in r:
            continue
        rec = r["rec"]
        name = str(rec.get(K_NAME) or "").strip()
        comp = [str(x).strip() for x in (rec.get(K_COMP) or []) if str(x).strip()]
        quote = str(rec.get(K_QUOTE) or "").strip()
        if not (name and comp and quote):
            continue
        # dedup key mirrors the D1 unique index (text_id, name, composition)
        key = (r.get("text_id", ""), norm(name), frozenset(norm(c) for c in comp))
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        final.append({
            "name": name,
            "aliases": r.get("aliases_hint") or [],
            "book": r.get("book", ""),
            "text_id": r.get("text_id", ""),
            "vol": r.get("vol"),
            "composition": comp,
            "indication": str(rec.get(K_INDI) or "").strip(),
            "quote": quote,
            "gate": GATE_PASS,
            "src": r.get("src", ""),
            "batch_id": f"fm{RUN_ID}",
        })

    out_path = "formula_mine_final.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in final:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    per_book_final = {}
    for rec in final:
        per_book_final[rec["book"]] = per_book_final.get(rec["book"], 0) + 1

    attempted = len(rows)
    ok_rows = status_total.get("ok", 0)
    gate_rejects = sum(status_total.get(k, 0) for k in
                       ("gate_name", "gate_quote", "gate_herb", "empty_composition"))
    print(f"\n=== merge summary (run {RUN_ID}) ===", flush=True)
    print(f"raw attempt rows (= LLM windows processed): {attempted}", flush=True)
    print(f"status breakdown: {json.dumps(status_total, ensure_ascii=False)}", flush=True)
    if attempted:
        print(f"gate pass rate: {ok_rows}/{attempted} = {100.0*ok_rows/attempted:.1f}%  "
              f"(gate rejects {gate_rejects} = {100.0*gate_rejects/attempted:.1f}%)", flush=True)
    print(f"final ok+deduped: {len(final)} (dup dropped={dup})", flush=True)
    print("per-book final count:", flush=True)
    for b, c in sorted(per_book_final.items(), key=lambda x: -x[1]):
        print(f"  {b}: {c}", flush=True)
    stats = {"raw_rows": attempted, "final_count": len(final), "dup_dropped": dup,
             "status_total": status_total, "ok_rows": ok_rows, "gate_rejects": gate_rejects,
             "gate_pass_rate": round(ok_rows / attempted, 4) if attempted else 0.0,
             "batch_id": f"fm{RUN_ID}",
             "per_book_status": per_book_status, "per_book_final": per_book_final}
    with open("formula_mine_stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"-> {out_path}", flush=True)
    sys.exit(0)

# ---------------------------------------------------------------------------
# worker mode
# ---------------------------------------------------------------------------
import requests
import boto3
from botocore.config import Config

CF_ACC = os.environ["CF_ACCOUNT_ID"]
D1_DB = os.environ["D1_DATABASE_ID"]
D1_TOK = os.environ["D1_API_TOKEN"]
D1_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"

S_EP = os.environ["S_EP"]
S_AK = os.environ["S_AK"]
S_SK = os.environ["S_SK"]
# NOTE: the S_BUCKET secret points at the OCR-output bucket (same one
# ocr.py/ocr_ndl.py write _ocr/{book_id}/page_NNNN.txt into) -- it is NOT
# where books_text_volumes.r2_key lives. align_full.py in this repo already
# established the correct fixed bucket for that (its BUCKET_TEXT constant,
# with the comment "not a secret, just a bucket name"); confirmed by a smoke
# -test run that came back with 0 candidates against every real volume when
# this pointed at S_BUCKET. Match align_full.py's precedent exactly.
TEXT_BUCKET = "guyaofang-assets"

# LLM chain secrets -- all optional at import time, checked lazily per
# provider so a missing/unset provider just gets skipped instead of
# crashing the shard.
AGNES_BASE = os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_KEY = os.environ.get("AGNES_API_KEY", "")
AGNES_MODEL = os.environ.get("AGNES_TEXT_MODEL", "")
XF_BASE = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
XF_MODEL = "xopqwen36v35b"
XF_KEYS = [k.strip() for k in os.environ.get("XF_KEYS", "").replace("\n", ",").split(",") if k.strip()]
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_MODEL = os.environ.get("ZHIPU_MODEL", "glm-4-flash")

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "1"))
OFFSET = int(os.environ.get("OFFSET", "0"))
LIMIT = int(os.environ.get("LIMIT", "50"))
# d1     = books_text / books_text_volumes (compliance-gated corpus)
# r2text = flat one-txt-per-book mirror, keys read from a committed worklist
SOURCE = os.environ.get("SOURCE", "d1").strip().lower()
if SOURCE not in ("d1", "r2text"):
    raise SystemExit(f"SOURCE must be d1 or r2text, got {SOURCE!r}")
WORKLIST_PATH = os.environ.get("WORKLIST_PATH") or (
    "formula_worklist_r2text.json" if SOURCE == "r2text" else "formula_worklist.json")
CONC = int(os.environ.get("CONC", "10"))
LLM_RETRY = int(os.environ.get("LLM_RETRY", "2"))
# Smoke-test throttle ONLY: cap how many volume units this shard takes, so a
# validation dispatch can touch "the first N volumes of \u666e\u6d4e\u65b9" without
# committing to its full 27620 windows. 0 = unlimited, and unlimited is the
# only setting a production dispatch may use. This is a *volume* bound for
# validation, never a per-book window ceiling -- window ceilings are exactly
# what the 2026-07-25 ruling forbids (a 600-window cap is why the pilot
# reported 328 formulas for a book that historically records thousands).
MAX_UNITS = int(os.environ.get("MAX_UNITS", "0"))

OUT_PATH = f"formula_mine_result_shard{SHARD}.jsonl"

S = requests.Session()
_write_lock = threading.Lock()
_counter = {"done": 0, "calls": 0}


def q(sql, params=None):
    body = {"sql": sql}
    if params is not None:
        body["params"] = params
    for att in range(3):
        try:
            r = S.post(D1_URL, json=body, headers={"Authorization": f"Bearer {D1_TOK}"}, timeout=60)
            j = r.json()
            if j.get("success"):
                return j["result"][0]["results"]
            raise RuntimeError(json.dumps(j.get("errors"))[:200])
        except Exception:
            if att == 2:
                raise
            time.sleep(2)


# No proxy config on purpose: GitHub Actions runners reach Cloudflare directly
# (the local proxy workaround is only needed on the developer's own machine).
s3 = boto3.client(
    "s3", endpoint_url=S_EP, aws_access_key_id=S_AK, aws_secret_access_key=S_SK,
    region_name="auto",
    config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 2}, max_pool_connections=16),
)


def get_text(key):
    for att in range(2):
        try:
            return s3.get_object(Bucket=TEXT_BUCKET, Key=key)["Body"].read().decode("utf-8", errors="replace")
        except Exception:
            if att == 1:
                return ""
            time.sleep(1)


# ---------------------------------------------------------------------------
# stage-1 regex candidate windows -- machine-extracted .pattern strings,
# machine-escaped to \uXXXX (do not re-tune here; see docs/new pilot report
# for the rationale behind each pattern)
# ---------------------------------------------------------------------------
NAME_RE = re.compile('([\u4e00-\u9fff]{1,8}[\u6c64\u6e6f\u4e38\u5713\u6563\u818f\u996e\u98f2\u4e39\u714e\u997c\u9905\u9152\u9732\u952d\u9320])')
METHOD_RE = re.compile('\u53f3\u4e3a\u672b|\u53f3\u70ba\u672b|\u4e0a\u4e3a\u672b|\u4e0a\u70ba\u672b|\u4e3a\u7ec6\u672b|\u70ba\u7d30\u672b|\u53f3\u356e\u5480|\u4e0a\u356e\u5480|\u356e\u5480|\u6c34\u714e\u670d|\u6bcf\u670d|\u8c03\u4e0b|\u8abf\u4e0b|\u5316\u4e0b|\u9001\u4e0b|\u714e\u670d|\u6e29\u670d|\u6eab\u670d|\u7c73\u98f2|\u7c73\u996e|\u767d\u6c64\u4e0b|\u767d\u6e6f\u4e0b|\u4e3a\u672b|\u70ba\u672b|\u7814\u5300|\u7814\u52fb|\u70bc\u871c|\u7149\u871c')
COMP_HINT_RE = re.compile('\u5404\u4e00|\u5404\u4e8c|\u5404\u534a|\u5404\u7b49\u5206|\uff08\u7099|\uff08\u7814|\uff08\u7092|\uff08\u53bb|\\(\u7099|\\(\u7814|\\(\u7092|\\(\u53bb')
T_JU_RE = re.compile('^====([^=\\n]{2,40})====\\s*$', re.M)
T_DX_RE = re.compile('^\\s*\\\\x([^\\\\\\n]{2,30})\\\\x\\s*$', re.M)
T_JIN_RE = re.compile('^<\u7bc7\u540d>([^\\n]{2,30})\\s*$', re.M)
STRUCT_HEAD_RE = re.compile('^(?:====[^=\\n]{2,40}====|\\s*\\\\x[^\\\\\\n]{2,30}\\\\x|<\u7bc7\u540d>[^\\n]{2,30}|=====[^=\\n]+=====)\\s*$', re.M)
ALIAS_RE = re.compile('(?:\u4e00\u540d|\u53c8\u540d|\u4ea6\u540d|\u6216\u540d|\u4e00\u65b9\u540d|\u540d)([\u4e00-\u9fff]{2,12})')

WIN_BEFORE = 50
WIN_AFTER = 600
# Same-name window cap *within one shard*. This is the only remaining
# throttle and it exists purely so a book that repeats one famous formula
# hundreds of times (the pilot's example: \u6842\u679d\u6c64 in \u533b\u5b97\u91d1\u9274) cannot eat the
# whole LLM budget. There is deliberately NO per-book window ceiling --
# the founder's 2026-07-25 ruling is that an artificial window cap is what
# made the pilot report only 328 entries for a 90-volume book whose real
# formula count runs into the thousands. Default raised 2 -> 3 to match the
# pilot script's current PER_NAME_CAP, since one name legitimately recurs
# with different compositions/provenance in the big compendia.
PER_NAME_CAP = int(os.environ.get("PER_NAME_CAP", "3"))


def clean_title_name(raw):
    """\u7ed3\u6784\u5316\u6807\u9898 \u2192 (\u4e3b\u540d, aliases)\u3002\u62ec\u53f7\u6ce8\u91cc\u89e3\u6790\u522b\u540d;\u4e3b\u540d\u8981\u6c42\u4ee5\u5242\u578b\u5c3e\u5b57\u7ed3\u5c3e\u3002"""
    raw = raw.strip()
    m = re.match(rf"([{HAN}\s]+?)[\uff08(]", raw)
    aliases = []
    if m:
        main = m.group(1)
        par = raw[m.end() - 1:]
        aliases = [a for a in ALIAS_RE.findall(par) if norm(a)]
    else:
        main = raw
    main = norm(main)
    # \u91d1\u9274\u300a\u4f24\u5bd2\u300b\u300a\u91d1\u532e\u300b\u683c\u5f0f:<\u7bc7\u540d>\u534a\u590f\u6cfb\u5fc3\u6c64\u65b9 \u2014\u2014 \u53bb\u5c3e\u5b57\u300c\u65b9\u300d\u540e\u82e5\u4ee5\u5242\u578b\u5b57\u7ed3\u5c3e,\u53d6\u5176\u4e3a\u65b9\u540d
    if len(main) >= 3 and main.endswith("\u65b9") and main[-2] in TAILS:
        main = main[:-1]
    if not (2 <= len(main) <= 12) or main[-1] not in TAILS:
        return None, []
    # \u522b\u540d\u53bb\u6389\u4e0e\u4e3b\u540d\u91cd\u590d\u7684
    aliases = [a for a in dict.fromkeys(aliases) if a != main]
    return main, aliases


def cut_window(text, start, struct_spans):
    """\u7a97\u53e3=\u65b9\u540d\u8d77\u70b9\u524d50~\u540e600;\u82e5\u4e2d\u9014\u78b0\u5230\u4e0b\u4e00\u4e2a\u7ed3\u6784\u5316\u6807\u9898\u5219\u622a\u5230\u6807\u9898\u524d(\u9632\u8de8\u6761\u76ee)\u3002"""
    lo = max(0, start - WIN_BEFORE)
    hi = min(len(text), start + WIN_AFTER)
    for s0, _ in struct_spans:
        if start < s0 < hi:
            hi = s0
            break
    return text[lo:hi]


def stage1_units(units, progress_every=40):
    """Candidate windows for a list of *volume* work-units, no truncation.

    Sharding happens one level below the book (see build_units): a work-unit
    is one volume, not one book. Sharding by book cannot work at production
    scale -- \u666e\u6d4e\u65b9 alone is 375 volumes / 27620 candidate windows, which at
    the measured ~25 windows/min would need ~18h in a single shard and blow
    straight through the 6h job ceiling, while the other 7 shards sit idle.
    Volume granularity spreads even the largest compendium across all shards
    and keeps each volume fetched from R2 exactly once.

    Body is a verbatim port of pilot_extract.py stage1_book(); only the loop
    header (volume unit instead of book+volumes) and the fetch call differ."""
    cands = []
    n_struct = 0
    n_generic = 0
    for ui, u in enumerate(units):
        text_id, book, vol_no = u["text_id"], u["book"], u["vol_no"]
        if progress_every and ui and ui % progress_every == 0:
            print(f"  [{round(time.time()-T0)}s] stage1 {ui}/{len(units)} unit(s), "
                  f"{len(cands)} window(s) so far", flush=True)
        text = get_text(u["r2_key"])
        if not text:
            continue
        struct_spans = [(m.start(), m.end()) for m in STRUCT_HEAD_RE.finditer(text)]
        seen_pos = set()

        # \u8defA:\u7ed3\u6784\u5316\u6807\u9898(\u5c40\u65b9====/\u5f97\u6548\u65b9\x/\u91d1\u9274<\u7bc7\u540d>,\u6807\u9898\u5373"\u6bb5\u843d\u5f00\u5934"\u6700\u5f3a\u5f62\u6001)
        for rx in (T_JU_RE, T_DX_RE, T_JIN_RE):
            for m in rx.finditer(text):
                name, aliases = clean_title_name(m.group(1))
                if not name:
                    continue
                win = cut_window(text, m.end(), [(s, e) for s, e in struct_spans if s > m.start()])
                cands.append({"wid": f"{text_id}:{vol_no}:{m.start()}", "text_id": text_id,
                              "book": book, "vol": vol_no, "name_hint": name,
                              "aliases_hint": aliases, "src": "struct", "window": win})
                seen_pos.add(m.start() // 40)
                n_struct += 1

        # \u8defB:\u901a\u7528\u6b63\u5219(\u4efb\u52a1\u4e66):\u65b9\u540d\u5019\u9009\u5728\u6bb5\u843d\u5f00\u5934 \u6216 \u5236\u6cd5/\u670d\u6cd5\u8bcd\u90bb\u8fd1
        for m in NAME_RE.finditer(text):
            pos = m.start()
            if pos // 40 in seen_pos:      # \u4e0e\u7ed3\u6784\u5316\u5019\u9009\u540c\u533a\u57df,\u8df3\u8fc7
                continue
            line_start = text.rfind("\n", 0, pos) + 1
            line_end = text.find("\n", pos)
            line = text[line_start: line_end if line_end > 0 else len(text)]
            at_para_head = (pos - line_start) <= 2 and len(norm(line)) <= 20
            after = text[m.end(): m.end() + 400]
            near_method = bool(METHOD_RE.search(after)) and bool(COMP_HINT_RE.search(after) or METHOD_RE.search(after[:200]))
            if not (at_para_head or near_method):
                continue
            name = norm(m.group(1))
            if len(name) < 2:
                continue
            win = cut_window(text, pos, struct_spans)
            cands.append({"wid": f"{text_id}:{vol_no}:{pos}", "text_id": text_id,
                          "book": book, "vol": vol_no, "name_hint": name,
                          "aliases_hint": [], "src": "generic", "window": win})
            seen_pos.add(pos // 40)
            n_generic += 1
    # \u540c\u4e66\u540c\u65b9\u540d cap(\u7ed3\u6784\u5316\u4f18\u5148\u4fdd\u7559,\u518d\u6309\u6587\u4e2d\u987a\u5e8f)
    # NB: the cap key is (text_id, name) -- the pilot ran one book per call so a
    # bare name sufficed; this function now spans books, and a bare name here
    # would cap \u6842\u679d\u6c64 to 3 windows *across the entire corpus* and throw away
    # every other book's version of the same famous formula. Sort is stable, so
    # struct-titled candidates win the cap slots and in-text order is preserved.
    cands.sort(key=lambda c: (0 if c["src"] == "struct" else 1,))
    kept, cnt = [], {}
    for c in cands:
        k = (c["text_id"], c["name_hint"])
        if cnt.get(k, 0) >= PER_NAME_CAP:
            continue
        cnt[k] = cnt.get(k, 0) + 1
        kept.append(c)
    return kept, {"struct": n_struct, "generic": n_generic,
                  "raw": n_struct + n_generic, "after_cap": len(kept)}

# ---------------------------------------------------------------------------
# stage-2: 3-provider LLM chain + verbatim pilot prompt/gates
# ---------------------------------------------------------------------------
# Prompt carried as base64 (matches this repo's existing tcm_judge.py
# convention for LLM-facing extraction prompts); decoded value is byte-exact
# to pilot_extract.py's SYS_PROMPT (round-trip asserted at generation time).
_PROMPT_B64 = '5L2g5piv5Lit5Yy75Y+k57GN5pa55YmC5p2h55uu5oq95Y+W5Zmo44CC6L6T5YWlPeS4gOauteaWueS5puWOn+aWh+eql+WPoyvkuIDkuKrmlrnlkI3lgJnpgInjgILku7vliqE65LuO56qX5Y+j5oq95Ye66K+l5pa555qE57uT5p6E5YyW5p2h55uu44CCCuWIpOWumuinhOWImTrnqpflj6PkuK3lh6Hlh7rnjrDoja/nianlkI3np7Dluo/liJfigJTigJTpgJrluLjkvLTpmo/liYLph48o5aaC44CM5LiJ5Lik44CN44CM5ZCE5LiA5YWp44CN44CM5Y2K5Y2H44CNKeaIlueCruWItuazqCjlpoLjgIzvvIjngpnvvInjgI3jgIzvvIjljrvomIbvvInjgI3jgIzvvIjnoJTvvInjgI0p4oCU4oCU5Y2z6KeG5Li65pyJ57uE5oiQLOW/hemhu+aKveWPlizkuI3orrjlgbfmh5LjgILlj6rmnInnqpflj6Pnoa7lrp7lrozlhajmsqHmnInor6XmlrnnmoToja/niannu4TmiJAo57qv55uu5b2V44CB57qv5q2M6K+A5o+Q5Y+K44CB44CM5pa56KeB5p+Q5p+Q44CNKeaXtizmiY3lhYHorrggc2tpcOOAggrovpPlh7oo5LqM6YCJ5LiALOWPqui+k+WHuuS4gOS4qiBKU09OIOWvueixoSzml6Dku7vkvZXlhbbku5bmloflrZcpOgpBLiB7IuaWueWQjSI6IuKApiIsIue7hOaIkCI6WyLoja/lkI0xIiwi6I2v5ZCNMiIs4oCmXSwi5Li75rK7Ijoi4oCmIiwi5Y6f5paH5byV5paHIjoi4oCmIn0KQi4geyJza2lwIjp0cnVlLCJ3aHkiOiLljYHlrZflhoXljp/lm6AifQrpk4Hlvoso6L+d5Y+N5Lu75L2V5LiA5p2h5Y2z5L2c5bqfKToKMS4g5omA5pyJ5YaF5a656YCQ5a2X5pGY6Ieq56qX5Y+j5Y6f5paHOuaWueWQjeOAgeavj+S4quiNr+WQjeOAgeW8leaWh+S4juWOn+aWh+eUqOWtl+WujOWFqOS4gOiHtOKAlOKAlOe5geS9k+eFp+aKhOe5geS9kyznroDkvZPnhafmioTnroDkvZMs57ud5LiN5YGa566A57mB6L2s5o2i44CB5LiN5pS55a2X44CB5LiN6KGl5a2X44CCCjIuIOe7hOaIkDrlj6rlhpnoja/lkI3mnKzkvZMs5Y675o6J5YmC6YeP5LiO54Ku5Yi25rOoKOOAjOeUmOiNie+8iOeCme+8jOWNiuWFqe+8ieOAjeWPquWGmeOAjOeUmOiNieOAjSk75oyJ5Y6f5paH5Ye6546w6aG65bqPO+e7neS4jea3u+WKoOeql+WPo+S4reayoeacieeahOiNr+OAggozLiDljp/mloflvJXmloc65LuO56qX5Y+j6YCQ5a2X6L+e57ut5pGY5b2V5pyA6IO95Luj6KGo6K+l5pa55Li75rK755qE5LiA5Y+lLDIwLTgw5a2XLOS4gOWtl+S4jeaUuSjlj6/ot6jljp/mlofmjaLooYws54Wn5oqE5paH5a2X5Y2z5Y+vKeOAggo0LiDkuLvmsrs65qaC5ous6K+l5pa55omA5rK755eF6K+BLOWwvemHj+ayv+eUqOWOn+aWh+ivjeaxhyw1MOWtl+WGheOAggo1LiDnqpflj6Poi6XlkKvlpJrkuKrmlrks5Y+q5oq95pa55ZCN5YCZ6YCJ5a+55bqU55qE6YKj5LiA5Liq44CC'
SYS_PROMPT = base64.b64decode(_PROMPT_B64).decode("utf-8")

DOSE_RE = re.compile('[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u534a]+(?:\u4e24|\u5169|\u94b1|\u9322|\u5347|\u5408|\u679a|\u7247|\u65a4)|\u5404\u7b49\u5206|\u5404\u4e00|\u5404\u4e8c|\u5404\u534a')


def gates(rec, window):
    """\u4e09\u95f8:\u65b9\u540d/\u5f15\u6587/\u6bcf\u5473\u836f \u2014\u2014 norm \u7a7a\u95f4\u4e25\u683c substring\u3002\u8fd4\u56de (\u8fc7?, \u5931\u8d25\u95f8\u540d, \u8be6\u60c5)\u3002"""
    W = norm(window)
    name = rec.get("\u65b9\u540d") or ""
    if not norm(name) or norm(name) not in W:
        return False, "gate_name", name
    quote = rec.get("\u539f\u6587\u5f15\u6587") or ""
    if len(norm(quote)) < 10 or norm(quote) not in W:
        return False, "gate_quote", quote[:60]
    comp = rec.get("\u7ec4\u6210") or []
    if not isinstance(comp, list) or len(comp) == 0:
        return False, "empty_composition", ""
    for h in comp:
        h2 = norm(str(h))
        if not h2 or h2 not in W:
            return False, "gate_herb", str(h)
    return True, "", ""


def parse_llm_json(s):
    """\u89e3\u6790 LLM \u8f93\u51fa:\u5265 fence;\u82e5\u542b\u591a\u4e2a\u5e76\u5217 JSON \u5bf9\u8c61(\u5982 {\"skip\":false}\\n{...}),
    raw_decode \u9010\u4e2a\u6536\u96c6,\u4f18\u5148\u53d6\u542b\u300c\u65b9\u540d\u300d\u952e\u7684\u3002\u89e3\u6790\u4e0d\u51fa\u629b ValueError\u3002"""
    t = (s or "").strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    i = t.find("{")
    if i < 0:
        raise ValueError("no json object")
    t = t[i:]
    dec = json.JSONDecoder()
    objs, pos = [], 0
    while pos < len(t):
        j = t.find("{", pos)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(t, j)
            if isinstance(obj, dict):
                objs.append(obj)
            pos = end
        except Exception:
            pos = j + 1
    if not objs:
        raise ValueError("json parse failed: " + t[:60])
    for o in reversed(objs):
        if o.get("\u65b9\u540d"):
            return normalize_keys(o)
    return normalize_keys(objs[0])


# The prompt is written in simplified Chinese, but the source texts are often
# traditional and the models mirror the text they are reading: the smoke run
# produced records keyed "\u7d44\u6210" (traditional) instead of "\u7ec4\u6210",
# which the composition check read as a missing field and threw away as
# json_fail. The other three keys are identical in both scripts, so this is
# the only alias that has ever been observed -- fold it before any field
# lookup. Values are untouched; only the key spelling is normalized.
_KEY_ALIASES = {"\u7d44\u6210": "\u7ec4\u6210"}


def normalize_keys(obj):
    if not isinstance(obj, dict):
        return obj
    for alias, canon in _KEY_ALIASES.items():
        if alias in obj and canon not in obj:
            obj[canon] = obj[alias]
    return obj


def _call_agnes(system, user, max_tokens, timeout):
    if not (AGNES_KEY and AGNES_MODEL):
        raise RuntimeError("agnes not configured")
    body = {"model": AGNES_MODEL, "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(f"{AGNES_BASE}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {AGNES_KEY}", "Content-Type": "application/json"},
                      timeout=timeout)
    if r.status_code in (429, 412):
        raise _Backoff(r.status_code, "agnes")
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


_xf_ki = [0]


def _call_xf(system, user, max_tokens, timeout):
    if not XF_KEYS:
        raise RuntimeError("xf not configured")
    _xf_ki[0] += 1
    key = XF_KEYS[_xf_ki[0] % len(XF_KEYS)]
    body = {"model": XF_MODEL, "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(f"{XF_BASE}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      timeout=timeout)
    if r.status_code in (429, 412):
        raise _Backoff(r.status_code, "xf")
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


def _call_zhipu(system, user, max_tokens, timeout):
    if not ZHIPU_KEY:
        raise RuntimeError("zhipu not configured")
    body = {"model": ZHIPU_MODEL, "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = requests.post(f"{ZHIPU_BASE}/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {ZHIPU_KEY}", "Content-Type": "application/json"},
                      timeout=timeout)
    if r.status_code in (429, 412):
        raise _Backoff(r.status_code, "zhipu")
    r.raise_for_status()
    j = r.json()
    msg = j["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning") or ""


class _Backoff(Exception):
    def __init__(self, code, provider):
        self.code = code
        self.provider = provider
        super().__init__(f"{provider} HTTP{code}")


CHAIN = [("agnes", _call_agnes), ("xf", _call_xf), ("zhipu", _call_zhipu)]


# 900 was not enough headroom: the smoke run's llm_fail rows were all
# well-formed JSON cut off mid-composition on formulas with long herb lists,
# and the big compendia this pipeline now targets are exactly where long
# formulas cluster. The budget is per-response so raising it costs nothing on
# the (majority) short answers.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1400"))


def llm_call(system, user, max_tokens=MAX_TOKENS, timeout=120):
    """Try agnes -> xf -> zhipu in order; 429/412 backs off and falls through
    to the next provider (xf also rotates its key pool across attempts)."""
    last_err = "no provider"
    for name, fn in CHAIN:
        for attempt in range(2):
            try:
                txt = fn(system, user, max_tokens, timeout)
                if txt and txt.strip():
                    return txt
                last_err = f"{name}: empty"
            except _Backoff as e:
                last_err = f"{name}: HTTP{e.code}"
                time.sleep(8 + random.random() * 4)
            except Exception as e:
                last_err = f"{name}: {type(e).__name__}:{str(e)[:60]}"
                time.sleep(1)
    raise RuntimeError(f"llm chain exhausted: {last_err}")


def process_window(cand, out_f, total):
    user = f"\u3010\u65b9\u540d\u5019\u9009\u3011{cand['name_hint']}\n\u3010\u7a97\u53e3\u539f\u6587\u3011\n{cand['window']}"
    rec = None
    status = "llm_fail"
    detail = ""
    dose_hits = len(DOSE_RE.findall(cand["window"]))
    for attempt in range(1 + LLM_RETRY):          # \u521d\u6b21+\u91cd\u8bd52\u6b21
        try:
            with _write_lock:
                _counter["calls"] += 1
            txt = llm_call(SYS_PROMPT, user, max_tokens=MAX_TOKENS, timeout=120)
            obj = parse_llm_json(txt)
            if obj.get("skip") and not obj.get("\u65b9\u540d"):
                if dose_hits >= 2 and attempt < LLM_RETRY:
                    status, detail = "llm_skip", f"\u53ef\u7591skip(\u5242\u91cf\u7279\u5f81x{dose_hits}):{str(obj.get('why') or '')[:30]}"
                    continue                       # \u7a97\u53e3\u660e\u663e\u6709\u7ec4\u6210\u7279\u5f81\u5374 skip \u2192 \u6362\u4e0b\u5bb6\u518d\u8bd5
                status, detail = "llm_skip", str(obj.get("why") or "\u7a97\u53e3\u65e0\u5b8c\u6574\u7ec4\u6210")[:60]
                break
            if not obj.get("\u65b9\u540d") or "\u7ec4\u6210" not in obj:
                status, detail = "json_fail", str(obj)[:80]
                continue
            ok, gate, gd = gates(obj, cand["window"])
            if ok:
                rec, status, detail = obj, "ok", ""
            else:
                status, detail = gate, gd
            break                                  # \u95f8\u62d2\u4e0d\u91cd\u8bd5(\u91cd\u8bd5\u540c\u6837\u7a97\u53e3\u5927\u6982\u7387\u540c\u62d2)
        except Exception as e:
            status, detail = "llm_fail", f"{type(e).__name__}:{str(e)[:80]}"
            time.sleep(2 * (attempt + 1))
    row = {"wid": cand["wid"], "book": cand["book"], "text_id": cand["text_id"], "vol": cand["vol"],
           "name_hint": cand["name_hint"], "aliases_hint": cand["aliases_hint"], "src": cand["src"],
           "status": status, "detail": detail,
           "win_head": cand["window"][:60]}
    if rec:
        row["rec"] = rec
    with _write_lock:
        out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        out_f.flush()
        _counter["done"] += 1
        if _counter["done"] % 25 == 0:
            print(f"[llm] {_counter['done']}/{total} done, pool_calls={_counter['calls']}", flush=True)


def build_units(segment):
    """Expand a worklist segment into a deterministic flat list of volume
    work-units [{text_id, book, vol_no, r2_key}, ...].

    source=d1     -- segment holds text_ids; volumes come from
                     books_text_volumes (which carries the r2_key). This is
                     the compliance-gated corpus: the worklist is built from
                     books_text.allow_sueai_extract=1 only.
    source=r2text -- segment holds pre-resolved {book, key} objects from a
                     committed worklist file (the flat one-txt-per-book
                     mirror under text/\u4e2d\u533b\u7eaf\u6587\u672c\u603b\u5e93/\u539f\u6587\u7248/). Keys come
                     from the committed file, never from a bucket listing:
                     the zero-LIST rule is CI-enforced by guard_no_list.yml.
    Order is fully determined by (worklist order, vol_no), so shard
    assignment is reproducible across reruns and resumes."""
    units = []
    if SOURCE == "r2text":
        for i, item in enumerate(segment):
            if isinstance(item, str):
                item = {"book": os.path.basename(item).rsplit(".", 1)[0], "key": item}
            units.append({"text_id": item.get("text_id") or f"r2t_{item['key']}",
                          "book": item.get("book", ""), "vol_no": item.get("vol", 1),
                          "r2_key": item["key"]})
        return units
    for text_id in segment:
        rows = q("SELECT clean_name FROM books_text WHERE text_id = ?", [text_id])
        if not rows:
            print(f"  WARN {text_id}: not found in books_text, skip", flush=True)
            continue
        book_name = rows[0]["clean_name"]
        vols = q("SELECT vol_no, r2_key FROM books_text_volumes WHERE text_id = ? ORDER BY vol_no",
                 [text_id])
        if not vols:
            print(f"  WARN {text_id} ({book_name}): no volumes, skip", flush=True)
            continue
        for v in vols:
            units.append({"text_id": text_id, "book": book_name,
                          "vol_no": v["vol_no"], "r2_key": v["r2_key"]})
    return units


def interleave_by_book(units):
    """Round-robin the volume units across books (vol 1 of every book, then
    vol 2 of every book, ...). Same trick the pilot's run_llm() used for its
    3 books, and it buys two things at production scale: every shard gets a
    mix of books instead of shard 0 drowning in \u666e\u6d4e\u65b9's 375 volumes while
    shard 7 gets short texts, and if a dispatch is cut short the coverage is
    even across books rather than complete on the first book and zero on the
    rest. Deterministic: input order fixes output order."""
    by = {}
    for u in units:
        by.setdefault(u["text_id"], []).append(u)
    lists = list(by.values())
    out, i = [], 0
    while True:
        added = False
        for L in lists:
            if i < len(L):
                out.append(L[i])
                added = True
        if not added:
            return out
        i += 1


def main():
    with open(WORKLIST_PATH, encoding="utf-8") as f:
        worklist = json.load(f)
    segment = worklist[OFFSET: OFFSET + LIMIT]
    print(f"[{round(time.time()-T0)}s] source={SOURCE} worklist={len(worklist)} "
          f"segment=[{OFFSET}:{OFFSET+LIMIT}]={len(segment)} item(s)", flush=True)

    units = interleave_by_book(build_units(segment))
    mine = units[SHARD::TOTAL]
    if MAX_UNITS > 0:
        mine = mine[:MAX_UNITS]
        print(f"[{round(time.time()-T0)}s] MAX_UNITS={MAX_UNITS} active "
              f"(SMOKE TEST ONLY -- production dispatches must leave this at 0)", flush=True)
    books_here = sorted({u["text_id"] for u in mine})
    print(f"[{round(time.time()-T0)}s] segment -> {len(units)} volume unit(s); "
          f"shard {SHARD}/{TOTAL} takes {len(mine)} unit(s) across {len(books_here)} book(s)", flush=True)

    done_wids = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    done_wids.add(json.loads(line)["wid"])
                except Exception:
                    pass
        print(f"[{round(time.time()-T0)}s] resume: {len(done_wids)} window(s) already in ledger", flush=True)

    all_cands, st = stage1_units(mine)
    print(f"[{round(time.time()-T0)}s] stage1 done: struct={st['struct']} generic={st['generic']} "
          f"raw={st['raw']} after name-cap({PER_NAME_CAP})={st['after_cap']}", flush=True)

    todo = [c for c in all_cands if c["wid"] not in done_wids]
    print(f"[{round(time.time()-T0)}s] total candidates={len(all_cands)} todo={len(todo)} conc={CONC}", flush=True)

    with open(OUT_PATH, "a", encoding="utf-8") as out_f:
        with ThreadPoolExecutor(max_workers=CONC) as ex:
            futs = [ex.submit(process_window, c, out_f, len(todo)) for c in todo]
            for fut in as_completed(futs):
                fut.result()

    el = max(1e-9, time.time() - T0)
    print(f"[{round(el)}s] shard {SHARD}/{TOTAL} DONE, processed {len(todo)} window(s), "
          f"llm_calls={_counter['calls']}, throughput={60.0*len(todo)/el:.1f} windows/min "
          f"-> {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
