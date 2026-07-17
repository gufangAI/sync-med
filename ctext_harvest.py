# -*- coding: utf-8 -*-
# ctext.org text harvester -> 直传123云盘"ctext"文件夹(GitHub Actions矩阵分片)。
# 不再经R2:抓取+上传123在同一个job内完成;去重账本ledger.json走actions/cache(GitHub自带免费缓存),
# 缺失/首次跑时从123实际文件列表自愈(绝不裸跑致重复上传)。
#
# 2026-07-17 创始人钦定规范:文件名必须是中文(书名),内容开头须有"書名/作者/說明"头部——
#   ctext的gettexttitles API只给title+urn,没有author/description字段(实测确认,非疏漏)。
#   作者用免费模型(智谱glm-4-flash)批量查——经典古籍作者学界大多有公论,查不准的老实标"作者不詳",
#   绝不瞎编;结果按title缓存(同名书作者不变,不用每次都查,走actions/cache同一套机制)。
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
AUTHOR_CACHE = "author_cache.json"

ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"

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


# ── 中文文件名 + 書名/作者/說明 头部(2026-07-17 创始人钦定规范)────────────
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def cn_filename(title, urn):
    """文件名 = 中文书名 + urn简称(保证唯一,同名书不冲突;urn本身天然唯一)。"""
    x = urn.split("ctp:")[-1]
    safe_title = _UNSAFE.sub("", title).strip() or x
    return f"{safe_title}_{x}.txt"


def load_author_cache():
    if os.path.exists(AUTHOR_CACHE):
        try:
            with open(AUTHOR_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_author_cache(cache):
    with open(AUTHOR_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)


def lookup_authors_batch(titles):
    """批量查经典古籍作者(智谱glm-4-flash免费档),每批20个题名,查不准老实标"作者不詳",绝不瞎编。"""
    if not ZHIPU_KEY or not titles:
        return {t: "作者不詳" for t in titles}
    out = {}
    batch_size = 20
    for i in range(0, len(titles), batch_size):
        batch = titles[i:i + batch_size]
        prompt = (
            "你是中国古典文献学专家。给出下列古籍题名各自公认的作者(或编者/传述者)。\n"
            "规则:①只写学界公认、有共识的作者,不确定/存在争议/佚名/历代递修无定论的,一律写\"作者不詳\";"
            "②绝不编造;③每个题名一行,格式:题名=作者。\n\n"
            + "\n".join(f"- {t}" for t in batch)
        )
        try:
            r = S.post(
                ZHIPU_BASE + "/chat/completions",
                headers={"Authorization": f"Bearer {ZHIPU_KEY}", "Content-Type": "application/json"},
                json={"model": "glm-4-flash", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                timeout=60,
            )
            content = r.json()["choices"][0]["message"]["content"]
            for line in content.strip().split("\n"):
                if "=" in line:
                    t, a = line.split("=", 1)
                    t, a = t.strip().lstrip("-").strip(), a.strip()
                    if t in batch:
                        out[t] = a or "作者不詳"
        except Exception as e:
            print("  [作者查询失败] %s" % e, flush=True)
        for t in batch:
            out.setdefault(t, "作者不詳")
        time.sleep(0.3)
    return out


def build_header(title, author, urn):
    return (
        f"書名：{title}\n"
        f"作者：{author}\n"
        f"說明：本文據中國哲學書電子化計劃(ctext.org)收錄版本整理，僅作古籍研究語料使用。URN：{urn}\n"
        f"{'─' * 40}\n\n"
    )


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

    # 作者批量查询(先查缓存,缺的才调模型;结果落盘复用,同名书不重复查)
    author_cache = load_author_cache()
    titles_needed = [b["title"] for b in mine if b["title"] not in author_cache]
    if titles_needed:
        fresh = lookup_authors_batch(sorted(set(titles_needed)))
        author_cache.update(fresh)
        save_author_cache(author_cache)
        print("作者查询: 新查 %d 个题名" % len(set(titles_needed)), flush=True)

    done = load_ledger()
    ledger_was_empty = not done
    want_names = {cn_filename(b["title"], b["urn"]) for b in mine}
    if ledger_was_empty and want_names:
        done |= bootstrap_ledger_from_pan(ctext_dir, want_names)
        print("ledger自愈:从123现有文件补回 %d 条" % len(done), flush=True)

    lock = threading.Lock()
    cnt = {"ok": 0, "skip": 0, "err": 0}
    scraped = []   # [(filename, text_bytes)]

    def scrape_one(b):
        name = cn_filename(b["title"], b["urn"])
        if name in done:
            with lock: cnt["skip"] += 1
            return
        try:
            body = harvest(b["urn"])
            if body and len(body) > 120:  # 提高阈值(原60太低,实测<=~100字符的多是目录/索引条目非正文,2026-07-17)
                author = author_cache.get(b["title"], "作者不詳")
                full = build_header(b["title"], author, b["urn"]) + body
                with lock: scraped.append((name, full.encode("utf-8")))
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

    save_ledger(done)
    print("完成: ok=%d skip=%d err=%d" % (cnt["ok"], cnt["skip"], cnt["err"]), flush=True)


if __name__ == "__main__":
    main()
