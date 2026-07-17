# -*- coding: utf-8 -*-
# ctext历史文件批量升级:旧命名(_ctext_{slug}.txt / {slug}.txt) -> 新规范(中文书名_urn.txt + 書名/作者/說明头部)
# 2026-07-17 创始人钦定:旧文件"改,不删"(零删除铁律)——下载旧内容→查书名(反查gettexttitles)→
#   查作者(智谱,复用同一套author_cache)→拼头部→传新文件名→确认传成功→再删旧文件名(绝不先删后传)。
import os, re, json, time, threading
import requests
from concurrent.futures import ThreadPoolExecutor

SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
LIMIT = int(os.environ.get("LIMIT", "300"))
PAN_BASE = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PAN_CID = os.environ["PAN_CID"]; PAN_SEC = os.environ["PAN_SEC"]
ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
LIST = "https://api.ctext.org/gettexttitles"
LEDGER = "migrate_ledger.json"      # 已处理过的旧文件名(不管成功失败,处理过就跳过,防重复扫)
AUTHOR_CACHE = "author_cache.json"  # 与 ctext_harvest.py 共用同一份缓存语义(各自独立文件,内容可合并)

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


def find_ctext_dir():
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    last = 0
    while True:
        r = S.get(PAN_BASE + "/api/v2/file/list", params={"parentFileId": 0, "limit": 100, "lastFileId": last}, headers=h, timeout=20)
        d = r.json().get("data") or {}
        for f in (d.get("fileList") or []):
            if f.get("filename") == "ctext" and f.get("type") == 1:
                return int(f["fileId"])
        last = d.get("lastFileId", -1)
        if last in (-1, 0, None):
            break
    raise SystemExit("找不到ctext文件夹")


def list_all_files(parent_id):
    """全量翻页拿ctext目录下所有未删除文件,返回list of dict(fileId, filename, size)"""
    out, last = [], 0
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    while True:
        r = S.get(PAN_BASE + "/api/v2/file/list", params={"parentFileId": parent_id, "limit": 100, "lastFileId": last}, headers=h, timeout=20)
        d = r.json().get("data") or {}
        fl = d.get("fileList") or []
        for f in fl:
            if not f.get("trashed"):
                out.append(f)
        last = d.get("lastFileId", -1)
        if last in (-1, 0, None) or not fl:
            break
    return out


_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def cn_filename(title, urn):
    x = urn.split("ctp:")[-1]
    safe_title = _UNSAFE.sub("", title).strip() or x
    return f"{safe_title}_{x}.txt"


def old_name_to_urn(name):
    """从旧文件名反推urn。两种旧格式:_ctext_{slug}.txt 或 {slug}.txt(wb数字book)。"""
    base = name[:-4] if name.endswith(".txt") else name
    if base.startswith("_ctext_"):
        slug = base[len("_ctext_"):]
    else:
        slug = base
    return "ctp:" + slug


def build_header(title, author, urn):
    return (
        f"書名：{title}\n"
        f"作者：{author}\n"
        f"說明：本文據中國哲學書電子化計劃(ctext.org)收錄版本整理，僅作古籍研究語料使用。URN：{urn}\n"
        f"{'─' * 40}\n\n"
    )


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def lookup_authors_batch(titles, cache):
    need = [t for t in titles if t not in cache]
    if not need or not ZHIPU_KEY:
        for t in need:
            cache.setdefault(t, "作者不詳")
        return
    batch_size = 20
    for i in range(0, len(need), batch_size):
        batch = need[i:i + batch_size]
        prompt = (
            "你是中国古典文献学专家。给出下列古籍题名各自公认的作者(或编者/传述者)。\n"
            "规则:①只写学界公认、有共识的作者,不确定/存在争议/佚名/历代递修无定论的,一律写\"作者不詳\";"
            "②绝不编造;③每个题名一行,格式:题名=作者。\n\n"
            + "\n".join(f"- {t}" for t in batch)
        )
        try:
            r = S.post(ZHIPU_BASE + "/chat/completions",
                       headers={"Authorization": f"Bearer {ZHIPU_KEY}", "Content-Type": "application/json"},
                       json={"model": "glm-4-flash", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                       timeout=60)
            content = r.json()["choices"][0]["message"]["content"]
            for line in content.strip().split("\n"):
                if "=" in line:
                    t, a = line.split("=", 1)
                    t, a = t.strip().lstrip("-").strip(), a.strip()
                    if t in batch:
                        cache[t] = a or "作者不詳"
        except Exception as e:
            print("  [作者查询失败] %s" % e, flush=True)
        for t in batch:
            cache.setdefault(t, "作者不詳")
        time.sleep(0.3)


def download_file(file_id):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    rd = S.get(PAN_BASE + "/api/v1/file/download_info", params={"fileId": file_id}, headers=h, timeout=20)
    url = (rd.json().get("data") or {}).get("downloadUrl")
    if not url:
        raise RuntimeError("no download url: " + json.dumps(rd.json())[:200])
    return requests.get(url, timeout=60).content


def upload_to_pan(parent_id, filename, data):
    import hashlib
    md5 = hashlib.md5(data).hexdigest()
    size = len(data)
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token(), "Content-Type": "application/json"}
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
    du = (ru.json() or {}).get("data") or {}
    return bool(du.get("completed"))


