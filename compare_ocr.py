# -*- coding: utf-8 -*-
# OCR引擎对比实验(平台CTO 2026-07-25,创始人问"NDL竖排是否更好/双引擎融合是否更好")
# 同样的真实古籍页 -> NDLOCR-Lite(竖排专训,逐块置信度) + 讯飞HunyuanOCR(VLM,语感强)
# 各跑一遍 -> 逐页并排 + 一致率指标 -> markdown报告(stdout + artifact)。
# 零生产影响:只读D1/123,不写R2、不写D1、不碰 _ocr/ 落点。
import os, io, json, re, sys, time, base64, difflib, subprocess
from collections import Counter
import requests

CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
PAN_CID = os.environ["PAN_CLIENT_ID"]; PAN_SEC = os.environ["PAN_CLIENT_SECRET"]
XF_BASE = os.environ.get("XF_BASE", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_MODEL", "xophunyuanocr")
BOOKS_OVERRIDE = os.environ.get("BOOKS", "").strip()          # 逗号分隔book_id,空=自动选
N_PAGES = int(os.environ.get("N_PAGES", "8"))                 # 每本抽几页
PROMPT = "识别图中所有文字，只输出文字"

def parse_keys(raw):
    raw = (raw or "").strip()
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [k.strip() for k in v if ":" in str(k)]
    except Exception:
        pass
    return [p.strip() for p in re.split(r"[\s,]+", raw) if ":" in p]

XF_KEY = (parse_keys(os.environ.get("XF_KEYS", "")) or [None])[0]
if not XF_KEY:
    raise SystemExit("no XF_KEYS")

_CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ゟ゠-ヿ]")
def cjk_ratio(s):
    t = re.sub(r"\s", "", s or "")
    return (len(_CJK_RE.findall(t)) / len(t)) if t else 0.0

def norm(s):
    return re.sub(r"[\s　。、,,..;;::!!??「」『』()()〔〕【】·*#\-—]", "", s or "")

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
        raise SystemExit("123 token fail: " + r.text[:200])
    return _tok["v"]

def fetch_page_from_123(pan_dir_id, page_str):
    h = {"Platform": "open_platform", "Authorization": "Bearer " + pan_token()}
    filename = f"page_{page_str}.webp"
    last_id, file_id = 0, None
    for _ in range(20):
        r = requests.get(f"{PAN}/api/v2/file/list",
                         params={"parentFileId": pan_dir_id, "limit": 100, "lastFileId": last_id},
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

def d1_query(sql, params=None):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACC}/d1/database/{D1_DB}/query"
    r = requests.post(url, headers={"Authorization": "Bearer " + D1_TOK},
                      json={"sql": sql, "params": params or []}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"D1 fail: {str(j.get('errors',''))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []

# ── 选书:自动=候选池里页数适中的前2本(确定性,可BOOKS覆盖) ──
if BOOKS_OVERRIDE:
    ids = [b.strip() for b in BOOKS_OVERRIDE.split(",") if b.strip()]
    ph = ",".join(["?"] * len(ids))
    rows = d1_query(f"SELECT book_id, book_title, page_count, pan_dir_id FROM books_assets_v2 "
                    f"WHERE book_id IN ({ph}) AND pan_dir_id IS NOT NULL", ids)
else:
    rows = d1_query(
        "SELECT book_id, book_title, page_count, pan_dir_id FROM books_assets_v2 "
        "WHERE frontend_visible=1 AND upload_status='done' AND page_count BETWEEN 30 AND 300 "
        "AND webp_prefix LIKE 'book/%' AND book_title NOT LIKE '%宮內廳%' "
        "AND pan_dir_id IS NOT NULL ORDER BY book_id LIMIT 2")
if not rows:
    raise SystemExit("no candidate books")
print(f"对比书目 {len(rows)} 本: " + " | ".join(f"{r['book_id']}({r['page_count']}p)" for r in rows), flush=True)

sess = requests.Session()
def xf_ocr(b64):
    for i in range(3):
        try:
            r = sess.post(XF_BASE + "/chat/completions",
                headers={"Authorization": "Bearer " + XF_KEY, "Content-Type": "application/json"},
                json={"model": XF_MODEL, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b64}}]}]},
                timeout=120, verify=False)
            if r.status_code == 200:
                return (r.json().get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            print(f"  xf http {r.status_code} retry{i}", flush=True)
        except Exception as e:
            print(f"  xf err {str(e)[:80]} retry{i}", flush=True)
        time.sleep(3)
    return None

OCR_SRC = "ndlocr-lite/src"
TMP = "/tmp/cmp_work"
os.makedirs(TMP, exist_ok=True)
CONF_MIN, CJK_MIN = 0.6, 0.3   # 与生产ocr_ndl.py同一套过滤,保证对比的是"生产会留下的文本"

def ndl_ocr(img_path, pstr):
    r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", img_path, "--output", TMP, "--json-only"],
                       cwd=OCR_SRC, capture_output=True, text=True, timeout=180)
    jf = f"{TMP}/page_{pstr}.json"
    if r.returncode != 0 or not os.path.exists(jf):
        return None, f"ndl fail: {r.stderr[-120:]}"
    data = json.load(open(jf, encoding="utf-8"))
    os.remove(jf)
    all_blocks = [b for pb in data.get("contents", []) for b in pb if b.get("text")]
    kept = [b.get("text") for b in all_blocks
            if (b.get("confidence") or 0) >= CONF_MIN and cjk_ratio(b.get("text")) >= CJK_MIN]
    return "\n".join(kept), f"blocks={len(all_blocks)} kept={len(kept)}"

