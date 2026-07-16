# -*- coding: utf-8 -*-
# ctext.org text harvester -> 直传123云盘"ctext"文件夹(GitHub Actions矩阵分片)。
# 不再经R2:抓取+上传123在同一个job内完成;去重账本ledger.json走actions/cache(GitHub自带免费缓存),
# 缺失/首次跑时从123实际文件列表自愈(绝不裸跑致重复上传)。
import os, re, html, time, json, hashlib, threading, urllib.request, urllib.parse, ssl
import requests
from concurrent.futures import ThreadPoolExecutor

SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
LIMIT = int(os.environ.get("LIMIT", "400"))            # new books per shard per run
EG = os.environ.get("EG", "https://ctext-egress.hosonzuo.workers.dev/fetch?url=")
LIST = os.environ.get("LIST", "https://api.ctext.org/gettexttitles")
PAUSE = float(os.environ.get("PAUSE", "0.8"))
WORKERS = int(os.environ.get("WORKERS", "8"))          # 只管抓取并发;123上传永远串行
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"

PAN_BASE = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PAN_CID = os.environ["PAN_CID"]; PAN_SEC = os.environ["PAN_SEC"]
LEDGER = "ledger.json"

S = requests.Session()
_tok = {"v": None}


def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN_BASE + "/api/v1/access_token", headers={"Platform": "open_platform", "Content-Type": "application/json"},
               json={"clientID": PAN_CID, "clientSecret": PAN_SEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        raise SystemExit("123 token failed: " + r.text[:200])
    return _tok["v"]


def list_dir(parent_id):
    out, last = [], 0
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    while True:
        r = S.get(PAN_BASE + "/api/v2/file/list",
                  params={"parentFileId": parent_id, "limit": 100, "lastFileId": last}, headers=h, timeout=20)
        j = r.json()
        d = j.get("data") or {}
        fl = d.get("fileList") or []
        out.extend(fl)
        last = d.get("lastFileId", -1)
        if last in (-1, 0, None) or not fl:
            break
    return out


def find_or_create_ctext_dir():
    """按名字在根目录找"ctext"文件夹;不存在就建。不写死fileId,避免跨账号ID不通用。"""
    root = list_dir(0)
    hit = next((f for f in root if f.get("filename") == "ctext" and f.get("type") == 1), None)
    if hit:
        return int(hit["fileId"])
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token(), "Content-Type": "application/json"}
    r = S.post(PAN_BASE + "/upload/v1/file/mkdir", json={"parentID": 0, "name": "ctext"}, headers=h, timeout=20)
    d = (r.json() or {}).get("data") or {}
    if d.get("dirID"):
        return int(d["dirID"])
    raise SystemExit("找不到也建不了ctext文件夹: " + r.text[:300])


def upload_to_pan(parent_id, filename, data):
    md5 = hashlib.md5(data).hexdigest()
    size = len(data)
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token(), "Content-Type": "application/json"}
    r = S.post(PAN_BASE + "/upload/v2/file/create",
               json={"parentFileID": parent_id, "filename": filename, "etag": md5, "size": size, "duplicate": 2},
               headers=h, timeout=30)
    j = r.json()
    if j.get("code") == 401:
        _tok["v"] = None
        h["Authorization"] = "Bearer " + token()
        r = S.post(PAN_BASE + "/upload/v2/file/create",
                   json={"parentFileID": parent_id, "filename": filename, "etag": md5, "size": size, "duplicate": 2},
                   headers=h, timeout=30)
        j = r.json()
    d = j.get("data") or {}
    if d.get("reuse"):
        return True
    servers = d.get("servers") or []
    if not servers:
        print("  [create fail] %s :: %s" % (filename, json.dumps(j, ensure_ascii=False)[:200]), flush=True)
        return False
    hb = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    ru = S.post(servers[0] + "/upload/v2/file/single/create",
                data={"parentFileID": str(parent_id), "filename": filename, "etag": md5, "size": str(size)},
                files={"file": (filename, data)}, headers=hb, timeout=60)
    juu = ru.json() or {}
    du = juu.get("data") or {}
    if du.get("completed"):
        return True
    print("  [upload fail] %s :: %s" % (filename, json.dumps(juu, ensure_ascii=False)[:200]), flush=True)
    return False


def eg_get(u, timeout=45):
    r = urllib.request.urlopen(urllib.request.Request(EG + urllib.parse.quote(u),
        headers={"User-Agent": UA}), timeout=timeout, context=CTX)
    return r.status, r.read().decode("utf-8", "replace")


def cells(h):
    out, prev = [], None
    for c in re.findall(r'<td[^>]*class="[^"]*ctext[^"]*"[^>]*>(.*?)</td>', h, re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
        if t and any('一' <= ch <= '鿿' for ch in t) and t != prev:
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
    text, prev = [], None
    for p in parts:
        if p != prev: text.append(p); prev = p
    return "\n".join(text)


def slug_name(urn):
    return re.sub(r"[^A-Za-z0-9_-]", "_", urn.split("ctp:")[-1]) + ".txt"


def load_ledger():
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def bootstrap_ledger_from_pan(ctext_dir, want_names):
    """ledger.json缺失(首次跑/cache被清)时,从123实际文件列表自愈,绝不裸跑致重复上传。"""
    existing = {f.get("filename") for f in list_dir(ctext_dir)}
    return want_names & existing


def save_ledger(done):
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, ensure_ascii=False)


def main():
    books = json.loads(eg_get(LIST, 90)[1])["books"]
    mine = [b for i, b in enumerate(books) if i % TOTAL == SHARD][:LIMIT]
    print("shard %d/%d  mine %d  workers %d" % (SHARD, TOTAL, len(mine), WORKERS), flush=True)

    ctext_dir = find_or_create_ctext_dir()
    print("ctext目录 fileId=%d" % ctext_dir, flush=True)

    done = load_ledger()
    ledger_was_empty = not done
    want_names = {slug_name(b["urn"]) for b in mine}
    if ledger_was_empty and want_names:
        done |= bootstrap_ledger_from_pan(ctext_dir, want_names)
        print("ledger自愈:从123现有文件补回 %d 条" % len(done), flush=True)

    lock = threading.Lock()
    cnt = {"ok": 0, "skip": 0, "err": 0}
    scraped = []   # [(filename, text_bytes)]

    def scrape_one(b):
        name = slug_name(b["urn"])
        if name in done:
            with lock: cnt["skip"] += 1
            return
        try:
            body = harvest(b["urn"])
            if body and len(body) > 120:  # 提高阈值(原60太低,实测<=~100字符的多是目录/索引条目非正文,2026-07-17)
                with lock: scraped.append((name, body.encode("utf-8")))
            else:
                with lock: cnt["err"] += 1
        except Exception:
            with lock: cnt["err"] += 1
        time.sleep(PAUSE)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(scrape_one, mine))

    print("抓取完成,待传123: %d 篇(串行传,别撑爆123并发)" % len(scraped), flush=True)

    for name, data in scraped:
        ok = upload_to_pan(ctext_dir, name, data)
        if ok:
            cnt["ok"] += 1
            done.add(name)
            if cnt["ok"] % 50 == 0:
                print("  ok=%d skip=%d err=%d" % (cnt["ok"], cnt["skip"], cnt["err"]), flush=True)
        else:
            cnt["err"] += 1

    save_ledger(done)
    print("=== shard %d ctext ok %d, skip %d, err %d (ledger共%d条) ===" % (SHARD, cnt["ok"], cnt["skip"], cnt["err"], len(done)), flush=True)


if __name__ == "__main__":
    main()
