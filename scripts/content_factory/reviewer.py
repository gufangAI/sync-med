#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容工厂 · 独立复核器 —— 把 draft 升成 published(或按住不放)

立此因(2026-08-02 实测):内容工厂产出的 92 条**全部卡在 draft**,published 零条。
  原设计是「入库 draft,由人工审核后上线」—— 但根本没有审核人,
  等于我给这条产线打了个**永远解不开的死结**:跑得再多,前台一条也看不见。
  这正是创始人骂的「建垃圾」。

解法不是取消闸门,而是**把闸门做成能真运转的**:
  生成用一个模型,复核**必须换另一个模型**,两个模型独立同意才放行。
  这比原来的「单模型 + 关键词黑名单」严格 —— 黑名单只能抓字面,
  第二个模型能抓「字面没踩雷但内容是编的/是临床结论」。

判定三档:
  pass  → status='published'  前台可见
  hold  → status='held'       有疑点,留给人工;不再反复复核,不占额度
  (解析失败/模型不肯换)→ 保持 draft,下轮再来

铁律:
  · 只走内部免费池网关,严禁任何按量计费源
  · 复核模型必须 != 生成模型(拿不到不同模型就不放行,宁可留 draft)
  · 默认从严:模型只要说不准 / 出现剂量或疗效承诺 / 出处像编的 → hold
