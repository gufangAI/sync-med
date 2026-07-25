# -*- coding: utf-8 -*-
"""
123 direct-link pre-warmer -- populates the pan:fid / pan:dlink KV caches that
functions/api/_lib/pan123.js's fetchViaDirectLink() reads, so real page-load traffic never
has to pay the first-hit 123 API cost. Does NOT download or store any image bytes anywhere
(no R2 write, no local file) -- it only resolves and caches two small strings per book: a
123 fileId and a fixed (unsigned) direct-link URL. Byte-level signing (PAN_DIRECT_KEY /
PAN_UID) happens later, server-side, at actual request time inside the Worker -- this script
never touches those secrets.

Background: 2026-07-25, same afternoon as the R2-thumb-hot-layer effort (warm_thumbs.py,
now retired), the founder supplied a 123 "purchased direct-link space" auth key. Deploy
b0957bb0 added fetchViaDirectLink() to pan123.js: a file's direct-link URL is FIXED (unlike
the old download_info flow's 7-day signed URL), so once resolved it can be cached for a full
year and reused with zero further 123 API calls. Measured cover-fetch success rate: 3% -> 84%.
This script exists to pre-populate that cache in bulk instead of waiting for organic traffic
to warm it one book at a time.

KV keys written (must match pan123.js's kvKey construction EXACTLY, verified empirically
against the live namespace before this script was written -- colons are literal, not
URL-encoded, and the REST API's stored key name was confirmed via the /keys list endpoint
to be byte-identical to what a Workers KV binding .get() call would look up):
  pan:fid:{panDirId}:{fileName}    fileId,      TTL 2592000  (30 days -- matches TTL_FID)
  pan:dlink:{panDirId}:{fileName}  raw URL,     TTL 31536000 (1 year  -- matches TTL_DLINK)
fileName is "thumb.webp" when available, else "page_0001.webp" (mirrors cover.js: wantThumb
books ask fetchViaDirectLink for 'thumb.webp' specifically; there is currently no book in
this candidate set where wantThumb is false, confirmed by direct D1 query on 2026-07-25).

Two-layer skip, zero list_objects/list-style bulk scan anywhere:
  1) ledger.json (this shard's actions/cache checkpoint) -- book_ids this shard has already
     confirmed done. Zero network cost, checked first.
  2) KV GET on pan:dlink:{panDirId}:{fileName} (ONLY for candidates not yet in the ledger,
     at most once per book_id for the lifetime of the pipeline) -- catches books warmed by
     real production traffic already calling fetchViaDirectLink() organically, or a previous
     run that resolved the link but didn't get to save its ledger.

Rate-limit discipline (123 open-platform file/list is 3 QPS, direct-link/url is 5 QPS, BOTH
shared account-wide with the download line's sync/OCR pipelines -- see pan123.js file header):
  KV hits (ledger or live GET) cost zero 123 calls and are not rate-limited. Every actual 123
  API call (access_token / file/list page / direct-link/url) is followed by >=1.2s sleep;
  consecutive rate-limited (HTTP 200 body {code:429,...}, per pan123.js's panJson() comment
  on 123's actual rate-limit response shape) or transient responses back off exponentially,
  capped; a clean success resets the backoff to the floor.

Sharding: matrix of <=3 shards, strictly serial within a shard (no threads).

Env vars:
  CF_ACCOUNT_ID, D1_DATABASE_ID, D1_API_TOKEN  -- D1 HTTP API (read books_assets_v2 only)
                                                    AND CF KV REST API (read+write) -- the
                                                    same D1_API_TOKEN was empirically
                                                    confirmed to have KV read+write scope on
                                                    this account, so no new secret was added
  PAN_CLIENT_ID, PAN_CLIENT_SECRET              -- 123 open-platform OAuth (access_token)
  SHARD, TOTAL                                  -- matrix shard index / shard count
  PILOT                                         -- optional cap on "attempted" (KV-miss ->
                                                    actual 123 resolution) items this shard
                                                    this run; empty = unlimited, gated only
                                                    by the time budget
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

# Not a secret -- resource id from guyaofang-web/wrangler.toml's [[kv_namespaces]] binding
# "KV" (same convention as warm_thumbs.py's hardcoded R2 bucket name: infra ids that are
# fixed and non-sensitive don't need a GitHub secret of their own).
KV_NS = "f7316f9f43744edfaa6d288fbc3c935b"
KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/storage/kv/namespaces/{KV_NS}"
KV_HEADERS = {"Authorization": "Bearer " + D1_TOK}

PAN = "https://open-api.123pan.com"
PAN_CLIENT_ID = os.environ["PAN_CLIENT_ID"]
PAN_CLIENT_SECRET = os.environ["PAN_CLIENT_SECRET"]

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "3"))
PILOT = os.environ.get("PILOT", "").strip()

SLEEP_FLOOR = 1.2          # floor sleep after every real 123 API call (coordinator's spec)
SLEEP_CAP = 30.0
SOFT_DEADLINE_MIN = 105
LEDGER = "ledger.json"

# TTLs -- must match pan123.js's TTL_FID / TTL_DLINK exactly, or the Worker's own next
# resolution would just overwrite with a different TTL anyway; matching keeps behavior
# identical whether a key was warmed by this script or by organic traffic.
TTL_FID = 2592000     # 30 days
TTL_DLINK = 31536000  # 1 year

S = requests.Session(); S.trust_env = False  # no proxy needed -- 123 and CF API both reachable
                                              # directly from GitHub Actions runners (proxy is
                                              # only a workaround for this developer's own
                                              # censored-network machine, confirmed unnecessary
                                              # by a real no-proxy smoke test against both APIs
                                              # before this script was written)


def d1_query(sql):
    for att in range(3):
        try:
            r = S.post(D1_URL, headers={"Authorization": "Bearer " + D1_TOK}, json={"sql": sql}, timeout=120)
            j = r.json()
            if j.get("success"):
                return j["result"][0]["results"]
            raise RuntimeError(json.dumps(j.get("errors"))[:300])
        except Exception:
            if att == 2:
                raise
            time.sleep(2)


def kv_get(key):
    try:
        r = S.get(f"{KV_BASE}/values/{key}", headers=KV_HEADERS, timeout=15)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def kv_put(key, value, ttl):
    try:
        r = S.put(f"{KV_BASE}/values/{key}?expiration_ttl={ttl}", headers=KV_HEADERS,
                  data=value.encode("utf-8"), timeout=15)
        return r.status_code == 200 and r.json().get("success")
    except Exception:
        return False


# --- 123 open-platform JSON API wrapper ------------------------------------------------
# Mirrors pan123.js's panJson(): 123's rate-limit response is HTTP 200 + a body whose
# message is the Chinese text for "operating too frequently, please try again later" --
# checking res.ok alone never catches it. code===0 is the only confirmed-success signal.
_backoff = {"v": SLEEP_FLOOR}
_CN_TOO_FREQUENT = "\u9891\u7e41"  # opsec: escaped, not literal CJK in a public repo file.
                                    # Decodes to the two characters 123's real API message
                                    # contains ("frequent"); must match byte-for-byte or the
                                    # rate-limit detection silently stops working.


def _rate_limited(j):
    if not j:
        return True
    if j.get("code") in (429, 401):
        return True
    msg = str(j.get("message") or "")
    return any(s in msg for s in (_CN_TOO_FREQUENT, "exceeded", "tokens number"))


def pan_json(method, url, headers=None, body=None, attempts=2):
    last = None
    for i in range(attempts):
        try:
            if method == "GET":
                r = S.get(url, headers=headers, timeout=15)
            else:
                r = S.post(url, headers=headers, json=body, timeout=15)
            last = r.json()
        except Exception:
            last = None
        if last and last.get("code") == 0:
            _backoff["v"] = SLEEP_FLOOR  # success -> reset backoff
            time.sleep(SLEEP_FLOOR + random.uniform(0, 0.3))  # jitter: 3 shards, avoid lockstep bursts
            return last
        if _rate_limited(last):
            _backoff["v"] = min(_backoff["v"] * 1.6, SLEEP_CAP)
        else:
            time.sleep(SLEEP_FLOOR)  # a confirmed non-rate-limit error still counts as a real call
            return last              # real error (e.g. bad params) -- don't burn retries on it
        time.sleep(_backoff["v"] + random.uniform(0, 0.3))
        if i == attempts - 1:
            return last
    return last


# -- token: process-local cache -> KV pan:tok -> mint fresh ------------------------------
_tok_cache = {"v": None}


def get_token():
    if _tok_cache["v"]:
        return _tok_cache["v"]
    cached = kv_get("pan:tok")
    if cached:
        _tok_cache["v"] = cached
        return cached
    r = pan_json("POST", PAN + "/api/v1/access_token",
                 headers={"Platform": "open_platform", "Content-Type": "application/json"},
                 body={"clientID": PAN_CLIENT_ID, "clientSecret": PAN_CLIENT_SECRET})
    token = r and r.get("code") == 0 and r["data"]["accessToken"]
    if not token:
        return None
    _tok_cache["v"] = token
    kv_put("pan:tok", token, 1728000)  # 20 days, matches pan123.js's TTL_TOK
    return token


# -- fileId resolution: mirrors pan123.js resolveFileId() exactly (pagination, trashed-copy
#    preference) so the two implementations never disagree about which file "the" match is.
def resolve_file_id(token, pan_dir_id, file_name, deadline):
    kv_key = f"pan:fid:{pan_dir_id}:{file_name}"
    cached = kv_get(kv_key)
    if cached:
        return cached, True  # (fileId, was_cached)

    headers = {"Platform": "open_platform", "Authorization": "Bearer " + token}
    last_file_id, file_id, trashed_fallback = 0, None, None
    for _ in range(20):
        if time.time() > deadline:
            break
        r = pan_json("GET", f"{PAN}/api/v2/file/list?parentFileId={pan_dir_id}&limit=100&lastFileId={last_file_id}",
                     headers=headers)
        if not r or r.get("code") != 0:
            break
        data = r.get("data") or {}
        fl = data.get("fileList") or []
        hit = next((f for f in fl if f.get("filename") == file_name and f.get("trashed") != 1), None)
        if hit:
            file_id = str(hit.get("fileId") or hit.get("fileID"))
            break
        if trashed_fallback is None:
            th = next((f for f in fl if f.get("filename") == file_name), None)
            if th:
                trashed_fallback = str(th.get("fileId") or th.get("fileID"))
        last_file_id = data.get("lastFileId")
        if last_file_id is None or last_file_id == -1 or len(fl) == 0:
            break
    if not file_id:
        file_id = trashed_fallback
    if file_id:
        kv_put(kv_key, file_id, TTL_FID)
    return file_id, False


def resolve_direct_link(token, pan_dir_id, file_name, deadline):
    """Returns (raw_url_or_None, was_cached)."""
    dlink_key = f"pan:dlink:{pan_dir_id}:{file_name}"
    cached = kv_get(dlink_key)
    if cached:
        return cached, True

    file_id, _ = resolve_file_id(token, pan_dir_id, file_name, deadline)
    if not file_id:
        return None, False
    headers = {"Platform": "open_platform", "Authorization": "Bearer " + token}
    r = pan_json("GET", f"{PAN}/api/v1/direct-link/url?fileID={file_id}", headers=headers)
    url = r and r.get("code") == 0 and (r.get("data") or {}).get("url")
    if url:
        kv_put(dlink_key, url, TTL_DLINK)
    return (url or None), False


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

    rows = d1_query("SELECT book_id, pan_dir_id, thumb_done_at FROM books_assets_v2 "
                     "WHERE frontend_visible=1 AND upload_status='done' "
                     "AND pan_dir_id IS NOT NULL AND pan_dir_id != '' ORDER BY book_id")
    all_rows = [(r["book_id"], str(r["pan_dir_id"]), bool(r.get("thumb_done_at"))) for r in rows]
    mine = [x for i, x in enumerate(all_rows) if i % TOTAL == SHARD]
    print(f"[shard {SHARD}/{TOTAL}] candidates total={len(all_rows)}, this shard={len(mine)}", flush=True)

    token = get_token()
    if not token:
        print(f"[shard {SHARD}] FATAL: could not obtain 123 access_token, aborting", flush=True)
        json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
        sys.exit(1)

    stat = {"ledger_skip": 0, "kv_skip": 0, "warmed_thumb": 0, "warmed_page": 0,
            "no_file": 0, "transient": 0, "attempted": 0}
    pilot_n = int(PILOT) if PILOT else None

    try:
        for book_id, pan_dir_id, has_thumb in mine:
            if time.time() > deadline:
                print(f"[shard {SHARD}] hit soft deadline ({SOFT_DEADLINE_MIN} min), wrapping up", flush=True)
                break
            if book_id in ledger:
                stat["ledger_skip"] += 1
                continue
            if pilot_n is not None and stat["attempted"] >= pilot_n:
                print(f"[shard {SHARD}] hit PILOT cap ({pilot_n}), wrapping up", flush=True)
                break

            primary_name = "thumb.webp" if has_thumb else "page_0001.webp"
            # authoritative KV check on the expected primary key -- zero 123 calls if already warm
            if kv_get(f"pan:dlink:{pan_dir_id}:{primary_name}"):
                ledger.add(book_id)
                stat["kv_skip"] += 1
                continue

            stat["attempted"] += 1
            if not token:
                stat["transient"] += 1
                continue

            url = None
            used_name = None
            if has_thumb:
                url, _ = resolve_direct_link(token, pan_dir_id, "thumb.webp", deadline)
                used_name = "thumb.webp"
            if not url:
                url, _ = resolve_direct_link(token, pan_dir_id, "page_0001.webp", deadline)
                used_name = "page_0001.webp"

            if url:
                ledger.add(book_id)
                if used_name == "thumb.webp":
                    stat["warmed_thumb"] += 1
                else:
                    stat["warmed_page"] += 1
            else:
                # Could not resolve either filename's direct link this attempt (neither file
                # exists in that pan_dir_id, or 123 was transiently unavailable/rate-limited
                # through both tries). Not ledgered -- picked up again next run. Deliberately
                # NOT a hard-stop-the-shard condition: this is a per-book batch job processing
                # thousands of independent rows, a few bad/edge-case ones are expected and
                # should just be tallied, not abort everything after them (matches this repo's
                # ocr.py: per-item try/except that logs and moves on, never aborts the shard).
                stat["transient"] += 1
                print(f"  no-dlink {book_id} (pan_dir_id={pan_dir_id}, tried thumb={has_thumb})", flush=True)

            n = stat["attempted"]
            if n % 25 == 0:
                el = time.time() - t_start
                print(f"[shard {SHARD}] progress: attempted={n} ledger_skip={stat['ledger_skip']} "
                      f"kv_skip={stat['kv_skip']} warmed_thumb={stat['warmed_thumb']} "
                      f"warmed_page={stat['warmed_page']} transient={stat['transient']} "
                      f"elapsed={el:.0f}s backoff={_backoff['v']:.1f}s", flush=True)
    finally:
        json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[shard {SHARD}] ledger saved, {len(ledger)} entries", flush=True)

    el = time.time() - t_start
    print(f"=== shard {SHARD}/{TOTAL} done in {el:.0f}s: attempted={stat['attempted']} "
          f"ledger_skip={stat['ledger_skip']} kv_skip={stat['kv_skip']} "
          f"warmed_thumb={stat['warmed_thumb']} warmed_page={stat['warmed_page']} "
          f"transient={stat['transient']} ===", flush=True)


if __name__ == "__main__":
    main()
