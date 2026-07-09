# -*- coding: utf-8 -*-
# Cloud public-domain classics text harvester (GitHub Actions).
# Source: public JSON title index -> per-book reading pages via CF edge relay (multi-IP, gentle).
# Extracts main-text cells -> uploads plain text to R2 (_ctext/<slug>.txt). Idempotent, per-run cap, cron-resumable.
# Gentle: low concurrency + pause; only public-domain text as AI corpus.
import os, re, html, time, json, threading, urllib.request, urllib.parse, ssl, boto3
from concurrent.futures import ThreadPoolExecutor

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
LIMIT = int(os.environ.get("LIMIT", "400"))            # new books per shard per run
EG = os.environ.get("EG", "https://ctext-egress.hosonzuo.workers.dev/fetch?url=")
LIST = os.environ.get("LIST", "https://api.ctext.org/gettexttitles")
PAUSE = float(os.environ.get("PAUSE", "0.8"))
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"
s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")


def eg_get(u, timeout=45):
    r = urllib.request.urlopen(urllib.request.Request(EG + urllib.parse.quote(u),
        headers={"User-Agent": UA}), timeout=timeout, context=CTX)
    return r.status, r.read().decode("utf-8", "replace")


def cells(h):
    out, prev = [], None
    for c in re.findall(r'<td[^>]*class="[^"]*ctext[^"]*"[^>]*>(.*?)</td>', h, re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
        if t and any('\u4e00' <= ch <= '\u9fff' for ch in t) and t != prev:
            out.append(t); prev = t
    return out


def chap_links(slug, h):
    seen, out = set(), []
    for hr in re.findall(r'href="/?(' + re.escape(slug) + r'/[^"/]+/zh)"', h):
        if hr not in seen:
            seen.add(hr); out.append("https://ctext.org/" + hr)
    return out


def harvest(urn):
    x = urn.split("ctp:")[-1]
    if re.match(r"^wb\d+$", x):                        # wiki single-page book
        s, h = eg_get("https://ctext.org/wiki.pl?if=gb&chapter=" + x[2:])
        return "\n".join(cells(h)) if s == 200 else ""
    s, idx = eg_get("https://ctext.org/%s/zh" % x)     # textdb: index -> chapters
    if s != 200:
        return ""
    ch = chap_links(x, idx)
    parts = []
    if ch:
        for cu in ch:
            try:
                cs, c = eg_get(cu)
                if cs == 200: parts += cells(c)
            except Exception:
                pass
            time.sleep(PAUSE)
    else:
        parts = cells(idx)
    # de-dup consecutive across chapters
    text, prev = [], None
    for p in parts:
        if p != prev: text.append(p); prev = p
    return "\n".join(text)


def slug_key(urn):
    return "_ctext/" + re.sub(r"[^A-Za-z0-9_-]", "_", urn.split("ctp:")[-1]) + ".txt"


def exists(key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key); return True
    except Exception:
        return False


WORKERS = int(os.environ.get("WORKERS", "8"))   # per-shard concurrency
books = json.loads(eg_get(LIST, 90)[1])["books"]
mine = [b for i, b in enumerate(books) if i % TOTAL == SHARD][:LIMIT]
print("shard %d/%d  mine %d  workers %d" % (SHARD, TOTAL, len(mine), WORKERS), flush=True)

lock = threading.Lock(); cnt = {"ok": 0, "err": 0, "skip": 0}


def work(b):
    key = slug_key(b["urn"])
    try:
        if exists(key):
            with lock: cnt["skip"] += 1
            return
        body = harvest(b["urn"])
        if body and len(body) > 60:
            s3.put_object(Bucket=BUCKET, Key=key, Body=body.encode("utf-8"))
            with lock:
                cnt["ok"] += 1
                if cnt["ok"] % 50 == 0:
                    print("  ok=%d skip=%d err=%d" % (cnt["ok"], cnt["skip"], cnt["err"]), flush=True)
        else:
            with lock: cnt["err"] += 1
    except Exception:
        with lock: cnt["err"] += 1
    time.sleep(PAUSE)


with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, mine))

s3.put_object(Bucket=BUCKET, Key="_ledger/ctext_%d.json" % SHARD,
              Body=json.dumps({"shard": SHARD, "n": len(mine), **cnt,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}).encode())
print("=== shard %d ctext ok %d, skip %d, err %d ===" % (SHARD, cnt["ok"], cnt["skip"], cnt["err"]), flush=True)
