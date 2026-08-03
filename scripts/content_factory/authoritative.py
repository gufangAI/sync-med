#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
权威库取数产线 —— 事实从权威库拉,AI 只写中医视角叙述

【立此因】2026-08-03 实测,老路线(让 LLM 凭空写分子式/SMILES/靶点/出处)已经走死:
  · biocomp_entries published 只有 37 条、herb_compare 13 条,跑了两天。
  · 已发布的 22 条带结构式条目里 **18 条结构式是编的**(_smiles 原子对账抓出)。
  · 被独立复核按住的 192 条,主因:SMILES 造假 19% / 出处不实 15% / 体外写成临床 8% / 靶点问题 7%。
  根因不是产能是**路线**:让模型凭空生成可验证的硬事实,编得越多拒得越多,产量永远上不去。

【本文件的做法】把"事实"和"叙述"彻底分开:
  · 事实字段(formula / smiles / mol_weight / targets / 出处)—— **一律从权威库取,一个字段都不让 AI 填**;
    查不到就留空,绝不由模型补。
  · AI 只写 tcm_link 那一段中医视角对照叙述,而且喂给它的是**已经拿到的真值**。
  · 每条记录 source / source_id / source_url / fetched_at —— 这就是真出处,直接治"出处不实"。

【实测结论(2026-08-03,详见 docs/new/交付_权威库打通_2026-08-03.md)】
  · PubChem PUG-REST:免密免费可用。**本机必须直连**——系统 HTTP_PROXY=127.0.0.1:1082 会把
    ncbi.nlm.nih.gov 的 TLS 打断(SSLEOFError),看起来像"PubChem 挂了",其实是代理。
    所以下面统一 trust_env=False;GitHub Actions 上本来就没代理,同一份代码两边都对。
  · ChEMBL:免密免费可用,而且**自带 full_molformula + canonical_smiles**,可当 PubChem 的独立校验与兜底;
    activities(pchembl_value>=6)给靶点,还带 document_chembl_id,是真出处。
  · UniProt:免密免费可用,把靶点标准化到 accession。
  · KEGG:REST 可用但**许可禁止**——官方明写非学术用途需商业授权。我们是收费平台,**不接**。
  · IMPPAT / TCMSP / SymMap / ETCM / TCMID:无开放 API(详见报告),本轮不接。

