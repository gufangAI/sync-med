"""Aggregate guji backup shard ledgers and write summary to R2 + GitHub step summary."""
import os, json, boto3

EP = os.environ["S_EP"]
AK = os.environ["S_AK"]
SK = os.environ["S_SK"]
BKT = "guji-sea"
LEDGER_PFX = "_ledger_guji/"

for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK,
                  aws_secret_access_key=SK, region_name="auto")

paginator = s3.get_paginator("list_objects_v2")
pages_iter = paginator.paginate(Bucket=BKT, Prefix=LEDGER_PFX)

STATUSES = ["ok", "reuse", "dup", "skip-done", "skip-empty", "skip-noname", "have", "err", "other"]
zip_counts = {s: 0 for s in STATUSES}
pdf_counts = {s: 0 for s in STATUSES}
total_books = 0
total_pages = 0


def classify(st):
    s = str(st)
    if s in zip_counts:
        return s
    if s.startswith("err"):
        return "err"
    return "other"


for page in pages_iter:
    for obj in (page.get("Contents") or []):
        k = obj["Key"]
        if not k.endswith(".json") or "summary" in k:
            continue
        try:
            data = json.loads(s3.get_object(Bucket=BKT, Key=k)["Body"].read())
        except Exception:
            continue
        for row in data:
            total_books += 1
            total_pages += row.get("pages", 0)
            zip_counts[classify(row.get("zip", "other"))] += 1
            pdf_counts[classify(row.get("pdf", "other"))] += 1

summary = {
    "total_books": total_books,
    "total_pages": total_pages,
    "zip": zip_counts,
    "pdf": pdf_counts,
}
s3.put_object(Bucket=BKT, Key=LEDGER_PFX + "summary.json",
              Body=json.dumps(summary, ensure_ascii=False, indent=2).encode())
print("summary written to R2", flush=True)

zip_rows = "\n".join(f"| {k} | {v} |" for k, v in zip_counts.items() if v)
pdf_rows = "\n".join(f"| {k} | {v} |" for k, v in pdf_counts.items() if v)
md = f"""## guji backup summary

| metric | value |
|--------|-------|
| books processed | {total_books} |
| pages total | {total_pages} |

### zip status
| status | count |
|--------|-------|
{zip_rows}

### pdf status
| status | count |
|--------|-------|
{pdf_rows}
"""

summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
if summary_path:
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(md + "\n")

print(json.dumps(summary, indent=2), flush=True)
