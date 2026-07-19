# -*- coding: utf-8 -*-
# GitHub Actions + NDLOCR-Lite(国立国会図書館官方OCR,CC BY 4.0,CPU免GPU)
# 页图 <- 阅读器真实公开API(穿透123兜底,不直读R2——R2的book/前缀影像已于2026-07-17迁123,直读会全部NoSuchKey)
# 识别结果 -> R2 _ocr/{book_id}/page_NNNN.txt(与RapidOCR那条ocr.py同一落点,阅读器fulltext.js两边通吃)
import os, io, json, re, time, subprocess, sys, boto3, requests

_CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ゟ゠-ヿ]")

def cjk_ratio(s):
    """2026-07-19实测:垃圾幻觉块(如'State the the...'、'1/00 000...FORE')CJK占比恒为0,
    真实古籍/漢方文字块恒接近1.0——即便置信度被模型判高(实测垃圾块confidence=0.944也见过),
    CJK占比仍能正确区分,双重判据比单一置信度更可靠。"""
    t = re.sub(r"\s", "", s or "")
    if not t:
        return 0.0
    return len(_CJK_RE.findall(t)) / len(t)

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]; BUCKET = os.environ["S_BUCKET"]
CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
PAN_CID = os.environ["PAN_CLIENT_ID"]; PAN_SEC = os.environ["PAN_CLIENT_SECRET"]
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
PILOT = os.environ.get("PILOT", "").strip()

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK, region_name="auto")

PAN = "https://open-api.123pan.com"
_tok = {"v": None}

