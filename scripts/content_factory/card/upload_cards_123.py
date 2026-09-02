#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把渲染好的方剂图文卡(out/*.png)上传到 123 网盘。

为什么存 123 不存 R2(创始人 2026-09-01 钦定):图片早已全迁 123,前台从 123 直链取图、
R2 已空。卡片存 123 才和现有图片服务架构一致、前台才取得到;存 R2 = 存到废弃位置。
遵零 R2 移动铁律(本来就不碰 R2)。

123 上传流程(create→拿URL→PUT→complete→轮询)直接复用生产 sync.py 的
token()/pan()/put_file() 三件套——sync.py 是脚本非库不可 import,故原样抄这三个函数
(单一实现的现实妥协:它们是同一套 proven 123 上传逻辑,改上传流程要同步改两处)。

需要的 env(与 sync.py 同款,workflow secret 已有):PAN_CID/PAN_SEC(123 写入凭据)、
PAN_DIR_CARDS(123 里存卡片的文件夹 id,创始人建一个 cards 文件夹给 id)。
无 PAN_DIR_CARDS → 跳过上传只本地(fork 自测),不报错。
"""
import os
import sys
import io
import time
import json
import hashlib
import glob

import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ.get("PAN_CID")
PSEC = os.environ.get("PAN_SEC")
DIR_CARDS = os.environ.get("PAN_DIR_CARDS")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

S = requests.Session()
S.trust_env = False
_tok = {"v": None}
_rl = {"streak": 0}


# ── 以下 token/pan/put_file 原样复用 sync.py(同一套 proven 123 上传逻辑)──────────
def token():
    if _tok["v"] is None:
        r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
                   json={"clientID": PCID, "clientSecret": PSEC}, timeout=60).json()
        _tok["v"] = (r.get("data") or {}).get("accessToken")
    return _tok["v"]


def pan(method, path, body=None):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    if body is not None:
        h["Content-Type"] = "application/json"
    delay = 2.0
    last = {}
    for _ in range(7):
        try:
            last = S.request(method, PAN + path, headers=h,
                             data=json.dumps(body) if body is not None else None, timeout=120).json()
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        msg = str(last.get("message", ""))
        code = last.get("code")
        if "exceeded" in msg or "tokens number" in msg or "频繁" in msg or code in (429, 401):
            if code == 401:
                _tok["v"] = None
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        _rl["streak"] = 0
        return last
    _rl["streak"] += 1
    return last


def put_file(local_path, parent_id, name):
    size = os.path.getsize(local_path)
    h = hashlib.md5()
    with open(local_path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    cr = pan("POST", "/upload/v1/file/create",
             {"parentFileID": parent_id, "filename": name, "etag": h.hexdigest(), "size": size})
    d = cr.get("data") or {}
    if d.get("reuse"):
        return "reuse"
    pid = d.get("preuploadID")
    if not pid:
        msg = str(cr.get("message") or "")
        if "重复" in msg or "已存在" in msg or "exist" in msg.lower():
            return "dup"
        return "err:" + msg[:40]
    url = (pan("POST", "/upload/v1/file/get_upload_url",
              {"preuploadID": pid, "sliceNo": 1}).get("data") or {}).get("presignedURL")
    with open(local_path, "rb") as f:
        S.put(url, data=f, timeout=1200)
    cd = pan("POST", "/upload/v1/file/upload_complete", {"preuploadID": pid}).get("data") or {}
    if cd.get("async"):
        for _ in range(180):
            time.sleep(1)
            if (pan("POST", "/upload/v1/file/upload_async_result",
                    {"preuploadID": pid}).get("data") or {}).get("completed"):
                return "ok"
        return "timeout"
    return "ok"
# ────────────────────────────────────────────────────────────────────────────


def main():
    pngs = sorted(glob.glob(os.path.join(OUT, "*.png")))
    if not pngs:
        print("upload_cards_123: out/ 无 png,渲染步骤可能没产出")
        return 0
    if not (PCID and PSEC and DIR_CARDS):
        print("upload_cards_123: 无 PAN_CID/PAN_SEC/PAN_DIR_CARDS → 跳过上传(fork 自测),只本地 %d 张" % len(pngs))
        return 0
    ok = dup = err = 0
    for p in pngs:
        name = os.path.basename(p)
        try:
            r = put_file(p, DIR_CARDS, name)
        except Exception as e:                                   # noqa: BLE001
            r = "err:" + str(e)[:40]
        if r in ("ok", "reuse"):
            ok += 1
        elif r == "dup":
            dup += 1
            print("  已存在(跳过): %s" % name)
        else:
            err += 1
            print("  ! 上传失败 %s: %s" % (name, r))
    print("123 上传: 成功 %d · 已存在 %d · 失败 %d / 共 %d 张 → 文件夹 %s" % (ok, dup, err, len(pngs), DIR_CARDS))
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
