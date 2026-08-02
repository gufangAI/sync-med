#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本草 / 生物计算 内容工厂 —— 让这两个版块的内容自己长

立此因(2026-08-02 创始人):「生物计算、本草,不可以派个 GitHub 的任务,或者派个 AI 去一直丰富内容吗」
  + 「我们的主要价值是在丰富功能,而不是在修修补补」。
实测原状:bencao.html 8KB / shengwu.html 9KB,条目**全部硬编码在 HTML 里**,
  加一味药要改代码重新部署 —— 所以两个多月一条没长过。

产线骨架照搬已跑通的「百家论道」:
  GitHub Actions cron → 免费池网关(零成本) → 合规硬闸 → 写 D1 → 前台读库

合规硬闸(07_合规文档,零容忍):
  生成结果里出现 剂量/煎服/疗效承诺/诊疗祈使句 → **整条丢弃并计数**,绝不入库。
  每条强制带认知档(默认②法度推演)+ 出处 + 版本印(模型/批次/提示词版本)。

铁律:
  · 只走内部免费池网关 https://gufangai.com/api/gateway/chat,严禁任何按量计费源
  · 本机零算力:纯 HTTP + D1
  · 入库一律 status='draft' —— **不自动上线**,由 CTO/创始人在后台审核后才 published

用法:
  D1_API_TOKEN=... python scripts/content_factory/herb_factory.py --target herb --count 6
  D1_API_TOKEN=... python scripts/content_factory/herb_factory.py --target biocomp --count 6
