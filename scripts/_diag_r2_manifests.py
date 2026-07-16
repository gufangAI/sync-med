import os, json, requests

acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (acc, db)


def q(sql):
    r = requests.post(url, headers={"Authorization": "Bearer " + tok}, json={"sql": sql}, timeout=60)
    j = r.json()
    if not j.get("success"):
        print("QUERY FAIL:", sql, "->", str(j.get("errors"))[:300], flush=True)
        return []
    return (j.get("result") or [{}])[0].get("results") or []


cols = q("PRAGMA table_info(books_assets_v2)")
print("=== books_assets_v2 columns ===", flush=True)
for c in cols:
    print(" ", c.get("name"), c.get("type"), flush=True)

sample = q("SELECT * FROM books_assets_v2 WHERE frontend_visible=1 AND upload_status='done' LIMIT 3")
print("=== sample rows ===", flush=True)
for row in sample:
    print(" ", json.dumps(row, ensure_ascii=False)[:400], flush=True)

cnt = q("SELECT COUNT(*) as n FROM books_assets_v2 WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
print("=== eligible row count ===", cnt, flush=True)
