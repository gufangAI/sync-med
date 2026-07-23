# -*- coding: utf-8 -*-
# OCR 质量闸 · 独立抽样审计器(蓝图「数据飞轮」第一组件)。
# 从 R2 _ocr/ 已产出文本里抽样,用 ocr_quality.analyze 纯规则打质量分,
# 统计幻觉/乱码占比,输出报告(R2 + 本地 artifact),并落一份 reject 清单供退回重跑。
# GitHub Actions 云端跑,本机只发 HTTP,免费。不改任何已入库文本,只读+审计。
#
# 取样纪律(遵 zero-r2-move「零 LIST 全桶」):
#   书目来自 R2 manifest(_cc/med_pages.json,与 ocr.py 同源)或 D1 兜底;
#   逐书用 prefix=_ocr/{bid}/ 的 scoped LIST(单书小前缀,非全桶扫)取该书已产出页,
#   随机抽若干页 GET 打分。绝不 list_objects 全桶。
import os, io, re, json, random, time, datetime, boto3, requests
from botocore.exceptions import ClientError
import ocr_quality as q

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
SAMPLE = int(os.environ.get("SAMPLE", "400"))          # 目标抽样页数
PAGES_PER_BOOK = int(os.environ.get("PAGES_PER_BOOK", "6"))
MAX_BOOKS_SCAN = int(os.environ.get("MAX_BOOKS_SCAN", "400"))  # 最多探多少本书(有的书还没 OCR)
SEED = os.environ.get("SEED") or os.environ.get("GITHUB_RUN_ID") or str(int(time.time()))
PAGES_KEY = os.environ.get("PAGES_KEY", "_cc/med_pages.json")
DATE = datetime.date.today().isoformat()

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")
random.seed(SEED)


def load_books():
    """书目 {bid: page_count}。优先 R2 manifest,失败再 D1 兜底。"""
    try:
        m = json.loads(s3.get_object(Bucket=BUCKET, Key=PAGES_KEY)["Body"].read().decode("utf-8"))
        if m:
            print(f"manifest 载入 {len(m)} 本(源:{PAGES_KEY})", flush=True)
            return {k: int(v) for k, v in m.items() if int(v) > 0}
    except Exception as e:
        print(f"manifest 不可读({str(e)[:80]})-> 转 D1 兜底", flush=True)
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise SystemExit("manifest 读不到且缺 D1 凭据,无法取书目")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    sql = ("SELECT book_id, page_count FROM books_assets_v2 "
           "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0")
    r = requests.post(url, headers={"Authorization": "Bearer " + tok}, json={"sql": sql}, timeout=120)
    r.raise_for_status()
    rows = (r.json().get("result") or [{}])[0].get("results") or []
    out = {row["book_id"]: int(row["page_count"]) for row in rows if row.get("book_id") and row.get("page_count")}
    print(f"D1 载入 {len(out)} 本", flush=True)
    return out


def list_ocr_pages(bid):
    """scoped LIST 单书 _ocr/{bid}/ 下的 .txt key(小前缀,非全桶)。"""
    keys, token = [], None
    prefix = f"_ocr/{bid}/"
    for _ in range(20):  # 单书页数上限保护
        kw = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", []) if o["Key"].endswith(".txt")]
        token = r.get("NextContinuationToken")
        if not token:
            break
    return keys


def main():
    books = load_books()
    bids = list(books.keys())
    random.shuffle(bids)

    samples = []          # [(key, analysis)]
    books_with_ocr = 0
    scanned = 0
    for bid in bids:
        if len(samples) >= SAMPLE or scanned >= MAX_BOOKS_SCAN:
            break
        scanned += 1
        try:
            pages = list_ocr_pages(bid)
        except ClientError as e:
            print(f"WARN list {bid}: {str(e)[:80]}", flush=True)
            continue
        if not pages:
            continue
        books_with_ocr += 1
        pick = random.sample(pages, min(PAGES_PER_BOOK, len(pages)))
        for k in pick:
            if len(samples) >= SAMPLE:
                break
            try:
                txt = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read().decode("utf-8", "replace")
            except Exception as e:
                print(f"WARN get {k}: {str(e)[:60]}", flush=True)
                continue
            samples.append((k, q.analyze(txt)))

    n = len(samples)
    if n == 0:
        print(f"=== 抽样为空:探了 {scanned} 本书,_ocr/ 下没找到已产出文本(可能 OCR 产出也已清理/迁移)===", flush=True)
        # 仍写一份说明报告,便于看板确认"审计跑过、结论=无数据"
        _write_report([], scanned, 0, DATE)
        return

    by_label = {"ok": 0, "suspect": 0, "reject": 0, "empty": 0}
    reason_tally = {}
    for _, a in samples:
        by_label[a["label"]] = by_label.get(a["label"], 0) + 1
        for rs in a["reasons"]:
            tag = rs.split("=")[0].split("~")[0]
            reason_tally[tag] = reason_tally.get(tag, 0) + 1

    bad = n - by_label["empty"]  # 非空样本
    reject_rate = by_label["reject"] / n if n else 0
    print(f"=== OCR 质量审计 {DATE} ===", flush=True)
    print(f"抽样 {n} 页 / 探书 {scanned}(有 OCR 的 {books_with_ocr} 本)", flush=True)
    print(f"ok={by_label['ok']} suspect={by_label['suspect']} reject={by_label['reject']} empty={by_label['empty']}", flush=True)
    print(f"reject_rate={reject_rate:.1%}  top_reasons={sorted(reason_tally.items(), key=lambda x:-x[1])[:6]}", flush=True)

    _write_report(samples, scanned, books_with_ocr, DATE)
    _write_reject_list(samples, DATE)
    _d1_sentinel(n, by_label, reject_rate)