"""
import os, sys, json, time, uuid, argparse, urllib.request

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "b7362ed77d212bab298a9ae8736c9868")
D1_DB      = os.environ.get("D1_DATABASE_ID", "2db89d3b-e988-4577-a9e3-fb7c563af72f")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GATEWAY    = os.environ.get("GW_URL", "https://gufangai.com/api/gateway/chat")
PROMPT_VER = "hf-v1-2026-08-02"

# ── 合规硬闸:命中任一即整条丢弃 ─────────────────────────────────
BANNED = [
    "克", "钱重", "水煎服", "每日三次", "每次服", "煎服", "顿服", "口服剂量",
    "疗效显著", "治愈率", "包治", "特效", "根治",
    "建议你服用", "你应该服用", "可以服用", "推荐剂量", "用法用量",
]

HERB_SEED = [  # 候选池:阿育吠陀常用药,AI 只需就"已给定的名字"产出结构化内容,不自己编药名
    ("Shatavari", "Asparagus racemosus"), ("Triphala", "Terminalia chebula 等三果"),
    ("Neem", "Azadirachta indica"), ("Amla", "Phyllanthus emblica"),
    ("Brahmi", "Bacopa monnieri"), ("Guggul", "Commiphora wightii"),
    ("Punarnava", "Boerhavia diffusa"), ("Manjistha", "Rubia cordifolia"),
    ("Vacha", "Acorus calamus"), ("Yashtimadhu", "Glycyrrhiza glabra"),
    ("Pippali", "Piper longum"), ("Arjuna", "Terminalia arjuna"),
]
BIO_SEED = [
    ("黄芩苷", "Baicalin"), ("丹参酮ⅡA", "Tanshinone IIA"), ("黄芪甲苷", "Astragaloside IV"),
    ("人参皂苷Rg1", "Ginsenoside Rg1"), ("小檗碱", "Berberine"), ("川芎嗪", "Ligustrazine"),
    ("葛根素", "Puerarin"), ("芍药苷", "Paeoniflorin"), ("雷公藤甲素", "Triptolide"),
    ("青蒿素", "Artemisinin"), ("淫羊藿苷", "Icariin"), ("绿原酸", "Chlorogenic acid"),
]


def d1(sql, params=None):
    if not D1_TOKEN:
        sys.exit("缺 D1_API_TOKEN")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_DB}/query"
    payload = {"sql": sql}
    if params:
        payload["params"] = params
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + D1_TOKEN, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if not j.get("success"):
        raise RuntimeError(str(j.get("errors"))[:250])
    return (j.get("result") or [{}])[0].get("results") or []


def ask(system, user, timeout=120):
    body = json.dumps({
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 1400, "temperature": 0.3, "json": True, "source": "content_factory",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY, method="POST", data=body,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"})
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    txt = j.get("text") or ""
    if not txt and j.get("choices"):
        txt = (j["choices"][0].get("message") or {}).get("content", "")
    return txt, (j.get("model") or j.get("supplier") or "")


def strip_json(t):
    t = (t or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if len(t.split("```")) > 1 else t
        t = t.replace("json", "", 1).strip()
    a, b = t.find("{"), t.rfind("}")
    return t[a:b + 1] if a >= 0 and b > a else t


def violates(obj):
    """整条扫合规红线,命中即丢。返回命中的词(空=干净)。"""
    blob = json.dumps(obj, ensure_ascii=False)
    return [w for w in BANNED if w in blob]


SYS_HERB = (
    "你是「古方 AI 星图」的跨文明本草研讨助手。任务:就**给定的**阿育吠陀药材,"
    "输出结构化的传统属性与中医视角对照。\n"
    "**硬性红线(违反则整条作废)**:①绝不写任何剂量、煎服法、用法用量;"
    "②绝不做疗效承诺或治愈表述;③绝不出现「建议你/你应该服用」这类祈使句;"
    "④中医对照必须写明是**推演**而非古籍原文,不得伪造出处。\n"
    "只输出 JSON,字段:name_cn(中文名)、name_native(梵文名)、props(性味 Rasa/Guna/Virya/Vipaka 对象)、"
    "constitution(调节体质)、traditional_use(传统功用)、indications(主治,用「文献记载用于…」句式)、"
    "actives(主要活性成分数组)、tcm_compare(中医视角对照,150-250字,须含「据文献推演」字样)、"
    "tcm_refs(所据中医方药/文献名数组)。"
)
SYS_BIO = (
    "你是「古方 AI 星图」的中药成分研讨助手。任务:就**给定的**中药活性成分,输出结构化的分子与机制信息。\n"
    "**硬性红线(违反则整条作废)**:①绝不写剂量/用法;②绝不做疗效承诺;③机制描述须是研讨性表述"
    "(「体外研究提示…」「文献报道…」),不得写成临床结论;④不确定的字段留空,**绝不编造** CID/UniProt 号。\n"
    "只输出 JSON,字段:name_cn、name_en、source_herb(来源药材中文名)、formula(分子式)、"
    "smiles(不确定则留空字符串)、mol_weight(数字,不确定填 null)、targets(已知靶点数组)、"
    "mechanism(作用机制,120-200字,研讨性表述)、tcm_link(与中医理论的对照推演,100-180字)、"
    "refs(文献出处数组,如 PubChem/PubMed 名称,不确定则给空数组)。"
)


def gen_herb(name_en, latin):
    txt, model = ask(SYS_HERB, f"药材:{name_en}(学名 {latin})。按要求输出 JSON。")
    return json.loads(strip_json(txt)), model


def gen_bio(cn, en):
    txt, model = ask(SYS_BIO, f"成分:{cn}({en})。按要求输出 JSON。")
    return json.loads(strip_json(txt)), model


def q(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    return "'" + str(v).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["herb", "biocomp"], required=True)
    ap.add_argument("--count", type=int, default=6)
    args = ap.parse_args()

    run_id = "cf_" + uuid.uuid4().hex[:12]
    tbl = "herb_compare" if args.target == "herb" else "biocomp_entries"
    now = int(time.time())
    d1(f"INSERT INTO content_gen_runs (run_id,target,requested,started_at) VALUES ({q(run_id)},{q(tbl)},{args.count},{now})")
    print(f"[内容工厂] run={run_id} target={tbl} 计划 {args.count} 条\n", flush=True)

    have = {r["n"] for r in d1(f"SELECT name_cn AS n FROM {tbl}")}
    seed = HERB_SEED if args.target == "herb" else BIO_SEED
    ins = dup = fail = rej = 0
    model_used = ""

    for item in seed:
        if ins + dup + fail + rej >= args.count:
            break
        try:
            obj, model = (gen_herb(*item) if args.target == "herb" else gen_bio(*item))
            model_used = model or model_used
        except Exception as e:
            fail += 1
            print(f"  ✗ {item[0]} 生成失败: {type(e).__name__} {str(e)[:90]}", flush=True)
            continue

        bad = violates(obj)
        if bad:
            rej += 1
            print(f"  ⛔ {item[0]} 触合规红线,整条丢弃 → 命中 {bad[:3]}", flush=True)
            continue

        name_cn = (obj.get("name_cn") or item[0]).strip()
        if name_cn in have:
            dup += 1
            print(f"  = {name_cn} 已存在,跳过", flush=True)
            continue

        eid = uuid.uuid4().hex[:16]
        if args.target == "herb":
            sql = (f"INSERT INTO herb_compare (herb_id,tradition,name_cn,name_en,name_latin,name_native,"
                   f"props_json,constitution,traditional_use,indications,actives,tcm_compare,tcm_refs,"
                   f"cog_tier,status,gen_model,gen_run_id,prompt_ver,created_at,updated_at) VALUES ("
                   f"{q(eid)},'ayurveda',{q(name_cn)},{q(item[0])},{q(item[1])},{q(obj.get('name_native'))},"
                   f"{q(obj.get('props'))},{q(obj.get('constitution'))},{q(obj.get('traditional_use'))},"
                   f"{q(obj.get('indications'))},{q(obj.get('actives'))},{q(obj.get('tcm_compare'))},"
                   f"{q(obj.get('tcm_refs'))},'②法度推演','draft',{q(model_used)},{q(run_id)},{q(PROMPT_VER)},{now},{now})")
        else:
            sql = (f"INSERT INTO biocomp_entries (entry_id,kind,name_cn,name_en,source_herb,formula,smiles,"
                   f"mol_weight,targets,mechanism,tcm_link,cog_tier,refs_json,status,gen_model,gen_run_id,"
                   f"prompt_ver,created_at,updated_at) VALUES ("
                   f"{q(eid)},'compound',{q(name_cn)},{q(obj.get('name_en'))},{q(obj.get('source_herb'))},"
                   f"{q(obj.get('formula'))},{q(obj.get('smiles'))},{q(obj.get('mol_weight'))},"
                   f"{q(obj.get('targets'))},{q(obj.get('mechanism'))},{q(obj.get('tcm_link'))},"
                   f"'②法度推演',{q(obj.get('refs'))},'draft',{q(model_used)},{q(run_id)},{q(PROMPT_VER)},{now},{now})")
        try:
            d1(sql)
            ins += 1
            have.add(name_cn)
            print(f"  ✓ {name_cn}  已入库(draft,待审)", flush=True)
        except Exception as e:
            fail += 1
            print(f"  ✗ {name_cn} 写库失败: {str(e)[:110]}", flush=True)

    d1(f"UPDATE content_gen_runs SET inserted={ins},skipped_dup={dup},failed={fail},"
       f"compliance_reject={rej},model={q(model_used)},finished_at={int(time.time())} WHERE run_id={q(run_id)}")
    print(f"\n[完] 入库 {ins} · 重复跳过 {dup} · 合规拦下 {rej} · 失败 {fail}")
    print(f"     全部 status='draft',**不自动上线**,后台审核通过才 published")
    print(f"     复查:SELECT * FROM {tbl} WHERE gen_run_id='{run_id}'")


if __name__ == "__main__":
    main()
