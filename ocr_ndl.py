# -*- coding: utf-8 -*-
# GitHub Actions + NDLOCR-Lite(国立国会図書館官方OCR,CC BY 4.0,CPU免GPU)
# 页图 <- 阅读器真实公开API(穿透123兜底,不直读R2——R2的book/前缀影像已于2026-07-17迁123,直读会全部NoSuchKey)
# 识别结果 -> R2 _ocr/{book_id}/page_NNNN.txt(与RapidOCR那条ocr.py同一落点,阅读器fulltext.js两边通吃)
import os, io, json, re, time, subprocess, sys, boto3, requests
from collections import Counter
import ocr_quality   # 页级质量闸:与 ocr.py / ocr_xf.py / ocr_reflash.py 同一份幻觉/乱码判据

# CJK 占比改用 ocr_quality.cjk_ratio ——本文件原先自带一份字面完全相同的副本
# (同一个正则、同一个去空白规则)。同一判据留两份就是 ocr_degeneracy.py 那次合并
# 要治的病:拷贝各自漂移之后,同一本书这边判退那边放行,谁都说不清产线的判据到底是什么。
# NDL 线自己保留的只是"用哪个阈值"这个决定,见下面的 CJK_MIN。
cjk_ratio = ocr_quality.cjk_ratio

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
def d1_report_run(status, total, done_n, skip_n, err_n, low_conf_n, error_msg="", rej_n=0):
    now = int(time.time())
    # 质量闸判退数在 ocr_jobs 里没有自己的列,但它不能只印在 run log 里——铁律:
    # "凡是只写进日志的产线一律视为没人看"(pan-register 在日志里连喊 12 天零人响应)。
    # 判退率突然拉高 = 引擎退化或上游图质量塌了,是必须能在看板上看见的数字。
    # 借 error_msg 这个自由文本列带出去:纯新增,不动表结构,不碰任何已有列的语义。
    msg = error_msg or ""
    if rej_n:
        msg = (msg + " " if msg else "") + "rej=%d" % rej_n
    try:
        d1_query(
            "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
            "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
            "created_at, started_at, finished_at, updated_at) "
            "VALUES ('_ndl_pipeline','_pipeline_run',?,?,?, 'ndlocr-lite', ?,?,?,?,?,?, ?,?,?,?)",
            [RUN_ID, SHARD, status, total, done_n, skip_n, err_n, low_conf_n,
             msg[:500], now, now, now, now],
        )
    except Exception as e:
        print(f"WARN D1汇总行写入失败(不影响OCR本身,只是后台看板少一条): {str(e)[:200]}", flush=True)


# 2026-07-28:死页规模也必须落进 D1,不能只印在 run log 里。
# 铁律:"凡是只写进日志的产线一律视为没人看"——pan-register 在日志里连喊 12 天 not-in-D1=3965,
# 零人响应。死页数是同一类数字:它反映的是上游 123 缺页的规模,只有进了库才能上看板/告警。
# 哨兵行 book_id='_ndl_deadletter'/table_name='_pipeline_dead',与 per-book 行和
# '_pipeline_run' 汇总行共存;纯 INSERT,不改任何已有记录。
# 列的含义在这条产线上被复用,写清楚免得后人误读:
#   total_pages   = 死信涉及多少本书
#   done_pages    = 本轮新进死信的页数
#   skip_pages    = 本轮因冷却未重试的页数(这个数越大,说明省下的空转越多)
#   failed_pages  = 死信台账当前总页数
def d1_report_dead(dead_pages, dead_books, dead_new, cooled, top):
    now = int(time.time())
    msg = "dead=%d books=%d new=%d cooled=%d top=%s" % (
        dead_pages, dead_books, dead_new, cooled,
        ",".join("%s:%d" % (b, c) for b, c in top))
    try:
        d1_query(
            "INSERT INTO ocr_jobs (book_id, table_name, run_id, shard, status, engine, "
            "total_pages, done_pages, skip_pages, failed_pages, low_conf_pages, error_msg, "
            "created_at, started_at, finished_at, updated_at) "
            "VALUES ('_ndl_deadletter','_pipeline_dead',?,?, 'deadletter', 'ndlocr-lite', "
            "?,?,?,?,0,?, ?,?,?,?)",
            [RUN_ID, SHARD, dead_books, dead_new, cooled, dead_pages, msg[:500],
             now, now, now, now],
        )
    except Exception as e:
        print(f"WARN D1死信行写入失败(死页统计只剩日志一份): {str(e)[:200]}", flush=True)

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
# 2026-07-25 修停摆根因: 原先在查ledger之前就 mine[:RUN_CAP] 截断——每shard永远只看slice前300页,
# 做完即永久空转(fleet-watch实证连续3轮零产出)。改为先滤掉ledger已做,再截RUN_CAP,才会持续推进。
_ledger_early = set()
if os.path.exists("ledger.json"):
    try:
        _ledger_early = set(json.load(open("ledger.json", encoding="utf-8")))
    except Exception:
        _ledger_early = set()

