# R2 "_ctext/" 前缀 -> 123云盘"ctext"文件夹(fileId固定)· 传完删R2(省R2存储费)
# 取代原来那个 CF Worker "r2-to-123"(每分钟跑/传根目录/已禁用cron,教训见 memory)
import os, sys, time, json, hashlib
import boto3
import requests
from botocore.config import Config

S_EP = os.environ["S_EP"]; S_AK = os.environ["S_AK"]; S_SK = os.environ["S_SK"]
S_BUCKET = os.environ["S_BUCKET"]
PAN_BASE = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PAN_CID = os.environ["PAN_CID"]; PAN_SEC = os.environ["PAN_SEC"]
LIMIT = int(os.environ.get("LIMIT", "500"))

s3 = boto3.client("s3", endpoint_url=S_EP, aws_access_key_id=S_AK, aws_secret_access_key=S_SK,
                   region_name="auto", config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}))

S = requests.Session()
_tok = {"v": None}

def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN_BASE + "/api/v1/access_token", headers={"Platform": "open_platform", "Content-Type": "application/json"},
               json={"clientID": PAN_CID, "clientSecret": PAN_SEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        sys.exit("123 token failed: " + r.text[:200])
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
    sys.exit("找不到也建不了ctext文件夹: " + r.text[:300])

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
        return True, None
    servers = d.get("servers") or []
    if not servers:
        return False, f"create resp: {json.dumps(j, ensure_ascii=False)[:300]}"
    hb = {"Platform": "open_platform", "Authorization": "Bearer " + token()}
    ru = S.post(servers[0] + "/upload/v2/file/single/create",
                data={"parentFileID": str(parent_id), "filename": filename, "etag": md5, "size": str(size)},
                files={"file": (filename, data)}, headers=hb, timeout=60)
    juu = ru.json() or {}
    du = juu.get("data") or {}
    if du.get("completed"):
        return True, None
    return False, f"single/create resp: {json.dumps(juu, ensure_ascii=False)[:300]}"

def main():
    ctext_dir = find_or_create_ctext_dir()
    print(f"ctext目录 fileId={ctext_dir}", flush=True)
    print(f"扫 R2[{S_BUCKET}] prefix=_ctext/ limit={LIMIT}", flush=True)
    paginator = s3.get_paginator("list_objects_v2")
    ok = fail = 0
    scanned = 0
    for page in paginator.paginate(Bucket=S_BUCKET, Prefix="_ctext/", PaginationConfig={"MaxItems": LIMIT}):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            scanned += 1
            try:
                body = s3.get_object(Bucket=S_BUCKET, Key=key)["Body"].read()
                name = key.replace("_ctext/", "").replace("/", "_")
                success, err = upload_to_pan(ctext_dir, name, body)
                if success:
                    s3.delete_object(Bucket=S_BUCKET, Key=key)
                    ok += 1
                else:
                    fail += 1
                    print(f"  [upload fail] {key} :: {err}", flush=True)
            except Exception as e:
                fail += 1
                print(f"  [err] {key}: {str(e)[:150]}", flush=True)
            if scanned % 50 == 0:
                print(f"  progress {scanned} scanned, ok={ok} fail={fail}", flush=True)
    print(f"=== done: scanned={scanned} ok={ok} fail={fail} ===", flush=True)

if __name__ == "__main__":
    main()
