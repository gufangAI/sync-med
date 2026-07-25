# -*- coding: utf-8 -*-
"""
RETIRED 2026-07-25, same day this was built -- see warm_thumbs.yml header for why
(superseded by warm_dlink.py's pan:dlink KV pre-warming once the 123 direct-link auth key
landed). Left in place (never delete, only retire) as a working reference for the
R2-thumb-hot-layer approach, in case the direct-link path ever needs a fallback.

Cover-thumb R2 hot-layer warmer -- GitHub Actions matrix, slow-drip version (2026-07-25).

Carries forward the approach already proven by
F:\\0book\\guyaofang-web\\tools\\warm_cover_thumbs.py: this script never talks to 123 or
writes R2 directly. It only makes a low, rate-limited stream of GET requests against the
production GET /api/reader/{book_id}/cover endpoint. Every time that Worker successfully
pulls a thumb.webp from 123, it already backfills R2 guyaofang-assets/thumbs/{book_id}.webp
itself via context.waitUntil() (functions/api/reader/[book_code]/cover.js -- untouched by
this script, that file is production code and out of scope here).

Two-layer skip strategy, zero R2 LIST anywhere:
  1) ledger.json (this shard's actions/cache checkpoint) -- book_ids this shard has already
     confirmed "R2 has it (or will never need it)". Zero network cost, checked first.
  2) R2 head_object probe (ONLY for candidates not yet in the ledger, at most once per
     book_id for the lifetime of the pipeline) -- catches books that got warmed some other
     way (organic production traffic backfilling on a real cache miss, or a previous run
     that finished the warm call but the ledger save got cut off).
  WARNING (see this repo's own ocr.py / clean_embed_ingest.py comments, 2026-07-19 incident):
     head_object'ing the *entire* candidate pool on *every* run was the confirmed root
     cause of an R2 HeadObject cost spike (2.43M calls / 24h against guyaofang-lib). This
     script avoids that pattern structurally: once a book_id enters the ledger it is never
     head_object'd again, so lifetime head_object volume converges to ~candidate-count
     (currently ~34k), not "full pool re-scanned every 3-hour tick forever".

Rate-limit discipline (123 open-platform download_info is 5 QPS, shared by the WHOLE 123
account -- the download-line's sync/OCR pipelines draw from the same quota):
  Only the "cold path" (R2 head missed -> real GET to /cover, which may trigger a live 123
  fetch) sleeps or counts against the budget. Ledger hits and R2-head hits cost zero 123
  calls and are not rate-limited. Every cold-path call is followed by >=1.5s sleep;
  consecutive non-success responses back off exponentially (capped), successes reset it.

Sharding: matrix of <=3 shards, strictly serial *within* each shard (no threads) -- worst
  case aggregate request rate from this whole pipeline stays well under the 5 QPS ceiling,
  leaving headroom for the download line.

Checkpointing: ledger.json persisted per-shard via actions/cache, same restore-keys-prefix
  pattern as ocr.yml / clean-embed.yml in this repo. Next run resumes, never re-warms.

Time budget: each shard self-terminates after SOFT_DEADLINE_MIN minutes even mid-list, so
  the "save ledger cache" step still runs normally afterward -- better than letting the
  workflow's hard timeout-minutes kill the job outright, which would skip the cache-save
  step entirely and lose that run's progress.

Env vars:
  CF_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN  -- D1 HTTP API (read books_assets_v2 only)
  S_EP, S_AK, S_SK                              -- R2 S3-compatible creds (head_object only,
                                                    read-only use in this script; the actual
                                                    thumb PUT happens inside the Worker, not
                                                    here)
  SHARD, TOTAL                                  -- matrix shard index / shard count
  PILOT                                         -- optional cap on "attempted" (head+maybe
                                                    warm) items this shard this run; empty =
                                                    unlimited, gated only by the time budget
"""
import os, sys, json, time, random
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

CF_ACC = os.environ["CF_ACCOUNT_ID"]
D1_DB = os.environ["D1_DATABASE_ID"]
D1_TOK = os.environ["D1_API_TOKEN"]
D1_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"

S_EP = os.environ["S_EP"]; S_AK = os.environ["S_AK"]; S_SK = os.environ["S_SK"]
# 2026-07-25: no new secret added on purpose -- reuses the pattern already proven twice in
# this repo (align_full.py's BUCKET_TEXT, formula_mine.py's TEXT_BUCKET), both hardcoding
# "guyaofang-assets" with the comment "not a secret, just a bucket name" and both confirmed
# working against this exact bucket with the existing S_AK/S_SK secret pair. wrangler.toml's
# STATIC binding (guyaofang-web/wrangler.toml) points at this same bucket_name.
ASSETS_BUCKET = "guyaofang-assets"

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "3"))
PILOT = os.environ.get("PILOT", "").strip()

SLEEP_COLD = 1.5           # floor sleep after every cold-path call (task requirement: >=1.5s)
SLEEP_CAP = 30.0           # exponential-backoff ceiling
SOFT_DEADLINE_MIN = 105    # internal wall-clock budget; workflow timeout-minutes set above this
COVER_BASE = "https://gufangai.com/api/reader"
LEDGER = "ledger.json"


def q(sql):
    # No proxy config on purpose: GitHub Actions runners reach Cloudflare directly (matches
    # align_full.py / formula_mine.py precedent in this repo -- proxy is a local-dev-only
    # workaround for this developer's censored-network machine).
    for att in range(3):
        try:
            r = requests.post(D1_URL, headers={"Authorization": "Bearer " + D1_TOK},
                               json={"sql": sql}, timeout=120)
            j = r.json()
            if j.get("success"):
                return j["result"][0]["results"]
            raise RuntimeError(json.dumps(j.get("errors"))[:300])
        except Exception:
            if att == 2:
                raise
            time.sleep(2)


