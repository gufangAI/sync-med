import os, requests

PAN_BASE = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PAN_CID = os.environ["PAN_CID"]; PAN_SEC = os.environ["PAN_SEC"]

S = requests.Session()
r = S.post(PAN_BASE + "/api/v1/access_token", headers={"Platform": "open_platform", "Content-Type": "application/json"},
           json={"clientID": PAN_CID, "clientSecret": PAN_SEC}, timeout=30)
tok = r.json().get("data", {}).get("accessToken")
h = {"Platform": "open_platform", "Authorization": "Bearer " + tok}

# 尝试常见的用户信息端点(不确定哪个真存在,都试一遍)
for path in ["/api/v1/user/info", "/api/v1/user/detail", "/api/v1/user", "/api/v1/user/quota"]:
    rr = S.get(PAN_BASE + path, headers=h, timeout=15)
    print(f"{path} -> {rr.status_code} :: {rr.text[:300]}", flush=True)

print("\n=== 根目录完整列表(帮忙认出这是哪个账号) ===", flush=True)
out, last = [], 0
while True:
    rr = S.get(PAN_BASE + "/api/v2/file/list", params={"parentFileId": 0, "limit": 100, "lastFileId": last}, headers=h, timeout=20)
    j = rr.json()
    d = j.get("data") or {}
    fl = d.get("fileList") or []
    out.extend(fl)
    last = d.get("lastFileId", -1)
    if last in (-1, 0, None) or not fl:
        break
for f in out:
    print(f"  {f.get('filename')}  type={f.get('type')}  size={f.get('size')}", flush=True)
print(f"共 {len(out)} 个根目录条目", flush=True)
