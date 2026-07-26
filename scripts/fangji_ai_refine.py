# -*- coding: utf-8 -*-
"""
Formula AI refine worker (runs on GitHub Actions).

Why: the regex miner grounds every field in the source text, but grounding is not
semantics -- fragments like "with four sheng of water" DO appear in the source yet
are decoction steps, not herbs. A model is needed to tell herbs from procedure.

Safety rails (non-negotiable -- an earlier LLM batch produced 66% ungrounded rows):
  * the model only JUDGES and SPLITS, it never writes content
  * every returned herb / name must appear verbatim in the quote, else the row is dropped
  * rows that fail verification are left untouched (ai_ok = 0), never overwritten with guesses

Idempotent: only rows with ai_ok IS NULL are processed, so re-runs resume.

Usage: python fangji_ai_refine.py --supplier modelscope --shard 0 --total 7 --limit 4000
"""
import argparse, json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GATE = "https://gufangai.com/api/gateway/chat"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
D1API = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (ACC, DB)
_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
OP = urllib.request.build_opener(urllib.request.ProxyHandler(
    {"http": _proxy, "https": _proxy} if _proxy else {}))

def d1(sql, tries=6):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(D1API, json.dumps({"sql": sql}).encode(),
                {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
            r = json.loads(OP.open(req, timeout=180).read())
            if not r.get("success"):
                raise RuntimeError(json.dumps(r.get("errors"))[:300])
            return r["result"][0].get("results", [])
        except Exception as e:
            last = e; time.sleep(2.0 * (a + 1))
    raise RuntimeError("d1 failed after retries: %s" % str(last)[:180])

SYS = """\u4f60\u662f\u4e2d\u533b\u53e4\u7c4d\u65b9\u5242\u6821\u5bf9\u5458\u3002\u4e0b\u9762\u7ed9\u4f60\u4e00\u6bb5\u53e4\u7c4d\u539f\u6587,\u4ee5\u53ca\u7a0b\u5e8f\u81ea\u52a8\u62bd\u53d6\u7684\u65b9\u540d\u548c\u7ec4\u6210(\u7ec4\u6210\u5f88\u53ef\u80fd\u62bd\u9519)\u3002
\u8bf7\u4ee5\u3010\u539f\u6587\u3011\u4e3a\u51c6,\u91cd\u65b0\u62bd\u53d6\u6b63\u786e\u7684\u65b9\u540d\u548c\u836f\u6750\u7ec4\u6210\u3002

\u94c1\u5f8b(\u8fdd\u53cd\u5373\u5224\u5e9f):
1. \u4f60\u53ea\u505a\u3010\u62bd\u53d6\u3011,\u7edd\u4e0d\u8bb8\u5199\u51fa\u539f\u6587\u91cc\u6ca1\u6709\u7684\u5b57\u3002\u65b9\u540d\u548c\u6bcf\u5473\u836f\u6750\u5fc5\u987b\u662f\u539f\u6587\u91cc\u3010\u9010\u5b57\u8fde\u7eed\u51fa\u73b0\u3011\u7684\u7247\u6bb5,
   \u4e0d\u8bb8\u6539\u5199\u3001\u8865\u5168\u3001\u8f6c\u7e41\u7b80\u3002\u539f\u6587\u5199"\u7518\u8349\u7099\u4e8c\u5169"\u5c31\u5199"\u7518\u8349",\u4e0d\u8bb8\u5199\u6210"\u7099\u7518\u8349"\u3002
2. \u53ea\u8981\u836f\u6750\u540d,\u4e0d\u8981\u714e\u670d\u6cd5\u3002\u4ee5\u4e0b\u90fd\u4e0d\u662f\u836f\u6750,\u4e00\u5f8b\u5254\u9664:
   \u4ee5\u6c34\u56db\u5347 / \u716e\u53d6\u4e09\u5347 / \u53bb\u6ed3 / \u6eab\u670d\u4e00\u5347 / \u6bcf\u670d\u4e09\u9322 / \u4e0a\u70ba\u672b / \u96a8\u8b49\u500d\u4e00\u5206 / \u4e8c\u5473 / \u7b49\u5206 / \u5404\u4e09\u5169
3. \u7c98\u8fde\u7684\u8981\u5207\u5f00:"\u828d\u85e5\u4e09\u5169\u7518\u8349\u4e8c\u5169" \u2192 ["\u828d\u85e5","\u7518\u8349"]\u3002
4. ok \u5224\u5b9a:
   - true  = \u539f\u6587\u91cc\u786e\u5b9e\u6709\u4e00\u9996\u65b9(\u6709\u65b9\u540d,\u4e14\u80fd\u62bd\u51fa\u22652\u5473\u771f\u836f\u6750)
   - false = \u53ea\u662f\u4e3b\u6cbb\u53d9\u8ff0/\u52a0\u51cf\u8bf4\u660e/\u714e\u670d\u6cd5,\u6216\u65b9\u540d\u5176\u5b9e\u662f\u53e5\u5b50\u7247\u6bb5(\u5982"\u65b9\u7528\u751f\u77f3\u818f"\u3001"\u91ab\u6bcf\u767c\u6563")

\u53ea\u8f93\u51fa JSON,\u65e0\u4efb\u4f55\u89e3\u91ca:
{"ok": true/false, "name": "\u65b9\u540d", "herbs": ["\u836f\u67501","\u836f\u67502"]}"""

def ask(item, supplier):
    body = json.dumps({
        "supplier": supplier, "fallback": False, "json": True,
        "temperature": 0.1, "max_tokens": 500,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content":
                      "\u65b9\u540d:%s\n\u7ec4\u6210:%s\n\u53e4\u7c4d\u539f\u6587:%s" % (item["ns"], item["comp"], (item["q"] or "")[:600])}],
    }).encode()
    req = urllib.request.Request(GATE, body,
        {"Content-Type": "application/json", "User-Agent": UA, "Referer": "https://gufangai.com/"})
    r = json.loads(OP.open(req, timeout=90).read())
    txt = r.get("content") or r.get("text") or ((r.get("data") or {}).get("content")) or ""
    m = re.search(r"\{[\s\S]*\}", str(txt))
    return json.loads(m.group(0)) if m else None

