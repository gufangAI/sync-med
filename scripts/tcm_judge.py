# TCM relevance judge over QA subset. Runs on GitHub Actions.
# Pulls subset csv.gz from R2 (S3 API), judges each row via XF maas chat API,
# writes verdict parts back to R2 + uploads artifact as fallback.
# Sharding: row_index % TOTAL == SHARD. Gentle concurrency (default 5/shard,
# pool shared with OCR line -> keep total across shards <= 20).
import os, sys, csv, gzip, io, json, time, base64, random
from concurrent.futures import ThreadPoolExecutor

import boto3
import urllib.request

SHARD = int(os.environ.get("SHARD", "0"))
TOTAL = int(os.environ.get("TOTAL", "4"))
LIMIT = int(os.environ.get("LIMIT", "0"))  # >0 = trial run on first N shard rows
CONC = int(os.environ.get("CONC", "5"))

XF_BASE = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
MODEL = "xopqwen36v35b"
KEYS = [k.strip() for k in os.environ["XF_KEYS"].replace("\n", ",").split(",") if k.strip()]

PROMPT = base64.b64decode(
    "5L2g5piv5Lit5Yy76K+t5paZ562b6YCJ5ZGY44CC5Yik5pat5LiL6Z2i6L+Z5p2h5Yy75oKj6Zeu562U55qE5qC45b+D5YaF5a655piv5ZCm5bGe5LqO5Lit5Yy76K+K55aX6IyD55W0KOi+qOivgeiuuuayu+OAgeS4reiNr+aWueWJguOAgemSiOeBuOaOqOaLv+OAgee7j+e7nOeptOS9jeetieS4uuS4u+imgeWGheWuuSks6ICM5LiN5piv6KW/5Yy76Zeu6K+K5Lit5Y+q6aG65bim5o+Q5Yiw5Lit6I2v5oiW5YGP5pa544CC5Y+q5Zue562U5LiA5Liq5pWw5a2XOjE95qC45b+D5piv5Lit5Yy7LDA95LiN5piv44CCCgrpl67nrZTlhoXlrrk6Cg=="
).decode("utf-8")


def s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S_EP"],
        aws_access_key_id=os.environ["S_AK"],
        aws_secret_access_key=os.environ["S_SK"],
    )


def ask(idx, row):
    text = ((row[1] or "") + "\n" + (row[2] or "") + "\n" + (row[3] or ""))[:1500]
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT + text}],
        "temperature": 0,
        "max_tokens": 8,
    }
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        key = KEYS[(idx + attempt) % len(KEYS)]
        req = urllib.request.Request(
            XF_BASE + "/chat/completions",
            data=data,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read().decode("utf-8"))
            ans = out["choices"][0]["message"]["content"].strip()
            v = 1 if ans.startswith("1") else 0
            return idx, row[0], v, ""
        except Exception as e:  # backoff on 429/5xx/timeouts, capped retries
            if attempt == 3:
                return idx, row[0], -1, type(e).__name__
            time.sleep(2 ** attempt + random.random() * 2)
    return idx, row[0], -1, "exhausted"


def main():
    cli = s3()
    bucket = "guyaofang-assets"
    obj = cli.get_object(Bucket=bucket, Key="_cc/tcm_subset.csv.gz")
    rows = []
    with gzip.open(io.BytesIO(obj["Body"].read()), "rt", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        next(rd, None)  # header
        for i, row in enumerate(rd):
            if i % TOTAL == SHARD and len(row) >= 4:
                rows.append((i, row))
    if LIMIT > 0:
        rows = rows[:LIMIT]
    print(f"shard {SHARD}/{TOTAL} rows={len(rows)} conc={CONC} keys={len(KEYS)}", flush=True)

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=CONC) as ex:
        for res in ex.map(lambda t: ask(t[0], t[1]), rows):
            results.append(res)
            done += 1
            if done % 500 == 0:
                pos = sum(1 for r in results if r[2] == 1)
                err = sum(1 for r in results if r[2] == -1)
                print(f"progress {done}/{len(rows)} pos={pos} err={err}", flush=True)

    pos = sum(1 for r in results if r[2] == 1)
    err = sum(1 for r in results if r[2] == -1)
    print(f"DONE shard={SHARD} judged={len(results)} tcm={pos} err={err}", flush=True)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["idx", "department", "verdict", "err"])
    for r in sorted(results):
        w.writerow(r)
    gz = gzip.compress(buf.getvalue().encode("utf-8"))
    name = f"part{SHARD}of{TOTAL}" + (f"_trial{LIMIT}" if LIMIT else "") + ".csv.gz"
    cli.put_object(Bucket=bucket, Key=f"_cc/tcm_judge/{name}", Body=gz)
    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", name), "wb") as f:
        f.write(gz)
    print(f"uploaded _cc/tcm_judge/{name} bytes={len(gz)}", flush=True)


if __name__ == "__main__":
    main()
