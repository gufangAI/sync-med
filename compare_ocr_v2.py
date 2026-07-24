# -*- coding: utf-8 -*-
# OCR五引擎赛马 v2(平台CTO 2026-07-25,创始人指令:前端OCR乱码=列序反+繁转简+馆章混入,别死守讯飞,试新引擎)
# 引擎: ndl(NDLOCR-Lite) / xf(讯飞HunyuanOCR) / glm(智谱GLM-4V-Flash免费) / gemini(Gemini-2.5-Flash免费档) / ppocr(PP-OCRv5)
# 考题必含创始人点名的 zi021-0001-01(素問,前端乱码铁证页)。
# 三条硬规则进提示词/排序: ①列序右→左 ②保持繁体禁简化 ③剔藏书章/水印。
# 零生产影响:只读D1/123,不写R2/_ocr/。产 compare_report_v2.md(stdout+artifact)。
import os, io, re, json, sys, time, base64, difflib, subprocess
from collections import Counter
import requests

CF_ACC = os.environ["CF_ACCOUNT_ID"]; D1_DB = os.environ["D1_DATABASE_ID"]; D1_TOK = os.environ["D1_API_TOKEN"]
PAN_CID = os.environ["PAN_CLIENT_ID"]; PAN_SEC = os.environ["PAN_CLIENT_SECRET"]
XF_BASE = os.environ.get("XF_BASE", "https://maas-api.cn-huabei-1.xf-yun.com/v2")
XF_MODEL = os.environ.get("XF_MODEL", "xophunyuanocr")
ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
BOOKS_OVERRIDE = os.environ.get("BOOKS", "zi021-0001-01,01-0022573,01-0022566").strip()
N_PAGES = int(os.environ.get("N_PAGES", "6"))
ENGINES = [e.strip() for e in os.environ.get("ENGINES", "ndl,xf,glm,gemini,ppocr").split(",") if e.strip()]

PROMPT_V2 = ("这是竖排繁体古籍书页,阅读顺序:列从右到左,每列从上到下。"
             "严格按此顺序输出全部正文文字;保持繁体原字形,禁止转为简体;"
             "跳过藏书印、馆藏章、水印文字(如「国立公文書館」「National Archives of Japan」);"
             "只输出正文,不要任何解释。")

WM_RE = re.compile(r"(国立公文書館|國立公文書館|National\s*Archives\s*of\s*Japan|国立国会図書館|國立國會圖書館|NationalDietLibrary)", re.I)
SIMP_MARKERS = set("问门陈风华检险开关观兴举义号书画际尔东乐")   # 粗测简化字泄漏(近似指标,报告注明)

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

_CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ゟ゠-ヿ]")
def cjk_ratio(s):
    t = re.sub(r"\s", "", s or "")
    return (len(_CJK_RE.findall(t)) / len(t)) if t else 0.0

def norm(s):
    return re.sub(r"[\s　。、,,..;;::!!??「」『』()()〔〕【】·*#\-—]", "", s or "")

def strip_wm(s):
    hits = len(WM_RE.findall(s or ""))
    return WM_RE.sub("", s or ""), hits

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
    for _ in range(30):
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

def to_jpeg_b64(webp_bytes):
    from PIL import Image
    im = Image.open(io.BytesIO(webp_bytes)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()

# ── 引擎实现(每个都 try/except 自兜底,单引擎挂不掀桌) ──
OCR_SRC = "ndlocr-lite/src"
TMP = "/tmp/race_work"
os.makedirs(TMP, exist_ok=True)
CONF_MIN, CJK_MIN = 0.6, 0.3

def eng_ndl(img_path, pstr, b64w, b64j):
    r = subprocess.run([sys.executable, "ocr.py", "--sourceimg", img_path, "--output", TMP, "--json-only"],
                       cwd=OCR_SRC, capture_output=True, text=True, timeout=240)
    jf = f"{TMP}/page_{pstr}.json"
    if r.returncode != 0 or not os.path.exists(jf):
        return None
    data = json.load(open(jf, encoding="utf-8"))
    os.remove(jf)
    blocks = [b for pb in data.get("contents", []) for b in pb if b.get("text")]
    kept = [b["text"] for b in blocks if (b.get("confidence") or 0) >= CONF_MIN and cjk_ratio(b["text"]) >= CJK_MIN]
    return "\n".join(kept)

def eng_xf(img_path, pstr, b64w, b64j):
    if not XF_KEY:
        return None
    for _ in range(3):
        try:
            r = requests.post(XF_BASE + "/chat/completions",
                headers={"Authorization": "Bearer " + XF_KEY, "Content-Type": "application/json"},
                json={"model": XF_MODEL, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT_V2},
                    {"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b64w}}]}]},
                timeout=120, verify=False)
            if r.status_code == 200:
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            pass
        time.sleep(3)
    return None

def eng_glm(img_path, pstr, b64w, b64j):
    if not ZHIPU_KEY:
        return None
    for _ in range(3):
        try:
            r = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={"Authorization": "Bearer " + ZHIPU_KEY, "Content-Type": "application/json"},
                json={"model": "glm-4v-flash", "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64j}},
                    {"type": "text", "text": PROMPT_V2}]}]},
                timeout=120)
            if r.status_code == 200:
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            print(f"    glm http {r.status_code}: {r.text[:120]}", flush=True)
        except Exception as e:
            print(f"    glm err {str(e)[:80]}", flush=True)
        time.sleep(3)
    return None