"""
import os, sys, json, time, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB      = os.environ.get("D1_DATABASE_ID", "")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GATEWAY    = os.environ.get("GW_URL", "https://gufangai.com/api/gateway/chat")
WORKERS    = int(os.environ.get("CF_WORKERS", "6"))
REVIEW_VER = "rv-v1-2026-08-02"

SYS_REVIEW = (
    "你是「古方 AI 星图」的**独立内容复核员**。给你的这条内容是另一个模型生成的,"
    "你的职责是**挑毛病**,不是背书。**默认不通过**,只有确实挑不出问题才放行。\n"
    "逐条检查,命中任何一条即判 hold:\n"
    "  ① 出现剂量、用法用量、煎服法(几克/几钱/水煎服/每日几次)—— 平台无诊疗资质,零容忍;\n"
    "  ② 出现疗效承诺或治愈表述(治愈率/根治/特效/包治/疗效显著);\n"
    "  ③ 写成对读者的医嘱(「你应该服用」「建议你服用」这类祈使句);\n"
    "  ④ 把体外/文献研究写成了**临床结论**(该用「体外研究提示」「文献报道」的地方写成了断言);\n"
    "  ⑤ **事实明显不对**:分子式/分子量/靶点/来源药材与该成分不符,或名称根本不是真实药材/成分;\n"
    "  ⑥ **出处像编的**:引了具体古籍书名或 PubChem/PubMed 编号但与内容对不上,或凭空生造;\n"
    "  ⑦ 内容空洞到没有信息量(通篇套话)。\n"
    # 2026-08-02 实测:20 条里 19 条被按住,大量理由是「引用不具体」「靶点未说明来源」——
    #   但生成侧已改成「宁可留空不许编造」。若这边继续把留空当缺陷,就是**两套提示词互相打架**,
    #   逼着模型去编 —— 那正是我们要防的事。所以必须显式写清:留空是诚实,不是毛病。
    "**以下情况明确 NOT hold(不许因此按住)**:\n"
    "  · `refs` / `targets` / `smiles` / `formula` **留空或为空数组** —— 这是诚实的「不知道」,\n"
    "    生成方被明确要求「宁可留空不许编造」。**只有写了却写错/写假才判 hold**。\n"
    "  · 内容篇幅偏短但准确无误。\n"
    "拿不准某个**已写出的事实**对不对 → hold。**放行一条错的,代价远大于按住一条对的。**\n"
    "只输出 JSON:{\"verdict\":\"pass\"或\"hold\",\"why\":\"20字以内理由\"}"
)


def d1(sql):
    if not (D1_TOKEN and CF_ACCOUNT and D1_DB):
        sys.exit("缺 D1_API_TOKEN / CF_ACCOUNT_ID / D1_DATABASE_ID")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
    req = urllib.request.Request(
        url, method="POST", data=json.dumps({"sql": sql}).encode("utf-8"),
        headers={"Authorization": "Bearer " + D1_TOKEN, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:250])
    return (j.get("result") or [{}])[0].get("results") or []


# 复核员候选,按"越强越靠前"排。实测(2026-08-02)免费池 12 家里只有这 4 家活着:
#   nvidia=nemotron-3-ultra-550b · openrouter=同款free · dashscope=qwen-plus · zhipu=glm-4-flash
# 生成侧目前落在 glm-4-flash,所以复核默认用 550b —— 比生成模型**更强**,而不是随便换一个。
# 注:先前试过用 source 参数区分链路,实测网关不认(两次都回同一个 glm-4-flash),
#     只有显式 supplier 才真能换,所以这里改成点名。
REVIEW_SUPPLIERS = ["nvidia", "dashscope", "openrouter", "zhipu"]


def ask(system, user, timeout=120, max_tokens=400, supplier=None):
    payload = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.1, "json": True, "source": "content_review",
    }
    if supplier:
        payload["supplier"] = supplier      # 点名供应商;失败仍按容错链兜(fallback 默认 true)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY, method="POST", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"})
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    txt = j.get("text") or ""
    if not txt and j.get("choices"):
        txt = (j["choices"][0].get("message") or {}).get("content", "")
    return txt, (j.get("model") or j.get("supplier") or "")


def parse_json(t):
    """只取第一个完整 JSON 对象 —— 与 herb_factory.parse_json 同一套判据(模型爱在 JSON 后面加话)。"""
    t = (t or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    a = t.find("{")
    if a < 0:
        raise ValueError("没有 JSON:" + t[:60])
    obj, _ = json.JSONDecoder().raw_decode(t, a)
    return obj


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


SPEC = {
    "herb": ("herb_compare", "herb_id",
             "name_cn,name_en,name_latin,props_json,constitution,traditional_use,"
             "indications,actives,tcm_compare,tcm_refs"),
    "biocomp": ("biocomp_entries", "entry_id",
                "name_cn,name_en,source_herb,formula,smiles,mol_weight,targets,mechanism,tcm_link,refs_json"),
}


def prefilter(target, row):
    """模型之前的**确定性硬闸**:不满足版块定义的,直接 hold,连复核调用都不用花。

    立此因(2026-08-02 实测):第一批放行的 12 条里有 6 条是「另加陀僧」「制天冬」「连皮绿豆」
      这种 —— **全部没有分子式**。「生物计算」版块收的是**分子层面的活性成分**,
      饮片名和 OCR 碎句不属于这一层。真成分必有分子式,这是版块定义本身,不是拍脑袋的过滤。
      两个模型都放行了它们,说明**语义判断不可能百分百可靠,该有确定性判据的地方就要有**。
    返回 None 表示放行到模型复核,返回字符串表示直接按住的理由。
    """
    if target == "biocomp":
        if not str(row.get("formula") or "").strip():
            return "无分子式:不是分子层面的活性成分(多为饮片名或 OCR 碎词)"
    return None


def review_one(row, gen_model, target="biocomp"):
    """返回 (verdict, why, review_model)。复核模型必须与生成模型不同,拿不到就判 skip 保持 draft。"""
    hard = prefilter(target, row)
    if hard:
        return "hold", hard, "prefilter"
    payload = json.dumps({k: v for k, v in row.items() if k not in ("id", "gen_model")},
                         ensure_ascii=False)[:3500]
    gen = (gen_model or "").strip()
    last_model = ""
    for sup in REVIEW_SUPPLIERS:
        try:
            txt, model = ask(SYS_REVIEW, "待复核内容:\n" + payload, supplier=sup)
        except Exception:
            continue                       # 这家挂了,换下一家
        last_model = model or last_model
        if model and gen and model.strip() == gen:
            continue                       # 容错链兜回了生成模型 —— 不算独立复核,换下一家
        try:
            o = parse_json(txt)
        except Exception:
            continue
        v = str(o.get("verdict", "")).lower().strip()
        why = str(o.get("why", ""))[:120]
        if v in ("pass", "hold"):
            return v, why, (model or sup)
        return "hold", "复核未给出明确结论", (model or sup)
    return "skip", f"没换到独立复核模型(生成={gen} 最后拿到={last_model})", last_model


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["herb", "biocomp"], required=True)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    tbl, pk, cols = SPEC[args.target]
    rows = d1(f"SELECT {pk} AS id, gen_model, {cols} FROM {tbl} "
              f"WHERE status='draft' ORDER BY updated_at ASC LIMIT {int(args.limit)}")
    print(f"[复核] {tbl} 待复核 draft {len(rows)} 条(本轮上限 {args.limit})\n", flush=True)
    if not rows:
        print("  没有待复核条目,收工")
        return

    passed = held = skipped = failed = 0
    now = int(time.time())

    def job(r):
        try:
            return r, review_one(r, r.get("gen_model") or "", args.target), None
        except Exception as e:
            return r, None, e

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fu in as_completed([ex.submit(job, r) for r in rows]):
            r, res, err = fu.result()
            name = r.get("name_cn") or r.get("name_en") or r["id"]
            if err is not None:
                failed += 1
                print(f"  ! {name} 复核出错: {type(err).__name__} {str(err)[:70]}", flush=True)
                continue
            verdict, why, rmodel = res
            if verdict == "skip":
                skipped += 1
                print(f"  – {name} 跳过({why[:50]}),保持 draft", flush=True)
                continue
            new_status = "published" if verdict == "pass" else "held"
            try:
                d1(f"UPDATE {tbl} SET status={q(new_status)}, review_model={q(rmodel)}, "
                   f"review_note={q(why)}, reviewed_at={now}, updated_at={now} WHERE {pk}={q(r['id'])}")
                if verdict == "pass":
                    passed += 1
                    print(f"  ✓ {name} 放行 → published(复核模型 {rmodel})", flush=True)
                else:
                    held += 1
                    print(f"  ⚠ {name} 按住 → held:{why[:40]}(复核模型 {rmodel})", flush=True)
            except Exception as e:
                failed += 1
                print(f"  ! {name} 写库失败: {str(e)[:80]}", flush=True)

    print(f"\n[完] 放行 {passed} · 按住 {held} · 跳过(没换到模型) {skipped} · 出错 {failed}")
    print(f"     放行的立刻在 /{'bencao' if args.target == 'herb' else 'shengwu'} 前台可见;"
          f"held 的留给人工,不会再自动复核。")


if __name__ == "__main__":
    main()
