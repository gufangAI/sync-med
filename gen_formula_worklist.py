# coding: utf-8
"""
Regenerate formula_worklist.json -- the book list formula_mine.py mines.

Why this script exists
----------------------
The first worklist was not "the corpus", it was the 85 books that passed a
*category* filter in the local Phase0 census (census_mining_order.py's
`ranked_fangshu`: category == formula-book, or the title contains the
character for "formula"/"preparation"). Measured against that same census,
the 85 books carry 88302 of the pool's 258885 candidate windows -- the
filter dropped 420 books and 170583 windows, 66% of the corpus, and among
the dropped books are the biggest compendia in the collection (Zhengzhi
Zhunsheng 13561 windows, Shengji Zonglu 9118, Yizong Jinjian 6888, Bencao
Gangmu 5120). A category label is not a reason to leave a book unmined:
clinical and materia-medica works quote formulas verbatim too, and the
three hard gates decide what is real, not the shelf a book sits on.

So the worklist is now generated from D1, not curated: every book in the
compliance-allowed pool goes in, ordered so that a dispatch cut short still
covers the densest material first.

Compliance
----------
FILTER=allow (default) selects books_text.allow_sueai_extract = 1. That
column is the platform's canonical AI-extraction gate (migrations/
010_books_text_rights.sql; guyaofang-web scripts/ancient-text/
classify-rights.mjs sets it; tools/sueai/v3_pipeline.py and the admin
sueai-status endpoint already read it). This script does not widen it and
writes nothing back to D1.

FILTER=rights selects rights_status IN (public_domain, internal_reference)
instead. As of 2026-07-25 that is 570 books rather than 505, because 65
books carry rights_status='public_domain' with a stale
allow_sueai_extract=0 -- inconsistent with classify-rights.mjs's own rule
(public_domain => allow=1) and with each other. Those 65 are all pre-1912
dynasties. Fixing them is a one-line D1 UPDATE on a rights column, which is
a decision for the CTO, not for this script; until it is made, the default
stays on the canonical column.

Ordering
--------
1. Whatever is already at the head of the current formula_worklist.json,
   in its existing order (the census density ranking, and the offset a
   running dispatch's resume ledger is keyed to). Entries no longer in the
   pool are dropped.
2. Everything else by total text bytes descending, text_id ascending as a
   deterministic tie-break. Bytes is a good proxy for candidate count here:
   census density is 40-90 windows per 10k characters across every tier, so
   size dominates.

Read-only: D1 SELECT only, no R2 access at all.

Usage:  python gen_formula_worklist.py [--out PATH] [--print-only]
Env:    CF_ACCOUNT_ID / D1_DATABASE_ID / D1_API_TOKEN, FILTER=allow|rights
"""
import os
import sys
import json
import time

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CF_ACC = os.environ["CF_ACCOUNT_ID"]
D1_DB = os.environ["D1_DATABASE_ID"]
D1_TOK = os.environ["D1_API_TOKEN"]
D1_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"

FILTER = os.environ.get("FILTER", "allow").strip().lower()
if FILTER not in ("allow", "rights"):
    raise SystemExit(f"FILTER must be allow or rights, got {FILTER!r}")

WHERE = {
    "allow": "t.allow_sueai_extract = 1",
    "rights": "t.rights_status IN ('public_domain', 'internal_reference')",
}[FILTER]

OUT_PATH = "formula_worklist.json"
PRINT_ONLY = False
_argv = sys.argv[1:]
while _argv:
    a = _argv.pop(0)
    if a == "--out":
        OUT_PATH = _argv.pop(0)
    elif a == "--print-only":
        PRINT_ONLY = True
    else:
        raise SystemExit(f"unknown arg {a!r}")

S = requests.Session()


def q(sql, params=None):
    body = {"sql": sql}
    if params is not None:
        body["params"] = params
    for att in range(3):
        try:
            r = S.post(D1_URL, json=body, headers={"Authorization": f"Bearer {D1_TOK}"}, timeout=90)
            j = r.json()
            if j.get("success"):
                return j["result"][0]["results"]
            raise RuntimeError(json.dumps(j.get("errors"))[:200])
        except Exception:
            if att == 2:
                raise
            time.sleep(2)


def main():
    # Only books that actually have volume rows can be mined: books_text_volumes
    # is where the r2_key lives, and a book with no volumes has no text to read.
    rows = q(
        "SELECT t.text_id AS text_id, t.clean_name AS name, t.category AS category, "
        "       t.rights_status AS rights, t.allow_sueai_extract AS allow, "
        "       COUNT(v.vol_no) AS vols, SUM(v.bytes) AS bytes "
        "FROM books_text t JOIN books_text_volumes v ON v.text_id = t.text_id "
        f"WHERE {WHERE} "
        "GROUP BY t.text_id "
        "ORDER BY bytes DESC, t.text_id ASC")
    pool = {r["text_id"]: r for r in rows}
    print(f"[gen] filter={FILTER} -> pool {len(pool)} book(s), "
          f"{sum(r['vols'] for r in rows)} volume(s), "
          f"{sum(r['bytes'] or 0 for r in rows)} byte(s)", flush=True)

    head = []
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                prev = json.load(f)
            head = [t for t in prev if isinstance(t, str) and t in pool]
            dropped = [t for t in prev if t not in pool]
            print(f"[gen] previous worklist: {len(prev)} entr(ies); "
                  f"{len(head)} kept as ordered head, {len(dropped)} no longer in pool", flush=True)
            for t in dropped[:10]:
                print(f"        dropped: {t}", flush=True)
        except Exception as e:
            print(f"[gen] previous worklist unreadable ({type(e).__name__}), starting fresh", flush=True)
            head = []

    seen = set(head)
    tail = [r["text_id"] for r in rows if r["text_id"] not in seen]
    worklist = head + tail

    assert len(worklist) == len(set(worklist)), "duplicate text_id in generated worklist"
    assert len(worklist) == len(pool), (
        f"worklist {len(worklist)} != pool {len(pool)} -- every eligible book must be present")

    head_vols = sum(pool[t]["vols"] for t in head)
    tail_vols = sum(pool[t]["vols"] for t in tail)
    print(f"[gen] worklist = {len(head)} head + {len(tail)} appended = {len(worklist)} book(s); "
          f"volumes {head_vols} + {tail_vols} = {head_vols + tail_vols}", flush=True)
    print("[gen] biggest newly added books:", flush=True)
    for t in tail[:15]:
        r = pool[t]
        print("        %-14s %-24s cat=%-10s vols=%-4d bytes=%d"
              % (t, (r["name"] or "")[:22], (r["category"] or "")[:10], r["vols"], r["bytes"] or 0), flush=True)

    if PRINT_ONLY:
        print("[gen] --print-only, nothing written", flush=True)
        return
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(worklist, f, ensure_ascii=False)
        f.write("\n")
    print(f"[gen] -> {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