def pan_token():
    if _tok["v"]:
        return _tok["v"]
    r = requests.post(PAN + "/api/v1/access_token",
                       headers={"Platform": "open_platform", "Content-Type": "application/json"},
                       json={"clientID": PAN_CID, "clientSecret": PAN_SEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        raise SystemExit("123 token 获取失败: " + r.text[:200])
    return _tok["v"]

def fetch_page_from_123(pan_dir_id, page_str):
    # 与生产代码 functions/api/_lib/pan123.js 的 fetchPageFrom123 同一逻辑(内部服务用途,不走消费者门禁)
    if not pan_dir_id:
        return None
    h = {"Platform": "open_platform", "Authorization": "Bearer " + pan_token()}
    filename = f"page_{page_str}.webp"
    last_id, file_id = 0, None
    for _ in range(20):
        r = requests.get(f"{PAN}/api/v2/file/list", params={"parentFileId": pan_dir_id, "limit": 100, "lastFileId": last_id},
                          headers=h, timeout=30)
        d = r.json().get("data") or {}
        fl = d.get("fileList") or []
        hit = next((f for f in fl if f.get("filename") == filename), None)
        if hit:
            file_id = hit.get("fileId") or hit.get("fileID")
            break
        last_id = d.get("lastFileId")
        if last_id in (None, -1) or not fl:
            break
    if not file_id:
        return None
    r = requests.get(f"{PAN}/api/v1/file/download_info", params={"fileId": file_id}, headers=h, timeout=30)
    url = (r.json().get("data") or {}).get("downloadUrl")
    if not url:
        return None
    r = requests.get(url, timeout=60)
    return r.content if r.status_code == 200 else None

# D1 里拉候选书目:已上线影像、非宮内庁(合规待批,先排除)、按book_id分片
def d1_query(sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                       json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1查询失败: {str(j.get('errors',''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []

RUN_ID = os.environ.get("GITHUB_RUN_ID", "")

# 2026-07-19创始人指示:OCR集结进后台管理资产——每个shard跑完写一行汇总到ocr_jobs,
# 哨兵 book_id='_ndl_pipeline'/table_name='_pipeline_run'(与per-book行共存,见migrations/040)。
# 后台 Tab4Ocr「云端NDLOCR流水线」区块靠这行数据显示,不用手动查GitHub。
def d1_report_run(status, total, done_n, skip_n, err_n, low_conf_n, error_msg=""):
    now = int(time.time())
    try:
        d1_query(
            "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
            "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
            "created_at, started_at, finished_at, updated_at) "
            "VALUES ('_ndl_pipeline','_pipeline_run',?,?,?, 'ndlocr-lite', ?,?,?,?,?,?, ?,?,?,?)",
            [RUN_ID, SHARD, status, total, done_n, skip_n, err_n, low_conf_n,
             (error_msg or "")[:500], now, now, now, now],
        )
    except Exception as e:
        print(f"WARN D1汇总行写入失败(不影响OCR本身,只是后台看板少一条): {str(e)[:200]}", flush=True)

rows = d1_query(
    "SELECT book_id, page_count, pan_dir_id FROM books_assets_v2 "
    "WHERE frontend_visible=1 AND upload_status='done' AND page_count > 0 "
    "AND webp_prefix LIKE 'book/%' AND book_title NOT LIKE '%宮內廳%' AND pan_dir_id IS NOT NULL"
)
books = {r["book_id"]: (int(r["page_count"]), r["pan_dir_id"]) for r in rows if r.get("book_id") and r.get("page_count") and r.get("pan_dir_id")}
print(f"候选书目 {len(books)} 本(已排除宮内厅合规待批那批,已过滤无pan_dir_id的)", flush=True)

pages = []
for bid, (pc, pdid) in books.items():
    pages += [(bid, n, pdid) for n in range(1, pc + 1)]
pages.sort()
mine = [p for i, p in enumerate(pages) if i % TOTAL == SHARD]
# 2026-07-19实测教训:40分片硬分20246本书全部页数,单分片摊到上万页,CPU跑OCR(无GPU)
# 一片3.5小时一个都跑不完,会撞GitHub Actions 6小时job上限被杀、白跑一次什么都产不出。
# 改成每次运行硬顶RUN_CAP页(默认300,可用PILOT覆盖做更小的手动试跑):
# 保证job window内可靠完成→每次都有真实D1汇总产出;6小时cron自然分批啃完全量,不再空转赌大的。
RUN_CAP = int(PILOT) if PILOT else 300
mine = mine[:RUN_CAP]
print(f"shard {SHARD}/{TOTAL} 分到 {len(mine)}/{len(pages)} 页(本轮硬顶{RUN_CAP})  pilot={PILOT or '无'}", flush=True)

OCR_SRC = "ndlocr-lite/src"
TMP = "/tmp/ndl_work"
os.makedirs(TMP, exist_ok=True)

CONF_MIN = 0.6    # 2026-07-19实测标定:密排类书垃圾输出置信度0.25-0.49,正常识别0.9+,两者有明显断层
CJK_MIN = 0.3     # 2026-07-19实测标定:垃圾幻觉块CJK占比恒为0(纯拉丁字母/数字),真实文字块恒接近1.0

# 2026-07-19创始人指示:去重台账改用GitHub Actions cache(本地ledger.json),
# 不再逐页R2 head_object——省R2调用,也不再需要"删测试文件"碰destructive-op-gate。
LEDGER = "ledger.json"
ledger = set()
if os.path.exists(LEDGER):
    try:
        ledger = set(json.load(open(LEDGER, encoding="utf-8")))
    except Exception:
        ledger = set()
print(f"ledger已有 {len(ledger)} 条记录", flush=True)

done, skip, err, low_conf = 0, 0, 0, 0
for bid, p, pdid in mine:
    pstr = str(p).zfill(4)
    lkey = f"{bid}:{pstr}"
    txtkey = f"_ocr/{bid}/page_{pstr}.txt"
    if lkey in ledger:
        skip += 1
        continue

    img_path = f"{TMP}/page_{pstr}.webp"
    try:
        content = fetch_page_from_123(pdid, pstr)
        if not content:
            print(f"ERR拉图 {bid} p{p} 123未找到该页", flush=True)
            err += 1
            continue
        with open(img_path, "wb") as f:
            f.write(content)
    except Exception as e:
        print(f"ERR拉图异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        err += 1
        continue

    try:
        r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", img_path, "--output", TMP, "--json-only"],
                            cwd=OCR_SRC, capture_output=True, text=True, timeout=90)
        jf = f"{TMP}/page_{pstr}.json"
        if r.returncode != 0 or not os.path.exists(jf):
            print(f"ERR识别 {bid} p{p} :: {r.stderr[-150:]}", flush=True)
            err += 1
            continue
        data = json.load(open(jf, encoding="utf-8"))
        all_blocks = [b for pb in data.get("contents", []) for b in pb if b.get("text")]
        # 2026-07-19实测发现:空白衬页/馆藏章页、密排多栏类书版式会让模型幻觉出重复垃圾
        # (如"State the the the..."),confidence明显偏低(0.25-0.49 vs 正常识别0.9+)。
        # 逐块过滤而非整页一刀切:部分清晰部分模糊的页面,保留清晰部分,只丢垃圾块。
        kept = [b.get("text") for b in all_blocks
                if (b.get("confidence") or 0) >= CONF_MIN and cjk_ratio(b.get("text")) >= CJK_MIN]
        dropped = len(all_blocks) - len(kept)
        text = "\n".join(kept)
        if not text.strip():
            # 过滤完基本空了(整页低质量/真空白页)——标记为空,不存半页垃圾冒充"识别成功"
            s3.put_object(Bucket=BUCKET, Key=txtkey, Body=b"", ContentType="text/plain; charset=utf-8")
            low_conf += 1
            ledger.add(lkey)
            if dropped:
                print(f"低质量跳过 {bid} p{p}:{dropped}个块全部低于置信度{CONF_MIN},存空文件", flush=True)
        else:
            s3.put_object(Bucket=BUCKET, Key=txtkey, Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
            done += 1
            ledger.add(lkey)
            if dropped:
                print(f"部分过滤 {bid} p{p}:丢{dropped}个低置信度块,保留{len(kept)}个", flush=True)
            if done % 20 == 0:
                print(f"进度 {done}/{len(mine)}", flush=True)
    except Exception as e:
        print(f"ERR处理异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        err += 1
    finally:
        for f in (img_path, f"{TMP}/page_{pstr}.json"):
            try:
                os.remove(f)
            except Exception:
                pass

json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocr_ndl_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine), "done": done, "skip": skip, "err": err, "low_conf": low_conf}).encode())
d1_report_run("done", len(mine), done, skip, err, low_conf)
print(f"=== shard {SHARD} 完成 done={done} skip={skip} err={err} low_conf={low_conf} / {len(mine)} ===", flush=True)