# 2026-07-28 修"失败页无限重试":原先只有 done / low_conf 进 ledger,err 的页一次都不记,
# 于是每 6 小时 cron 一到就把同一批死页原样再拉一遍。审计实测 2.7 天 done=46749 页、
# err=93840 页——失败是成功的两倍,其中 92%+ 是"123未找到该页";抽样看 bcgm 这本书相隔 48 小时
# 的两次 run 失败在几乎完全相同的一组页码(p942/p1102/p1382…),是稳定死页不是偶发抖动。
# 更要命的不是浪费,是挤占:err 摊到每 shard 每轮约 217 页,而 RUN_CAP 只有 300,
# 死页已经吃掉七成预算且只增不减——照这个趋势这条线会走到 100% 空转,一页新书都产不出。
#
# 死信 ≠ 永久拉黑。"123未找到该页"很可能是上游同步滞后,页以后会补上,拉黑等于自断退路。
# 所以记「失败次数 n + 上次尝试时间 t」,按次数降频重探:前 FREE_TRIES 次照旧每轮重试
# (网络抖动、临时 5xx 不该被当死页),之后 1 天 → 3 天 → 7 天封顶。任何一页最迟一周内
# 都会被再试一次,上游补页后能自己恢复;成功即从死信摘除,数字能降下来。
DEADLETTER = "deadletter.json"
FREE_TRIES = 3
COOLDOWN_STEPS = ((FREE_TRIES, 0), (6, 86400), (10, 3 * 86400))
COOLDOWN_MAX = 7 * 86400


def cooldown_for(n):
    """失败 n 次之后,距离下次重试至少要等多少秒。"""
    for lim, wait in COOLDOWN_STEPS:
        if n < lim:
            return wait
    return COOLDOWN_MAX


dead = {}
if os.path.exists(DEADLETTER):
    try:
        _d = json.load(open(DEADLETTER, encoding="utf-8"))
        if isinstance(_d, dict):
            dead = _d
    except Exception:
        dead = {}
_dead_stat = {"new": 0}
_NOW = int(time.time())


def dead_due(k):
    e = dead.get(k)
    if not e:
        return True
    return (_NOW - int(e.get("t", 0))) >= cooldown_for(int(e.get("n", 0)))


def mark_dead(k, why):
    e = dead.get(k)
    if e is None:
        e = {"n": 0}
        _dead_stat["new"] += 1
    e["n"] = int(e.get("n", 0)) + 1
    e["t"] = int(time.time())
    e["why"] = why
    dead[k] = e


def clear_dead(k):
    """这页最终跑通了(常见于上游把缺的页补上了)——从死信摘掉。
    不摘的话死信数只会单调上涨,涨成一个没人信的数字。"""
    dead.pop(k, None)


# 冷却过滤必须发生在 RUN_CAP 截断【之前】:放在后面等于死页照样先占满 300 页预算,
# 这个修复就一点用都没有了。
_slice = [t for t in mine if f"{t[0]}:{str(t[1]).zfill(4)}" not in _ledger_early]
_before_cool = len(_slice)
_slice = [t for t in _slice if dead_due(f"{t[0]}:{str(t[1]).zfill(4)}")]
cooled = _before_cool - len(_slice)
mine = _slice[:RUN_CAP]
print(f"shard {SHARD}/{TOTAL} 分到 {len(mine)}/{len(pages)} 页"
      f"(已滤台账·避开冷却中的死页{cooled}页·本轮硬顶{RUN_CAP})  pilot={PILOT or '无'}", flush=True)
print(f"死信台账载入 {len(dead)} 页 / {len({k.split(':')[0] for k in dead})} 本", flush=True)

OCR_SRC = "ndlocr-lite/src"
TMP = "/tmp/ndl_work"
os.makedirs(TMP, exist_ok=True)