def trash_file(file_id):
    """123回收站删除(可恢复,非永久),只在新文件确认传成功后才调用。"""
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token(), "Content-Type": "application/json"}
    r = S.post(PAN_BASE + "/api/v1/file/trash", json={"fileIDs": [file_id]}, headers=h, timeout=20)
    return r.json().get("code") == 0


def main():
    print("拉取ctext.org题名表(urn->title)...", flush=True)
    titles_data = requests.get(LIST, timeout=90).json()["books"]
    urn2title = {b["urn"]: b["title"] for b in titles_data}
    print(f"题名表共{len(urn2title)}条", flush=True)

    ctext_dir = find_ctext_dir()
    print(f"ctext目录 fileId={ctext_dir}", flush=True)

    print("全量列目录(可能需要几分钟)...", flush=True)
    all_files = list_all_files(ctext_dir)
    print(f"目录共{len(all_files)}个文件", flush=True)

    new_style = re.compile(r'^[一-鿿㐀-䶿].*_[a-zA-Z0-9-]+\.txt$')
    old_files = [f for f in all_files if f.get("filename", "").endswith(".txt") and not new_style.match(f["filename"])]
    print(f"待升级旧文件共{len(old_files)}个", flush=True)

    ledger = set(load_json(LEDGER, []))
    mine = [f for i, f in enumerate(old_files) if i % TOTAL == SHARD and f["filename"] not in ledger][:LIMIT]
    print(f"shard {SHARD}/{TOTAL}  本次处理 {len(mine)} 个", flush=True)

    # 先解析urn+title,批量查作者(减少调用次数)
    resolved = []
    unresolved = 0
    for f in mine:
        urn = old_name_to_urn(f["filename"])
        title = urn2title.get(urn)
        if not title:
            unresolved += 1
            continue
        resolved.append((f, urn, title))
    if unresolved:
        print(f"  {unresolved}个文件反查不到题名(可能已被ctext下架),跳过", flush=True)

    author_cache = load_json(AUTHOR_CACHE, {})
    need_titles = sorted(set(t for _, _, t in resolved if t not in author_cache))
    if need_titles:
        lookup_authors_batch(need_titles, author_cache)
        save_json(AUTHOR_CACHE, author_cache)
        print(f"作者查询: 新查 {len(need_titles)} 个题名", flush=True)

    lock = threading.Lock()
    cnt = {"ok": 0, "err": 0}

    def migrate_one(item):
        f, urn, title = item
        old_name = f["filename"]
        try:
            old_content = download_file(f["fileId"])
            author = author_cache.get(title, "作者不詳")
            new_content = build_header(title, author, urn).encode("utf-8") + old_content
            new_name = cn_filename(title, urn)
            ok = upload_to_pan(ctext_dir, new_name, new_content)
            if ok:
                trash_file(f["fileId"])   # 新文件确认传成功后,才删旧文件(回收站,可恢复)
                with lock:
                    cnt["ok"] += 1
                    ledger.add(old_name)
            else:
                with lock: cnt["err"] += 1
        except Exception as e:
            print(f"  [迁移失败] {old_name} :: {e}", flush=True)
            with lock: cnt["err"] += 1

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(migrate_one, resolved))

    save_json(LEDGER, sorted(ledger))
    print(f"完成: ok={cnt['ok']} err={cnt['err']}", flush=True)


if __name__ == "__main__":
    main()