import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

s3 = boto3.client(
    "s3", endpoint_url=S_EP, aws_access_key_id=S_AK, aws_secret_access_key=S_SK,
    region_name="auto",
    config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2}, max_pool_connections=8),
)

_perm_warn_shown = [0]


def r2_has_thumb(book_id):
    """Zero-LIST, single-key HEAD probe. Returns True / False / None (None = probe itself
       failed for a non-404 reason, e.g. permission -- treated as 'unknown', falls through
       to the cold path rather than silently blocking progress)."""
    try:
        s3.head_object(Bucket=ASSETS_BUCKET, Key=f"thumbs/{book_id}.webp")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        _perm_warn_shown[0] += 1
        if _perm_warn_shown[0] <= 5:
            print(f"  WARN head_object unexpected error {book_id}: {code} {e}", flush=True)
        return None
    except Exception as e:
        _perm_warn_shown[0] += 1
        if _perm_warn_shown[0] <= 5:
            print(f"  WARN head_object exception {book_id}: {e}", flush=True)
        return None


S = requests.Session(); S.trust_env = False


def warm_one(book_id):
    """One real GET against the production /cover endpoint. Returns (outcome, info):
       'real'        -- R2 hot layer already had it, or was just backfilled this call
       'ineligible'  -- got a real page image but NOT via the thumb path (cover.js's
                        wantThumb gate blocked it, e.g. a pinned non-1 cover_page) --
                        not a failure, but this book_id will never get a thumb via this
                        endpoint, so don't keep retrying it
       'transient'   -- placeholder SVG or a request-level error; worth retrying later
    """
    try:
        r = S.get(f"{COVER_BASE}/{book_id}/cover", timeout=30)
    except Exception as e:
        return "transient", f"exc:{str(e)[:60]}"
    kind = r.headers.get("X-Cover-Kind", "")
    src = r.headers.get("X-Source", "")
    if kind == "page" and ("r2thumb" in src or "pan123thumb" in src):
        return "real", src
    if kind == "page":
        return "ineligible", src
    return "transient", f"{kind}:{src}"


def main():
    t_start = time.time()
    deadline = t_start + SOFT_DEADLINE_MIN * 60

    ledger = set()
    if os.path.exists(LEDGER):
        try:
            ledger = set(json.load(open(LEDGER, encoding="utf-8")))
        except Exception:
            ledger = set()
    print(f"[shard {SHARD}/{TOTAL}] ledger already has {len(ledger)} entries", flush=True)

    rows = q("SELECT book_id FROM books_assets_v2 WHERE frontend_visible=1 "
             "AND upload_status='done' AND thumb_done_at IS NOT NULL ORDER BY book_id")
    all_ids = [r["book_id"] for r in rows]
    mine = [b for i, b in enumerate(all_ids) if i % TOTAL == SHARD]
    print(f"[shard {SHARD}/{TOTAL}] candidates total={len(all_ids)}, this shard={len(mine)}", flush=True)

    stat = {"ledger_skip": 0, "head_skip": 0, "warmed": 0, "ineligible": 0, "transient": 0, "attempted": 0}
    backoff = SLEEP_COLD
    pilot_n = int(PILOT) if PILOT else None

    try:
        for book_id in mine:
            if time.time() > deadline:
                print(f"[shard {SHARD}] hit soft deadline ({SOFT_DEADLINE_MIN} min), wrapping up", flush=True)
                break
            if book_id in ledger:
                stat["ledger_skip"] += 1
                continue
            if pilot_n is not None and stat["attempted"] >= pilot_n:
                print(f"[shard {SHARD}] hit PILOT cap ({pilot_n}), wrapping up", flush=True)
                break

            stat["attempted"] += 1
            has = r2_has_thumb(book_id)
            if has is True:
                ledger.add(book_id)
                stat["head_skip"] += 1
                continue

            # cold path (has is False, or None = probe itself failed -- either way let the
            # /cover endpoint's own fallback chain decide, it never trusts our probe blindly)
            outcome, info = warm_one(book_id)
            if outcome == "real":
                ledger.add(book_id)
                stat["warmed"] += 1
                backoff = SLEEP_COLD
            elif outcome == "ineligible":
                ledger.add(book_id)   # permanently unreachable via this endpoint -- stop retrying
                stat["ineligible"] += 1
                backoff = SLEEP_COLD
                print(f"  INELIGIBLE {book_id}: {info} (wantThumb gate blocked it)", flush=True)
            else:
                stat["transient"] += 1
                backoff = min(backoff * 1.6, SLEEP_CAP)
                if stat["transient"] % 20 == 1:
                    print(f"  transient {book_id}: {info} (backing off to {backoff:.1f}s)", flush=True)

            n = stat["attempted"]
            if n % 25 == 0:
                el = time.time() - t_start
                print(f"[shard {SHARD}] progress: attempted={n} ledger_skip={stat['ledger_skip']} "
                      f"head_skip={stat['head_skip']} warmed={stat['warmed']} "
                      f"ineligible={stat['ineligible']} transient={stat['transient']} elapsed={el:.0f}s",
                      flush=True)

            time.sleep(backoff + random.uniform(0, 0.3))  # small jitter so shards don't pulse in lockstep
    finally:
        json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[shard {SHARD}] ledger saved, {len(ledger)} entries", flush=True)

    el = time.time() - t_start
    print(f"=== shard {SHARD}/{TOTAL} done in {el:.0f}s: attempted={stat['attempted']} "
          f"ledger_skip={stat['ledger_skip']} head_skip={stat['head_skip']} warmed={stat['warmed']} "
          f"ineligible={stat['ineligible']} transient={stat['transient']} ===", flush=True)


if __name__ == "__main__":
    main()