# ---- 块级闸(NDL 线独有,页级闸给不了)----
# ocr_quality.py 只看文本:它拿不到 confidence,也不做块粒度的取舍。这两条必须留着——
# 逐块过滤能在"部分清晰部分模糊"的页面上保住清晰的那部分,而不是整页一刀切。
# 2026-07-28 接页级闸之后两层是叠加关系,不是替代:
#   块级先剔掉坏块(低置信度 / 纯拉丁数字垃圾)→ 页级再看剩下的整页拼起来像不像人话。
CONF_MIN = 0.6    # 2026-07-19实测标定:密排类书垃圾输出置信度0.25-0.49,正常识别0.9+,两者有明显断层
CJK_MIN = 0.3     # 2026-07-19实测标定:垃圾幻觉块CJK占比恒为0(纯拉丁字母/数字),真实文字块恒接近1.0。
                  # 双重判据比单一置信度可靠:实测见过 confidence=0.944 的纯拉丁垃圾块,CJK 占比照样判得出。

# 页级闸的最短生效长度【已上收】。2026-07-28 本文件曾自带一份 GATE_MIN_CHARS,
# 用来挡住 repeat_ngram / max_run / line_dup 在短页上误杀方剂组成页;
# 同日这道限长已收进 ocr_quality.MIN_LEN_FOR_REPEAT,5 条线一致生效,
# 这里的副本随即撤掉——同一判据留两份就是 ocr_degeneracy.py 那次合并要治的病。
#
# 撤掉不是把行为改回去,是把行为改对:本地那份 GATE_MIN_CHARS 拦的是【整个 reject 判决】,
# 连 garbage / single_char(它们自己的下限是 20 字,早就验过)一起拦掉了,
# 于是 20~39 字的纯乱码页、单字符刷屏页在这条线上会被当好页写进 _ocr/。
# 收进模块后只有该挡的三条被挡,乱码/刷屏照常判死,本线不再是 5 条里唯一的例外。

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