def eng_gemini(img_path, pstr, b64w, b64j):
    if not GEMINI_KEY:
        return None
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64j}},
                    {"text": PROMPT_V2}]}]},
                timeout=120)
            if r.status_code == 200:
                parts = (((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                return "".join(p.get("text", "") for p in parts).strip()
            if r.status_code == 429:
                print("    gemini 429, backoff 30s", flush=True)
                time.sleep(30)
                continue
            print(f"    gemini http {r.status_code}: {r.text[:120]}", flush=True)
        except Exception as e:
            print(f"    gemini err {str(e)[:80]}", flush=True)
        time.sleep(5)
    return None

_pp = {"ocr": None, "dead": False}
def eng_ppocr(img_path, pstr, b64w, b64j):
    if _pp["dead"]:
        return None
    try:
        if _pp["ocr"] is None:
            from paddleocr import PaddleOCR
            _pp["ocr"] = PaddleOCR(lang="ch", use_doc_orientation_classify=False, use_doc_unwarping=False)
        from PIL import Image
        jpath = f"{TMP}/pp_{pstr}.jpg"
        Image.open(img_path).convert("RGB").save(jpath, "JPEG", quality=92)
        out = _pp["ocr"].predict(jpath)
        os.remove(jpath)
        if not out:
            return ""
        res = out[0]
        texts = res.get("rec_texts") if isinstance(res, dict) else getattr(res, "rec_texts", None)
        polys = res.get("rec_polys") if isinstance(res, dict) else getattr(res, "rec_polys", None)
        if texts is None:
            return ""
        items = []
        W = 2000.0
        for i, t in enumerate(texts):
            try:
                pts = polys[i]
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                items.append((sum(xs) / len(xs), min(ys), t))
                W = max(W, max(xs))
            except Exception:
                items.append((0, i, t))
        # 竖排右→左列序:x中心按页宽3%分箱,箱从右到左,箱内自上而下
        binw = max(30.0, W * 0.03)
        items.sort(key=lambda it: (-round(it[0] / binw), it[1]))
        return "\n".join(t for _, _, t in items)
    except Exception as e:
        print(f"    ppocr dead: {str(e)[:150]}", flush=True)
        _pp["dead"] = True
        return None

ENGINE_FN = {"ndl": eng_ndl, "xf": eng_xf, "glm": eng_glm, "gemini": eng_gemini, "ppocr": eng_ppocr}

# ── 选书取页 ──
ids = [b.strip() for b in BOOKS_OVERRIDE.split(",") if b.strip()]
ph = ",".join(["?"] * len(ids))
rows = d1_query(f"SELECT book_id, book_title, page_count, pan_dir_id FROM books_assets_v2 "
                f"WHERE book_id IN ({ph}) AND pan_dir_id IS NOT NULL", ids)
if not rows:
    raise SystemExit("no candidate books (pan_dir_id missing?)")
print("赛马书目: " + " | ".join(f"{r['book_id']}({r['page_count']}p)" for r in rows), flush=True)

results = []   # {book,page,eng:{text,chars,wm,simp}}
for row in rows:
    bid, pc, pdid, title = row["book_id"], int(row["page_count"]), row["pan_dir_id"], row.get("book_title", "")
    if bid == "zi021-0001-01":
        sample = [3, 4, 5, 6, 10, 20][:N_PAGES]   # 创始人点名铁证页:第3页起
    else:
        lo, hi = max(2, int(pc * 0.2)), max(3, int(pc * 0.8))
        step = max(1, (hi - lo) // max(1, N_PAGES - 1))
        sample = sorted(set(range(lo, hi + 1, step)))[:N_PAGES]
    print(f"[{bid}] {title[:26]} 抽页 {sample}", flush=True)
    for p in sample:
        pstr = str(p).zfill(4)
        content = fetch_page_from_123(pdid, pstr)
        if not content:
            print(f"  p{p} 123拉图失败,跳过", flush=True)
            continue
        img_path = f"{TMP}/page_{pstr}.webp"
        open(img_path, "wb").write(content)
        b64w = base64.b64encode(content).decode()
        try:
            b64j = to_jpeg_b64(content)
        except Exception:
            b64j = b64w
        page_res = {"book": bid, "page": p, "eng": {}}
        for e in ENGINES:
            t0 = time.time()
            raw = ENGINE_FN[e](img_path, pstr, b64w, b64j)
            if raw is None:
                page_res["eng"][e] = None
                print(f"  p{p} {e}: 失败/不可用", flush=True)
                continue
            txt, wm = strip_wm(raw)
            n = norm(txt)
            simp = sum(1 for c in n if c in SIMP_MARKERS)
            page_res["eng"][e] = {"text": txt, "n": n, "chars": len(n), "wm": wm, "simp": simp}
            print(f"  p{p} {e}: {len(n)}字 wm={wm} simp={simp} {time.time()-t0:.0f}s", flush=True)
            if e == "gemini":
                time.sleep(6)   # 免费档RPM限速
        os.remove(img_path)
        results.append(page_res)

# ── 计分:以NDL列序为序基准(它版面模型按右→左阅读序出块);字符重合看识别一致性 ──
def sim(a, b):
    if not a or not b:
        return 0.0, 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ca, cb = Counter(a), Counter(b)
    ov = sum((ca & cb).values()) / max(len(a), len(b))
    return ov, seq

L = ["# OCR五引擎赛马报告 v2(考题含创始人点名素問zi021-0001-01)", "",
     f"引擎: {','.join(ENGINES)}  提示词铁则: 列序右→左·保持繁体·剔馆章", "",
     "| 书 | 页 | " + " | ".join(f"{e}字数" for e in ENGINES) + " | " + " | ".join(f"{e}vs NDL重合/序" for e in ENGINES if e != "ndl") + " |",
     "|---|---|" + "---|" * (len(ENGINES) + (len(ENGINES) - 1)) ]
agg = {e: {"chars": [], "ov": [], "seq": [], "wm": 0, "simp": 0, "fail": 0} for e in ENGINES}
for pr in results:
    ndl = pr["eng"].get("ndl")
    ndl_n = ndl["n"] if ndl else ""
    cells1, cells2 = [], []
    for e in ENGINES:
        d = pr["eng"].get(e)
        if d is None:
            cells1.append("×")
            agg[e]["fail"] += 1
        else:
            cells1.append(str(d["chars"]))
            agg[e]["chars"].append(d["chars"])
            agg[e]["wm"] += d["wm"]; agg[e]["simp"] += d["simp"]
        if e != "ndl":
            if d and len(ndl_n) >= 100:
                ov, seq = sim(ndl_n, d["n"])
                cells2.append(f"{ov:.0%}/{seq:.0%}")
                agg[e]["ov"].append(ov); agg[e]["seq"].append(seq)
            else:
                cells2.append("-")
    L.append(f"| {pr['book']} | {pr['page']} | " + " | ".join(cells1) + " | " + " | ".join(cells2) + " |")

L += ["", "## 引擎总评(均值)", "", "| 引擎 | 均字数 | vs NDL字符重合 | vs NDL列序相似 | 馆章混入次数 | 简化字泄漏(粗测) | 失败页 |", "|---|---|---|---|---|---|---|"]
for e in ENGINES:
    a = agg[e]
    mc = sum(a["chars"]) / len(a["chars"]) if a["chars"] else 0
    mo = sum(a["ov"]) / len(a["ov"]) if a["ov"] else 0
    ms = sum(a["seq"]) / len(a["seq"]) if a["seq"] else 0
    L.append(f"| {e} | {mc:.0f} | {mo:.0%} | {ms:.0%} | {a['wm']} | {a['simp']} | {a['fail']} |")
L += ["", "> 列序相似=与NDL(版面模型右→左出块)顺序一致度,是「排列符合古书规则」的代理指标;",
      "> 简化字泄漏为近似粗测(标记字集有限);单引擎失败不影响其他引擎成绩。", ""]

L += ["## 创始人铁证页样张(zi021-0001-01 p3,各引擎前240字)", ""]
for pr in results:
    if pr["book"] == "zi021-0001-01" and pr["page"] == 3:
        for e in ENGINES:
            d = pr["eng"].get(e)
            L += [f"**{e}:**", "```", (d["text"][:240] if d else "(失败/不可用)"), "```", ""]
rep = "\n".join(L)
open("compare_report_v2.md", "w", encoding="utf-8").write(rep)
# 全量原始输出落盘(供指挥部拿真值文本逐字算真准确率——重合率只是互证,不冒充准确率)
dump = [{"book": pr["book"], "page": pr["page"],
         "eng": {e: (d["text"] if d else None) for e, d in pr["eng"].items()}} for pr in results]
open("raw_outputs.json", "w", encoding="utf-8").write(json.dumps(dump, ensure_ascii=False))
print("\n" + "=" * 60 + "\n" + rep, flush=True)
print(f"=== RACE DONE pages={len(results)} engines={len(ENGINES)} ===", flush=True)