results = []
for row in rows:
    bid, pc, pdid, title = row["book_id"], int(row["page_count"]), row["pan_dir_id"], row.get("book_title", "")
    lo, hi = max(2, int(pc * 0.2)), max(3, int(pc * 0.8))
    step = max(1, (hi - lo) // max(1, N_PAGES - 1))
    sample = sorted(set(range(lo, hi + 1, step)))[:N_PAGES]
    print(f"[{bid}] {title[:30]} 抽页 {sample}", flush=True)
    for p in sample:
        pstr = str(p).zfill(4)
        content = fetch_page_from_123(pdid, pstr)
        if not content:
            print(f"  p{p} 123拉图失败,跳过", flush=True)
            continue
        img_path = f"{TMP}/page_{pstr}.webp"
        open(img_path, "wb").write(content)
        ndl_t, ndl_note = ndl_ocr(img_path, pstr)
        xf_t = xf_ocr(base64.b64encode(content).decode())
        os.remove(img_path)
        if ndl_t is None and xf_t is None:
            print(f"  p{p} 双引擎都失败", flush=True)
            continue
        a, b = norm(ndl_t or ""), norm(xf_t or "")
        seq_sim = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        ca, cb = Counter(a), Counter(b)
        overlap = sum((ca & cb).values()) / max(len(a), len(b)) if (a or b) else 0.0
        results.append({"book": bid, "title": title, "page": p,
                        "ndl_chars": len(a), "xf_chars": len(b),
                        "ndl_cjk": round(cjk_ratio(ndl_t or ""), 2), "xf_cjk": round(cjk_ratio(xf_t or ""), 2),
                        "seq_sim": round(seq_sim, 3), "char_overlap": round(overlap, 3),
                        "ndl_text": (ndl_t or "")[:400], "xf_text": (xf_t or "")[:400], "ndl_note": ndl_note})
        print(f"  p{p} ndl={len(a)}字 xf={len(b)}字 字符重合={overlap:.0%} 顺序相似={seq_sim:.0%}", flush=True)

# ── 报告 ──
L = ["# NDLOCR-Lite vs 讯飞HunyuanOCR 同页对比报告", "",
     f"页数: {len(results)}  过滤口径: 与生产一致(conf>={CONF_MIN}, cjk>={CJK_MIN})", "",
     "| 书 | 页 | NDL字数 | XF字数 | 字符重合率 | 顺序相似度 | NDL_CJK | XF_CJK |",
     "|---|---|---|---|---|---|---|---|"]
for r in results:
    L.append(f"| {r['book']} | {r['page']} | {r['ndl_chars']} | {r['xf_chars']} | "
             f"{r['char_overlap']:.0%} | {r['seq_sim']:.0%} | {r['ndl_cjk']} | {r['xf_cjk']} |")
if results:
    avg_o = sum(r["char_overlap"] for r in results) / len(results)
    avg_s = sum(r["seq_sim"] for r in results) / len(results)
    L += ["", f"**均值: 字符重合率 {avg_o:.0%} · 顺序相似度 {avg_s:.0%}**", "",
          "> 字符重合率=两引擎认出的字集一致程度(不看顺序);高=互证可信,低=至少一家在错。", ""]
for r in results:
    L += [f"## {r['book']} p{r['page']}({r['title'][:24]})",
          f"重合{r['char_overlap']:.0%} / {r['ndl_note']}", "",
          "**NDL(竖排专训+置信度过滤):**", "```", r["ndl_text"] or "(空)", "```",
          "**讯飞HunyuanOCR(VLM):**", "```", r["xf_text"] or "(空)", "```", ""]
rep = "\n".join(L)
open("compare_report.md", "w", encoding="utf-8").write(rep)
print("\n" + "=" * 60 + "\n" + rep, flush=True)
print(f"=== COMPARE DONE pages={len(results)} ===", flush=True)