done, skip, err, low_conf, rejected = 0, 0, 0, 0, 0
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
            mark_dead(lkey, "123-missing")
            err += 1
            continue
        with open(img_path, "wb") as f:
            f.write(content)
    except Exception as e:
        print(f"ERR拉图异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        mark_dead(lkey, "123-error")
        err += 1
        continue

    try:
        r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", img_path, "--output", TMP, "--json-only"],
                            cwd=OCR_SRC, capture_output=True, text=True, timeout=90)
        jf = f"{TMP}/page_{pstr}.json"
        if r.returncode != 0 or not os.path.exists(jf):
            print(f"ERR识别 {bid} p{p} :: {r.stderr[-150:]}", flush=True)
            mark_dead(lkey, "ocr-failed")
            err += 1
            continue
        data = json.load(open(jf, encoding="utf-8"))
        groups_raw = data.get("contents", []) or []
        all_blocks = [b for pb in groups_raw for b in pb if b.get("text")]
        # 2026-07-19实测发现:空白衬页/馆藏章页、密排多栏类书版式会让模型幻觉出重复垃圾
        # (如"State the the the..."),confidence明显偏低(0.25-0.49 vs 正常识别0.9+)。
        # 逐块过滤而非整页一刀切:部分清晰部分模糊的页面,保留清晰部分,只丢垃圾块。
        def _ok(b):
            return (b.get("confidence") or 0) >= CONF_MIN and cjk_ratio(b.get("text")) >= CJK_MIN
        kept = [b.get("text") for b in all_blocks if _ok(b)]
        dropped = len(all_blocks) - len(kept)
        # 2026-07-28:ndlocr 的 contents 是【按版面分组】的嵌套结构(一组≈一个版面区块/栏),
        # 原来 for pb ... for b in pb 把它拍平、再 "\n".join,等于把国会图书馆那套版面分析
        # 的成果整个扔掉——只剩一片没有边界的文字。
        # 代价直接落在下游:灌库按 CHUNK=700 硬切,一条完整方证
        # (「太陽中風，陽浮而陰弱…桂枝湯主之。桂枝三兩去皮 芍藥三兩」)会被从中间劈成两块
        # 分进互不相干的 chunk,检索永远拿不到完整的一条。
        # 改法只保留边界、不改落点:组内仍用 \n,组【之间】用空行。纯文本读者看不出差别,
        # 阅读器 fulltext.js 照旧;但灌库可以优先在空行处断句,按版面边界切而不是按字数切。
        # 不额外写 .json/.md——每页多一次 PUT 会让 R2 写入翻倍,那是 Class A 账单,
        # 用一个换行符换回结构,不值得再花那笔钱。
        blocks = []
        for pb in groups_raw:
            g = [b.get("text") for b in pb if b.get("text") and _ok(b)]
            if g:
                blocks.append("\n".join(g))
        text = "\n\n".join(blocks) if blocks else "\n".join(kept)
        if not text.strip():
            # 过滤完基本空了(整页低质量/真空白页)——标记为空,不存半页垃圾冒充"识别成功"
            s3.put_object(Bucket=BUCKET, Key=txtkey, Body=b"", ContentType="text/plain; charset=utf-8")
            low_conf += 1
            ledger.add(lkey)
            clear_dead(lkey)
            if dropped:
                print(f"低质量跳过 {bid} p{p}:{dropped}个块全部低于置信度{CONF_MIN},存空文件", flush=True)
        else:
            # 页级质量闸(2026-07-28 补接)。块级闸只能看单块:每块自己置信度够高、CJK 够多,
            # 拼成一页仍可能是同一句刷屏、整栏复读、或版面分析把同一区域读了两遍——
            # 这些只有拿整页文本才看得出来,单块视角天然看不见。
            # 在此之前这条线没有这一层,而 NDL 已是主力 OCR(cron 30 */6):产出直落 _ocr/,
            # 再被 ocr_to_clean_text.py 桥进 RAG,等于平台引用的"古籍原文"里可能混着没过闸的垃圾。
            # 判退的不进燃料池,原样落 _ocr_rejected/ 留证(与 ocr.py / ocr_xf.py 同一落点约定),
            # 供换引擎重跑时比对。注意这不是新增 R2 写入:判退页原本也要 PUT 一次,只是换了前缀。
            qa = ocr_quality.analyze(text)
            if qa["label"] == "reject":
                try:
                    s3.put_object(Bucket=BUCKET, Key=f"_ocr_rejected/{bid}/page_{pstr}.txt",
                                  Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
                except Exception:
                    pass
                # 判退也要记账。不记的话每 6 小时 cron 会把同一页重拉重跑——同一张图喂同一个
                # 引擎不会产出不同结果,那是刚修完的死页空转的同一个坑。
                ledger.add(lkey)
                clear_dead(lkey)
                rejected += 1
                # reasons 只含比例和 n,不含原文片段,可以安全印进 public 仓的 run log。
                print(f"质量闸判退 {bid} p{p}:{'/'.join(qa['reasons'])}", flush=True)
                continue
            s3.put_object(Bucket=BUCKET, Key=txtkey, Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
            done += 1
            ledger.add(lkey)
            clear_dead(lkey)
            if dropped:
                print(f"部分过滤 {bid} p{p}:丢{dropped}个低置信度块,保留{len(kept)}个", flush=True)
            if done % 20 == 0:
                print(f"进度 {done}/{len(mine)}", flush=True)
    except Exception as e:
        print(f"ERR处理异常 {bid} p{p} :: {str(e)[:100]}", flush=True)
        mark_dead(lkey, "proc-error")
        err += 1
    finally:
        for f in (img_path, f"{TMP}/page_{pstr}.json"):
            try:
                os.remove(f)
            except Exception:
                pass

json.dump(sorted(ledger), open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
# 死信无条件落盘,哪怕是空 dict:workflow 的 cache save 列了这个路径,
# 文件不存在会让整份死信记录静默丢掉,下一轮又从零开始把死页全部重拉一遍。
json.dump(dead, open(DEADLETTER, "w", encoding="utf-8"), ensure_ascii=False, sort_keys=True)

dead_pages = len(dead)
_dead_by_book = Counter(k.split(":")[0] for k in dead)
dead_books = len(_dead_by_book)
top_dead = _dead_by_book.most_common(5)

s3.put_object(Bucket=BUCKET, Key=f"_ledger/ocr_ndl_{SHARD}.json",
              Body=json.dumps({"shard": SHARD, "total": len(mine), "done": done, "skip": skip,
                               "err": err, "low_conf": low_conf, "rejected": rejected,
                               "cooled": cooled,
                               "dead_pages": dead_pages, "dead_books": dead_books,
                               "dead_new": _dead_stat["new"]}).encode())
d1_report_run("done", len(mine), done, skip, err, low_conf, rej_n=rejected)
d1_report_dead(dead_pages, dead_books, _dead_stat["new"], cooled, top_dead)
print(f"=== shard {SHARD} 完成 done={done} skip={skip} err={err} low_conf={low_conf} "
      f"质量闸判退={rejected} / {len(mine)} ===", flush=True)
print(f"=== 死信 {dead_pages}页/{dead_books}本 (本轮新增{_dead_stat['new']}·冷却跳过{cooled}) ===", flush=True)
if top_dead:
    # 死页最集中的几本 = 上游 123 缺页最严重的几本,给 CTO 判断上游窟窿规模用。
    print("    死页最多: " + "  ".join(f"{b}×{c}" for b, c in top_dead), flush=True)