【红线】AI 推理只走内部免费池网关;PubChem/ChEMBL/UniProt 是公开科学数据库,不是 AI 推理源,不受该条限制。
"""
import os, re, sys, json, time, argparse, threading, datetime
import urllib.request, urllib.parse, urllib.error

# Windows 控制台默认 GBK,打 ✓/✗ 和中文会直接 UnicodeEncodeError 把整轮跑挂
# (2026-08-03 实测:取数全成功,却死在 print 上)。**显示层不该杀死数据层**,这里强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _ai import d1, q, ask_best, parse_json          # 公共底座,禁止再抄一份
from _smiles import smiles_matches_formula           # 入库前确定性对账

PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
CHEMBL  = "https://www.ebi.ac.uk/chembl/api/data"
UNIPROT = "https://rest.uniprot.org/uniprotkb"
UA = "guyaofang-tcm-research/1.0 (+https://gufangai.com; contact hosonzuo@gmail.com)"
PROMPT_VER = "auth-v1"

# ── 限速器 ─────────────────────────────────────────────────────────────
# PubChem 官方要求 ≤5 请求/秒,我们压到 3 —— 把 IP 打黑的代价远大于省下的几十秒。
class RateLimiter:
    def __init__(self, per_sec):
        self.interval = 1.0 / float(per_sec)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            if now < self.next_at:
                time.sleep(self.next_at - now)
                now = time.time()
            self.next_at = now + self.interval

RL = {"pubchem": RateLimiter(3), "chembl": RateLimiter(5), "uniprot": RateLimiter(5)}


def http_json(url, who, tries=4, timeout=30):
    """直连取 JSON。429/5xx 指数退避;404 视为"查无此物"返回 None,不当异常。

    **必须绕开系统代理**:本机 HTTP_PROXY 指向 127.0.0.1:1082,它会打断 NCBI 的 TLS
    (稳定复现 SSLEOFError),而直连 200。用 ProxyHandler({}) 显式清空,等价于 trust_env=False。
    """
    RL[who].wait()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    delay = 1.0
    for attempt in range(tries):
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                return None                       # 查无此物 / 属性名不认,交给调用方降级
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(delay); delay *= 2; RL[who].wait(); continue
            return None
        except Exception:
            if attempt < tries - 1:
                time.sleep(delay); delay *= 2; continue
            return None
    return None


# ── PubChem ────────────────────────────────────────────────────────────
# 属性名各版本不一致(CanonicalSMILES 曾被 ConnectivitySMILES/SMILES 取代)。
# 不照抄文档,**启动时实测一次**,认哪个用哪个。
_SMILES_PROP = None
_SMILES_CANDIDATES = ("SMILES", "ConnectivitySMILES", "CanonicalSMILES", "IsomericSMILES")


def detect_pubchem_smiles_prop():
    global _SMILES_PROP
    if _SMILES_PROP:
        return _SMILES_PROP
    for p in _SMILES_CANDIDATES:
        j = http_json(f"{PUBCHEM}/compound/cid/2353/property/{p}/JSON", "pubchem", tries=2)
        props = ((j or {}).get("PropertyTable") or {}).get("Properties") or []
        if props and any(k != "CID" and props[0].get(k) for k in props[0]):
            _SMILES_PROP = p
            print(f"  [PubChem] SMILES 属性名实测为 {p}", flush=True)
            return p
    print("  [PubChem] 四个候选属性名都取不到 SMILES,本轮不取结构式", flush=True)
    _SMILES_PROP = ""
    return ""


def pubchem_lookup(name_en):
    """英文名 → CID → 权威属性。返回 dict 或 None。事实字段全部来自返回体。"""
    if not name_en:
        return None
    enc = urllib.parse.quote(name_en.strip())
    j = http_json(f"{PUBCHEM}/compound/name/{enc}/cids/JSON", "pubchem")
    cids = ((j or {}).get("IdentifierList") or {}).get("CID") or []
    if not cids:
        return None
    cid = cids[0]
    sp = detect_pubchem_smiles_prop()
    fields = ["MolecularFormula", "MolecularWeight", "InChIKey", "IUPACName"]
    if sp:
        fields.insert(1, sp)
    j = http_json(f"{PUBCHEM}/compound/cid/{cid}/property/{','.join(fields)}/JSON", "pubchem")
    props = ((j or {}).get("PropertyTable") or {}).get("Properties") or []
    if not props:
        return None
    p = props[0]
    return {
        "source": "pubchem",
        "source_id": f"CID{cid}",
        "source_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        "formula": p.get("MolecularFormula"),
        "smiles": p.get(sp) if sp else None,
        "mol_weight": p.get("MolecularWeight"),
        "inchikey": p.get("InChIKey"),
        "iupac": p.get("IUPACName"),
    }


# ── ChEMBL ─────────────────────────────────────────────────────────────
def chembl_lookup(name_en):
    """英文名 → CHEMBL ID → 分子式/SMILES。ChEMBL 自带这两项,是 PubChem 的**独立**交叉验证源。"""
    if not name_en:
        return None
    n = urllib.parse.quote(name_en.strip().upper())
    j = http_json(f"{CHEMBL}/molecule.json?pref_name__iexact={n}&limit=1", "chembl")
    mols = (j or {}).get("molecules") or []
    if not mols:      # pref_name 不中就走同义名
        j = http_json(f"{CHEMBL}/molecule.json?molecule_synonyms__molecule_synonym__iexact={n}&limit=1", "chembl")
        mols = (j or {}).get("molecules") or []
    if not mols:
        return None
    m = mols[0]
    ms = m.get("molecule_structures") or {}
    mp = m.get("molecule_properties") or {}
    cid = m.get("molecule_chembl_id")
    return {
        "source": "chembl",
        "source_id": cid,
        "source_url": f"https://www.ebi.ac.uk/chembl/compound_report_card/{cid}/",
        "formula": mp.get("full_molformula"),
        "smiles": ms.get("canonical_smiles"),
        "mol_weight": mp.get("full_mwt"),
        "inchikey": ms.get("standard_inchi_key"),
        "pref_name": m.get("pref_name"),
        "max_phase": m.get("max_phase"),
    }


def chembl_targets(chembl_id, min_pchembl=6.0, limit=25):
    """靶点只取 pChEMBL≥6 的高质量活性记录,并**带上 document_chembl_id 当出处**。

    这是治"靶点无出处"的正解:老路线让模型写靶点,复核问"出处呢"就哑了;
    现在每个靶点都能指到 ChEMBL 的具体文献条目。
    """
    if not chembl_id:
        return [], []
    j = http_json(f"{CHEMBL}/activity.json?molecule_chembl_id={chembl_id}"
                  f"&pchembl_value__gte={min_pchembl}&limit={limit}", "chembl", timeout=45)
    acts = (j or {}).get("activities") or []
    seen, targets, refs = {}, [], []
    for a in acts:
        tid, tname = a.get("target_chembl_id"), a.get("target_pref_name")
        if not tid or not tname or tname == "Unchecked" or tid in seen:
            continue
        seen[tid] = 1
        targets.append({
            "target": tname, "chembl_target_id": tid,
            "assay": a.get("standard_type"), "value": a.get("standard_value"),
            "units": a.get("standard_units"), "pchembl": a.get("pchembl_value"),
            "organism": a.get("target_organism"),
            "evidence": "in_vitro",     # ChEMBL activity 一律是体外/生化,**绝不写成临床**
        })
        if a.get("document_chembl_id"):
            refs.append({"type": "chembl_document", "id": a["document_chembl_id"],
                         "url": f"https://www.ebi.ac.uk/chembl/document_report_card/{a['document_chembl_id']}/"})
    return targets, refs


def uniprot_accession(target_name):
    """靶点名 → UniProt accession(人类,已审编)。取不到就留空,不猜。

    【2026-08-03 线上事故修复】首轮真跑 120 条**全部失败**,报 `HTTPError: HTTP Error 400`,
      入库 0(而取数是好的:PubChem 命中 75、ChEMBL 61)。
      真因就在这里:ChEMBL 的靶点名常带括号/斜杠/逗号
      (如 `Cytochrome P450 3A4 (CYP3A4)`、`Sodium/potassium-transporting ATPase`),
      原样塞进 UniProt 的 Lucene 查询串会**打断语法 → 400**;
      而这一步**没有自己的 try**,异常直接冒到条目级,整条作废。
      dry-run 没暴露是因为它跑的是 `--no-ai` 且样本不同,没撞上带特殊字符的靶点名。
    两处修:①只保留字母数字与空格再查(拿不到就留空,本来就允许空)
           ②**自己兜异常** —— 这是"锦上添花"的字段,绝不许它拖垮整条入库。
    """
    if not target_name:
        return None
    # Lucene 语法敏感字符一律洗掉;洗完为空就别查了
    clean = re.sub(r'[^0-9A-Za-z \-]', ' ', str(target_name)).strip()
    clean = re.sub(r'\s+', ' ', clean)[:60]
    if len(clean) < 3:
        return None
    try:
        qs = urllib.parse.urlencode({
            "query": f'protein_name:"{clean}" AND organism_id:9606 AND reviewed:true',
            "format": "json", "size": 1, "fields": "accession,protein_name"})
        j = http_json(f"{UNIPROT}/search?{qs}", "uniprot")
        res = (j or {}).get("results") or []
        return res[0].get("primaryAccession") if res else None
    except Exception:
        return None    # 取不到就留空 —— 绝不让它拖垮整条入库


# ── 合并 + 对账 ────────────────────────────────────────────────────────
def merge_and_verify(pc, ce):
    """PubChem 与 ChEMBL 合并。**两个独立库的结构式必须互相印证**,这是权威库版的"交叉验证"。

    取向沿用 _smiles 的老规矩:宁可留空,不放错结构式。
    """
    fact = {"source": None, "source_id": None, "source_url": None,
            "formula": None, "smiles": None, "mol_weight": None, "inchikey": None,
            "xref": {}, "checks": []}
    primary = pc or ce
    if not primary:
        return None
    fact.update({k: primary.get(k) for k in
                 ("source", "source_id", "source_url", "formula", "smiles", "mol_weight", "inchikey")})
    if pc:
        fact["xref"]["pubchem"] = pc["source_id"]
    if ce:
        fact["xref"]["chembl"] = ce["source_id"]

    # ① 两库都有 InChIKey → 必须一致,否则是**认错分子**(比结构式错更严重),整条弃掉
    if pc and ce and pc.get("inchikey") and ce.get("inchikey"):
        if pc["inchikey"].split("-")[0] != ce["inchikey"].split("-")[0]:
            fact["checks"].append(("mismatch_identity", f"PubChem {pc['inchikey']} ≠ ChEMBL {ce['inchikey']}"))
            return None
        fact["checks"].append(("identity_ok", "PubChem/ChEMBL InChIKey 骨架一致"))

    # ② 结构式与分子式做原子对账 —— 权威库的数据也要过一遍,防字段错配
    ok, why = smiles_matches_formula(fact.get("smiles"), fact.get("formula"))
    if ok:
        fact["checks"].append(("smiles_formula_ok", why))
    else:
        alt = ce if primary is pc else pc
        ok2, why2 = (False, "无备选源")
        if alt:
            ok2, why2 = smiles_matches_formula(alt.get("smiles"), alt.get("formula"))
        if ok2:
            fact["smiles"], fact["formula"] = alt["smiles"], alt["formula"]
            fact["checks"].append(("smiles_fallback_ok", f"主源对不上({why}),改用 {alt['source']}:{why2}"))
        else:
            fact["smiles"] = None            # 清掉结构式,条目仍可发布,只是不画结构图
            fact["checks"].append(("smiles_dropped", why))
    return fact


# ── AI:只写中医视角叙述,一个事实字段都不给它填 ──────────────────────
SYS_TCM = (
    "你是中医药学者。任务:只写「中医视角对照」一段叙述。"
    "硬性要求:"
    "①**不得输出任何分子式、SMILES、分子量、靶点名、文献编号**——这些已由权威数据库给定,不需要你提供;"
    "②不得写剂量、疗效承诺、诊疗建议;"
    "③已给的靶点数据全部来自体外/生化实验,**只能表述为体外研究提示,严禁写成临床疗效**;"
    "④中医内容只讲传统性味归经、功效主治与该成分的对应关系,讲不出就直说证据不足;"
    "⑤只输出 JSON:{\"tcm_link\":\"...\"},150-260 字。"
)


def ai_tcm_narrative(name_cn, name_en, herb, targets):
    tnames = "、".join(t["target"] for t in targets[:6]) or "(权威库未收录高质量靶点数据)"
    user = (f"成分中文名:{name_cn}\n英文名:{name_en}\n主要来源药材:{herb or '未知'}\n"
            f"权威库(ChEMBL)记载的体外活性靶点:{tnames}\n"
            f"请只写中医视角对照叙述,输出 {{\"tcm_link\":\"...\"}}")
    try:
        txt, model = ask_best(SYS_TCM, user, need="{", max_tokens=900, timeout=90)
        obj = parse_json(txt)
        return (obj.get("tcm_link") or "").strip(), model
    except Exception as e:
        return "", f"AI失败:{type(e).__name__}"


# ── 表结构:加列(只加不改,已有 AI 编造条目一条不删)────────────────
# 【2026-08-03 实测纠正】pragma_table_info 查出来的真实列里,**已经有 pubchem_cid / uniprot_id**,
#   只是老工厂从来没往里写过(published 37 条里 pubchem_cid 有值的 = 0 条,全表 uniprot_id = 0 条)。
#   所以这两个字段**不能再新建同义列**,必须直接填已有的那两列 —— 这就是"先核实再动手"的意义。
NEW_COLS = [("source", "TEXT"), ("source_id", "TEXT"),
            ("source_url", "TEXT"), ("fetched_at", "INTEGER")]


def existing_cols(table):
    return {r["name"] for r in d1(f"SELECT * FROM pragma_table_info('{table}')")}


def ensure_columns(table="biocomp_entries"):
    """先 pragma_table_info 核实,缺哪列加哪列。**不猜列名、不改已有列、不删数据**。
    加完后 source 为空的老条目 = AI 生成,天然可区分。"""
    have = existing_cols(table)
    if not have:
        raise RuntimeError(f"{table} 读不到列定义,停手(不许在不确定的表上写数据)")
    added = []
    for col, typ in NEW_COLS:
        if col not in have:
            d1(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            added.append(col)
    print(f"  [表结构] {table} 已有 {len(have)} 列;本次新增 {added or '无(已齐)'}", flush=True)
    return added


# ── 主流程 ─────────────────────────────────────────────────────────────
def load_worklist(args):
    """待办来源:①--names 显式给 ②D1 里 name_en 有值但 source 为空的老条目(优先补真值)
       ③内置种子表(首轮冷启动)。中英文对照绝不让 AI 现编——种子表是人工核过的。"""
    if args.names:
        return [(n.strip(), None, None) for n in args.names.split(",") if n.strip()]
    out = []
    if not args.dry_run:
        try:
            rows = d1("SELECT name_cn, name_en, source_herb FROM biocomp_entries "
                      "WHERE (source IS NULL OR source='') AND name_en IS NOT NULL AND name_en<>'' "
                      f"LIMIT {int(args.count)}")
            out = [(r["name_cn"], r["name_en"], r.get("source_herb")) for r in rows]
        except Exception as e:
            print(f"  [待办] 读 D1 失败({str(e)[:80]}),改用种子表", flush=True)
    if not out:
        out = [(cn, en, hb) for cn, en, hb in SEED][:int(args.count)]
    return out


# 种子表:中文名 / 英文名 / 来源药材 —— 人工核对过的中医常用成分,**英文名不许模型现编**
SEED = [
    ("小檗碱", "berberine", "黄连"), ("黄芩苷", "baicalin", "黄芩"),
    ("汉黄芩素", "wogonin", "黄芩"), ("黄芩素", "baicalein", "黄芩"),
    ("苦参碱", "matrine", "苦参"), ("氧化苦参碱", "oxymatrine", "苦参"),
    ("川芎嗪", "tetramethylpyrazine", "川芎"), ("阿魏酸", "ferulic acid", "当归"),
    ("丹参酮IIA", "tanshinone IIA", "丹参"), ("丹酚酸B", "salvianolic acid B", "丹参"),
    ("人参皂苷Rg1", "ginsenoside Rg1", "人参"), ("人参皂苷Rb1", "ginsenoside Rb1", "人参"),
    ("黄芪甲苷", "astragaloside IV", "黄芪"), ("毛蕊异黄酮", "calycosin", "黄芪"),
    ("甘草酸", "glycyrrhizic acid", "甘草"), ("甘草次酸", "glycyrrhetinic acid", "甘草"),
    ("芍药苷", "paeoniflorin", "白芍"), ("柚皮苷", "naringin", "枳实"),
    ("橙皮苷", "hesperidin", "陈皮"), ("葛根素", "puerarin", "葛根"),
    ("大黄素", "emodin", "大黄"), ("大黄酸", "rhein", "大黄"),
    ("槲皮素", "quercetin", "多药材共有"), ("木犀草素", "luteolin", "金银花"),
    ("绿原酸", "chlorogenic acid", "金银花"), ("熊果酸", "ursolic acid", "女贞子"),
    ("齐墩果酸", "oleanolic acid", "女贞子"), ("青蒿素", "artemisinin", "青蒿"),
    ("雷公藤甲素", "triptolide", "雷公藤"), ("穿心莲内酯", "andrographolide", "穿心莲"),
    ("薯蓣皂苷元", "diosgenin", "山药"), ("淫羊藿苷", "icariin", "淫羊藿"),
    ("五味子甲素", "deoxyschisandrin", "五味子"), ("桂皮醛", "cinnamaldehyde", "桂枝"),
    ("厚朴酚", "magnolol", "厚朴"), ("和厚朴酚", "honokiol", "厚朴"),
    ("补骨脂素", "psoralen", "补骨脂"), ("白藜芦醇", "resveratrol", "虎杖"),
    ("虎杖苷", "polydatin", "虎杖"), ("栀子苷", "geniposide", "栀子"),
]


def run(args):
    started = time.time()
    run_id = os.environ.get("GITHUB_RUN_ID") or f"local-{int(started)}"
    stat = {"待办": 0, "PubChem命中": 0, "ChEMBL命中": 0, "任一库命中": 0, "两库都命中": 0,
            "身份不一致弃": 0, "结构式对账通过": 0, "结构式被清": 0, "拿到靶点": 0,
            "靶点条数": 0, "UniProt标准化": 0, "AI叙述成功": 0, "入库": 0, "跳过已有": 0, "失败": 0}
    detail = []

    if not args.dry_run:
        ensure_columns("biocomp_entries")
        try:
            done = {r["source_id"] for r in d1(
                "SELECT source_id FROM biocomp_entries WHERE source_id IS NOT NULL AND source_id<>''")}
        except Exception:
            done = set()
    else:
        done = set()
    print(f"  [幂等] 已有 source_id {len(done)} 个,重复的直接跳过", flush=True)

    work = load_worklist(args)
    stat["待办"] = len(work)
    print(f"  [待办] {len(work)} 条\n", flush=True)

    for i, (name_cn, name_en, herb) in enumerate(work, 1):
        en = name_en or name_cn
        try:
            pc = pubchem_lookup(en)
            ce = chembl_lookup(en)
            if pc: stat["PubChem命中"] += 1
            if ce: stat["ChEMBL命中"] += 1
            if pc and ce: stat["两库都命中"] += 1
            fact = merge_and_verify(pc, ce)
            if not fact:
                if pc and ce:
                    stat["身份不一致弃"] += 1
                stat["失败"] += 1
                print(f"  [{i}/{len(work)}] ✗ {name_cn}({en}) 权威库查无 / 身份不一致", flush=True)
                detail.append({"name": name_cn, "en": en, "ok": False, "why": "查无或身份不一致"})
                continue
            stat["任一库命中"] += 1
            if fact.get("smiles"):
                stat["结构式对账通过"] += 1
            elif any(c[0] == "smiles_dropped" for c in fact["checks"]):
                stat["结构式被清"] += 1

            targets, refs = chembl_targets(fact["xref"].get("chembl"))
            if targets:
                stat["拿到靶点"] += 1
                stat["靶点条数"] += len(targets)
                if not args.no_uniprot:
                    for t in targets[:3]:
                        acc = uniprot_accession(t["target"])
                        if acc:
                            t["uniprot"] = acc
                            stat["UniProt标准化"] += 1

            tcm, model_used = ("", "skipped")
            if not args.no_ai:
                tcm, model_used = ai_tcm_narrative(name_cn, en, herb, targets)
                if tcm:
                    stat["AI叙述成功"] += 1

            if fact["source_id"] in done:
                stat["跳过已有"] += 1
                print(f"  [{i}/{len(work)}] ~ {name_cn} {fact['source_id']} 已有,跳过", flush=True)
                continue

            refs_all = [{"type": "database", "source": fact["source"], "id": fact["source_id"],
                         "url": fact["source_url"], "fetched_at": int(time.time())}] + refs[:8]
            detail.append({"name": name_cn, "en": en, "ok": True, "source": fact["source"],
                           "source_id": fact["source_id"], "formula": fact["formula"],
                           "smiles_ok": bool(fact["smiles"]), "mw": fact["mol_weight"],
                           "targets": len(targets), "checks": fact["checks"], "tcm_len": len(tcm)})

            if args.dry_run:
                print(f"  [{i}/{len(work)}] ✓(dry-run) {name_cn} {fact['source_id']} "
                      f"{fact['formula']} SMILES={'有' if fact['smiles'] else '空'} 靶点{len(targets)}", flush=True)
                continue

            now = int(time.time())
            eid = f"auth-{fact['source_id']}".lower()
            # pubchem_cid / uniprot_id 用**表里已有的列**(老工厂一直空着),不另建同义列
            pcid = (fact["xref"].get("pubchem") or "").replace("CID", "") or None
            upid = next((t["uniprot"] for t in targets if t.get("uniprot")), None)
            sql = (f"INSERT INTO biocomp_entries (entry_id,kind,name_cn,name_en,source_herb,formula,smiles,"
                   f"pubchem_cid,uniprot_id,mol_weight,targets,mechanism,tcm_link,cog_tier,refs_json,status,"
                   f"gen_model,gen_run_id,prompt_ver,created_at,updated_at,source,source_id,source_url,fetched_at"
                   f") VALUES ("
                   f"{q(eid)},'compound',{q(name_cn)},{q(en)},{q(herb)},{q(fact['formula'])},{q(fact['smiles'])},"
                   f"{q(pcid)},{q(upid)},"
                   f"{q(fact['mol_weight'])},{q(targets)},{q('')},{q(tcm)},"
                   f"'①有出处',{q(refs_all)},'draft',{q(model_used)},{q(run_id)},{q(PROMPT_VER)},{now},{now},"
                   f"{q(fact['source'])},{q(fact['source_id'])},{q(fact['source_url'])},{now})")
            d1(sql)
            done.add(fact["source_id"])
            stat["入库"] += 1
            print(f"  [{i}/{len(work)}] ✓ {name_cn} {fact['source_id']} {fact['formula']} "
                  f"靶点{len(targets)} 已入库(draft)", flush=True)
        except Exception as e:
            stat["失败"] += 1
            print(f"  [{i}/{len(work)}] ✗ {name_cn} 异常 {type(e).__name__}: {str(e)[:120]}", flush=True)

    print("\n======== 本轮真实数字 ========")
    for k, v in stat.items():
        print(f"  {k}: {v}")
    print(f"  墙钟: {round(time.time()-started,1)}s")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"stat": stat, "detail": detail,
                       "at": datetime.datetime.now().isoformat()}, f, ensure_ascii=False, indent=1)
        print(f"  明细已写 {args.report}")
    return stat


def main():
    ap = argparse.ArgumentParser(description="权威库取数产线:事实来自权威库,AI 只写中医叙述")
    ap.add_argument("--count", default="20", help="本轮处理条数")
    ap.add_argument("--names", default="", help="逗号分隔的成分名(中文或英文),给了就只跑这些")
    ap.add_argument("--dry-run", action="store_true", help="只取数不写 D1")
    ap.add_argument("--no-ai", action="store_true", help="跳过 AI 叙述(纯取数验证用)")
    ap.add_argument("--no-uniprot", action="store_true", help="跳过 UniProt 标准化(省请求)")
    ap.add_argument("--report", default="", help="把本轮明细写到这个 json")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