def _snip(key):
    """取 reject 页前 40 字符做证据片段(转义,避免回吐原文 CJK 进 artifact/日志)。"""
    try:
        t = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8", "replace")
        t = re.sub(r"\s+", " ", t).strip()[:40]
        return t.encode("unicode_escape").decode("ascii")
    except Exception:
        return ""


def _write_report(samples, scanned, books_with_ocr, date):
    n = len(samples)
    by_label = {"ok": 0, "suspect": 0, "reject": 0, "empty": 0}
    reason_tally = {}
    rejects = []
    for k, a in samples:
        by_label[a["label"]] = by_label.get(a["label"], 0) + 1
        for rs in a["reasons"]:
            tag = rs.split("=")[0].split("~")[0]
            reason_tally[tag] = reason_tally.get(tag, 0) + 1
        if a["label"] == "reject":
            rejects.append((k, a))

    lines = []
    lines.append(f"# OCR 质量闸 · 抽样审计报告 {date}")
    lines.append("")
    lines.append(f"- 抽样页数:**{n}**;探测书目 {scanned} 本(其中已有 _ocr 产出 {books_with_ocr} 本)")
    if n:
        lines.append(f"- 判级:ok **{by_label['ok']}** / suspect **{by_label['suspect']}** / "
                     f"reject **{by_label['reject']}** / empty **{by_label['empty']}**")
        lines.append(f"- **幻觉/乱码 reject 率 = {by_label['reject']/n:.1%}**;非空异常(reject+suspect)率 = "
                     f"{(by_label['reject']+by_label['suspect'])/n:.1%}")
        lines.append(f"- 命中判据 top:{sorted(reason_tally.items(), key=lambda x:-x[1])[:8]}")
    else:
        lines.append("- 抽样为空:_ocr/ 下未找到已产出文本(OCR 产出可能已随影像迁移/清理)。")
    lines.append("")
    if rejects:
        lines.append("## reject 样本(前 30,证据片段已转义)")
        lines.append("")
        lines.append("| key | reasons | snippet(40 chars, escaped) |")
        lines.append("|---|---|---|")
        for k, a in rejects[:30]:
            reasons = "; ".join(a["reasons"])
            lines.append(f"| `{k}` | {reasons} | `{_snip(k)}` |")
        lines.append("")
    lines.append("---")
    lines.append("*纯规则闸(重复短语/连续复读/整行复读/乱码率/单字符刷屏/CJK 占比)。"
                 "跨书语义串味需 LLM/语料统计复核,本闸只诚实覆盖上述便宜可靠的主流幻觉失败模式。*")
    md = "\n".join(lines)

    key = f"_cc/ocr_quality/{date}.md"
    try:
        s3.put_object(Bucket=BUCKET, Key=key, Body=md.encode("utf-8"), ContentType="text/markdown; charset=utf-8")
        print(f"报告已写 R2: {key}", flush=True)
    except Exception as e:
        print(f"WARN 报告写 R2 失败: {str(e)[:120]}", flush=True)
    try:
        with open("ocr_quality_report.md", "w", encoding="utf-8") as f:
            f.write(md)
        print("报告已写本地 ocr_quality_report.md(供 workflow artifact 上传)", flush=True)
    except Exception as e:
        print(f"WARN 本地报告写入失败: {str(e)[:120]}", flush=True)


def _write_reject_list(samples, date):
    rej = [{"key": k, "book_page": k[len("_ocr/"):].rsplit(".", 1)[0],
            "reasons": a["reasons"], "score": a["score"]}
           for k, a in samples if a["label"] == "reject"]
    if not rej:
        print("无 reject 页,不写 reject 清单", flush=True)
        return
    key = f"_cc/ocr_quality/reject_{date}.json"
    try:
        s3.put_object(Bucket=BUCKET, Key=key,
                      Body=json.dumps(rej, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json; charset=utf-8")
        print(f"reject 清单已写 R2: {key}({len(rej)} 条,供退回重跑)", flush=True)
    except Exception as e:
        print(f"WARN reject 清单写入失败: {str(e)[:120]}", flush=True)


def _d1_sentinel(n, by_label, reject_rate):
    """可选:写一行哨兵到 D1 ocr_jobs,让后台看板显示质量审计结果。缺凭据/失败均不致命。"""
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        return
    now = int(time.time())
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    sql = ("INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
           "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
           "created_at, started_at, finished_at, updated_at) "
           "VALUES ('_ocr_quality','_quality_audit',?,0,'done','quality-gate',?,?,?,?,?,?,?,?,?,?)")
    params = [os.environ.get("GITHUB_RUN_ID", ""), n, by_label["ok"], by_label["empty"],
              by_label["reject"], by_label["suspect"],
              f"reject_rate={reject_rate:.3f}", now, now, now, now]
    try:
        r = requests.post(url, headers={"Authorization": "Bearer " + tok},
                          json={"sql": sql, "params": params}, timeout=60)
        if r.json().get("success"):
            print("D1 看板哨兵行已写(book_id=_ocr_quality)", flush=True)
        else:
            print(f"WARN D1 哨兵写入未成功: {str(r.json().get('errors',''))[:160]}", flush=True)
    except Exception as e:
        print(f"WARN D1 哨兵写入异常(不影响审计): {str(e)[:120]}", flush=True)


if __name__ == "__main__":
    main()