PAREN = re.compile(r"[\uff08(].*?[\uff09)]")
def verify(res, item):
    """Local re-check: every character the model returned must exist in the source quote."""
    if not res: return None
    q = item["q"] or ""
    if not res.get("ok"):
        return {"ok": 0, "herbs": None}
    nm = str(res.get("name") or "").strip()
    if nm and nm not in q: return None          # hallucinated name -> drop the whole row
    herbs = []
    for h in (res.get("herbs") or []):
        hh = PAREN.sub("", str(h)).strip()
        if not hh or len(hh) > 12: continue
        if hh not in q: return None             # hallucinated herb -> drop the whole row
        if hh not in herbs: herbs.append(hh)
    if len(herbs) < 2: return {"ok": 0, "herbs": None}
    return {"ok": 1, "herbs": "\u3001".join(herbs[:24])}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--total", type=int, default=1)
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    rows = d1("SELECT formula_id AS id, COALESCE(name_s,name) AS ns, composition AS comp, quote AS q "
              "FROM sue_formulas WHERE ai_ok IS NULL AND COALESCE(is_formula,1)=1 AND dup_of IS NULL "
              "AND formula_id %% %d = %d ORDER BY formula_id LIMIT %d" % (a.total, a.shard, a.limit))
    print("[%s shard %d/%d] pending rows: %d" % (a.supplier, a.shard, a.total, len(rows)), flush=True)
    if not rows: return

    done, drop, t0 = [], 0, time.time()
    def work(it):
        for attempt in range(2):
            try:
                return (it, verify(ask(it, a.supplier), it))
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        return (it, None)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (it, v) in enumerate(ex.map(work, rows)):
            if v is None: drop += 1; continue
            done.append((it["id"], v))
            if (i + 1) % 200 == 0:
                print("   %d/%d  kept=%d dropped=%d  %.0fs" %
                      (i + 1, len(rows), len(done), drop, time.time() - t0), flush=True)

    real = sum(1 for _, v in done if v["ok"] == 1)
    print("[%s] verified=%d (formula=%d, non-formula=%d) dropped=%d  %.0fs" %
          (a.supplier, len(done), real, len(done) - real, drop, time.time() - t0), flush=True)

    for i in range(0, len(done), 50):
        chunk = done[i:i+50]
        ok_cases = " ".join("WHEN %d THEN %d" % (rid, v["ok"]) for rid, v in chunk)
        hb_cases = " ".join("WHEN %d THEN %s" % (
            rid, ("'" + v["herbs"].replace("'", "''") + "'") if v["herbs"] else "NULL") for rid, v in chunk)
        ids = ",".join(str(rid) for rid, _ in chunk)
        d1("UPDATE sue_formulas SET ai_ok = CASE formula_id %s END, "
           "ai_herbs = CASE formula_id %s END, ai_src = '%s' WHERE formula_id IN (%s)"
           % (ok_cases, hb_cases, a.supplier, ids))
    print("[%s] written back %d rows" % (a.supplier, len(done)), flush=True)

main()
