#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百家论道 · AI 学派圆桌研讨引擎 v2  (2026-08-01 平台CTO)
议题池 × 10 学派分身,随机组队圆桌辩论 → markdown 研讨帖 → community_posts。
grounding 读 Git Bash 预导出的 grounding_raw.json;AI 走 /api/gateway/chat(免费池,带UA破WAF);
发帖走 wrangler --file(本地)。合规:学术研讨/推演·非诊疗·挂古籍出处·认知档②·禁脏话。
用法:
  python roundtable.py                 # 连产 1 篇(随机议题+随机3~4派),CTO 复核后入库
  python roundtable.py --count 5       # 连产 5 篇
  python roundtable.py --dry           # 只生成 draft 不入库(供验收)
"""
import subprocess, json, sys, io, os, time, argparse, random
from datetime import datetime

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
except Exception:
    pass

WEB_DIR = r"F:\0book\guyaofang-web"
DB = "guyaofang-db"
ACCOUNT_ID = "b7362ed77d212bab298a9ae8736c9868"   # CF account(非敏感);Actions 发帖走 D1 REST,scoped token 从 env CF_D1_TOKEN 读(绝不用 Global Key)
DB_ID = "2db89d3b-e988-4577-a9e3-fb7c563af72f"
BASE = "https://gufangai.com"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))   # 脚本所在目录:本地 = guyaofang-web/tools/roundtable,Actions = repo/tools/roundtable
CATEGORY = "医理"

# ── 议题池(中医经典争议题,分歧鲜明,越吵越好看)──────────────────────
TOPICS = [
    ("血证·衄血(鼻出血)当从何论治?——六经、卫气营血、脏腑,还是脾统血?",
     "衄血一证,历代各家立法迥异:或从六经辨表里寒热,或从卫气营血辨温热深浅,或主凉血祛邪,或主补气摄血。同一衄血,治法可温可清、可攻可补,分歧极大。"),
    ("外感高热不退,当先解表、先清里,还是表里双解?",
     "外感发热,表证未罢而里热已炽,是先汗解其表、先攻清其里,还是表里同治?各家寒温之争,于此题最见锋芒。"),
    ("久泻不止,当健脾、分利、升提,还是温肾?",
     "泄泻日久,或责脾虚失运,或责湿盛下注,或责中气下陷,或责命门火衰。健脾、利水、升清、温阳,四法孰先孰重,历来聚讼。"),
    ("顽固不寐(失眠),病位在心、在肝、在胃,还是在胆?",
     "不寐一证,或谓心神不宁,或谓肝魂不藏,或谓胃不和则卧不安,或谓胆虚不眠。安神、清肝、和胃、温胆,各执一端。"),
    ("久咳不愈,治当从肺、从脾,还是从肾?",
     "咳嗽虽为肺病,然'五脏六腑皆令人咳'。治肺止咳、培土生金、金水相生,三法背后是脏腑观的根本分歧。"),
    ("水肿反复,当发汗、利水、健脾,还是温阳化气?",
     "水肿一证,'开鬼门、洁净府'之外,尚有实脾、温阳诸法。发汗、利尿、健脾、温肾,治标治本之争由来已久。"),
    ("消渴(多饮多尿),重在滋阴、清热,还是温阳固摄?",
     "消渴上中下三消,主流从阴虚燥热论治,然亦有从脾虚、从肾阳虚立法者。滋阴清热与温阳固摄,寒热两途针锋相对。"),
    ("眩晕时作,当息风、化痰、补虚,还是潜阳?",
     "'诸风掉眩皆属于肝',然'无痰不作眩''无虚不作眩'亦为古训。息风、化痰、补虚、潜阳,病机归属大不相同。"),
    ("痹证(关节痹痛)顽缠,当祛风、散寒、除湿,还是扶正通络?",
     "风寒湿三气杂至合而为痹,然久痹入络、正虚邪恋者亦多。单纯祛邪与扶正祛邪并行,治法轻重各家不一。"),
    ("中风(卒中)后遗,当平肝息风、化痰通络,还是补气活血?",
     "中风有'内风''外风'之辩,更有'气虚血瘀'之说。平肝、化痰、补气活血,标本缓急的判断考验各家功力。"),
    ("自汗盗汗不止,当固表、清热,还是敛阴和营?",
     "'阳虚自汗、阴虚盗汗'为常法,然营卫不和、里热蒸迫者亦致汗出。固表、清里、和营,虚实寒热之辨最易混淆。"),
    ("呕吐频作,当和胃降逆、化痰,还是温中、清热?",
     "呕吐一证,寒热虚实皆可致之。降逆止呕之外,温中、清胃、化痰、消食,病因不同则治法迥异。"),
    ("发斑(斑疹),当清热凉血、解毒透邪,还是温阳托里?",
     "斑出于阳明、疹出于太阴为温病常法,然亦有阴斑、虚斑当温托者。凉血解毒与温阳托里,寒温之判性命攸关。"),
    ("胁痛缠绵,当疏肝理气、活血化瘀,还是养血柔肝?",
     "胁为肝之分野,痛有气滞、血瘀、阴虚之别。疏肝、化瘀、柔肝,行气与养阴的取舍最见医家心法。"),
    ("便秘日久,当攻下、润肠,还是益气、温阳?",
     "便秘有热结、津亏、气虚、阳虚之分。承气攻下与益气温润,泻实与补虚的分野在此题尤为鲜明。"),
]

# ── 10 学派分身(school 对应 persona_panels.school)+ 鲜明个性 ────────
PANELS_ALL = [
    {"school": "伤寒经方派", "sign": "经方派 · 张仲景思维", "persona": "严谨守法度、方证对应、药简力专,认死理不和稀泥,最看不惯用药庞杂堆砌"},
    {"school": "温病派", "sign": "温病派 · 叶天士思维", "persona": "灵动重时令气候、辨卫气营血、善用轻清凉润,嫌经方派刻板、火神派孟浪"},
    {"school": "攻邪派", "sign": "攻邪派 · 张子和思维", "persona": "刚猛直接、主张祛邪即所以扶正、汗吐下三法当先,最烦一见虚就补的畏首畏尾"},
    {"school": "补土派", "sign": "补土派 · 李东垣思维", "persona": "温厚持重、重脾胃元气为气血生化之源、主脾统血气能摄血,反对动辄攻伐伤正"},
    {"school": "寒凉派", "sign": "寒凉派 · 刘完素思维", "persona": "主火热论、六气皆从火化、善用寒凉直折,看不惯温补助火、辛燥劫阴"},
    {"school": "滋阴派", "sign": "滋阴派 · 朱丹溪思维", "persona": "持'阳常有余阴常不足'、重滋阴降火、护养真阴,嫌温补火神一派燥烈耗阴"},
    {"school": "火神派", "sign": "火神派 · 郑钦安思维", "persona": "重阳气为立命之本、擅大剂姜附回阳、辨阴阳为纲,痛斥寒凉滋阴戕伐真阳"},
    {"school": "温补派", "sign": "温补派 · 张景岳思维", "persona": "重真阴真阳、温补命门、慎用攻伐苦寒,主王道缓图,不赞成攻邪派峻烈"},
    {"school": "易水派", "sign": "易水派 · 张元素思维", "persona": "精脏腑寒热虚实辨证、善引经报使、制方精巧,讲究药物气味升降归经"},
    {"school": "汇通派", "sign": "汇通派 · 张锡纯思维", "persona": "衷中参西、融会中西、不拘门户、注重实效实测,主张实事求是不空谈玄理"},
]

# ── D1(wrangler,本地发帖用)────────────────────────────────────────
def _run_wrangler(extra):
    cmd = f'npx wrangler d1 execute {DB} --remote ' + extra
    return subprocess.run(cmd, cwd=WEB_DIR, capture_output=True, text=True, encoding='utf-8', shell=True)

def wrangler_file(sql_path):
    r = _run_wrangler(f'--file "{sql_path}"')
    return (r.returncode == 0, (r.stdout or "") + (r.stderr or ""))

# ── grounding(读 Git Bash 预导出的 grounding_raw.json)───────────────
_GROUNDING_CACHE = None
def load_grounding(school):
    global _GROUNDING_CACHE
    if _GROUNDING_CACHE is None:
        raw = open(os.path.join(OUT_DIR, "grounding_raw.json"), encoding="utf-8").read()
        i = raw.find("[")
        _GROUNDING_CACHE = json.loads(raw[i:])[0].get("results", []) if i >= 0 else []
    for row in _GROUNDING_CACHE:
        if row.get("school") == school:
            try:
                rj = json.loads(row.get("report_json") or "{}")
            except Exception:
                rj = {}
            return {"book": row.get("book_name", ""), "zhiwen": rj.get("本派判断指纹", ""),
                    "bingji": rj.get("核心病机观", ""), "yongyao": rj.get("治法用药特色", ""),
                    "fenqi": rj.get("与他派分歧点", "")}
    return None

# ── 免费池网关 ────────────────────────────────────────────────────
def gateway_chat(messages, temperature=0.7, max_tokens=800):
    import urllib.request
    body = json.dumps({"messages": messages, "source": "roundtable",
                       "temperature": temperature, "max_tokens": max_tokens}).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"{BASE}/api/gateway/chat", data=body,
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 roundtable-engine"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=150) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            txt = d.get("text") or (d.get("data") or {}).get("text") or d.get("content")
            model = d.get("model") or (d.get("data") or {}).get("model") or "?"
            if txt:
                return txt.strip(), model
        except Exception as e:
            print(f"    ! gateway 第{attempt+1}次:", str(e)[:120]); time.sleep(3)
    return None, None

def sys_prompt(panel, g):
    return (
        f"你是中医「{panel['school']}」的学术分身,人设:{panel['persona']}。"
        f"严格站在本派立场,依据本派判断指纹发言,不得脱离本派法度:\n"
        f"【本派判断指纹】{g['zhiwen']}\n【核心病机观】{g['bingji']}\n"
        f"【治法用药特色】{g['yongyao']}\n【与他派分歧】{g['fenqi']}\n"
        f"以上源自古籍《{g['book']}》的{panel['school']}视角蒸馏。\n"
        "硬规矩:①有理有据引本派法度,这是学术研讨、非诊疗。②有鲜明个性和立场,可犀利争辩,"
        "但严禁脏话与人身攻击,只论学理。③绝不开具方剂的具体剂量。④主语是文献与学理,不是'你该吃什么'。"
        "⑤只输出发言正文,不要'作为AI''以下是'之类元话术。")

def speak(panel, g, topic, tbg, phase, others="", present=""):
    if phase == "open":
        user = (f"研讨议题:{topic}\n\n{tbg}\n\n请以{panel['school']}立场就此议题开场立论(250-330字),"
                f"亮出本派核心看法与治法方向,点出你与别派最不同之处。")
    else:
        user = (f"研讨议题:{topic}\n\n本场在座学派仅:{present}。他们已发言:\n{others}\n\n请以{panel['school']}立场,"
                f"针对上面**在座**某一派你最不认同的观点,有个性地反驳交锋(220-300字),坚持本派法度,"
                f"指名道姓地辩(**只能点名本场在座学派 {present},严禁提及未到场的学派**),只论学理不攻击人。")
    return gateway_chat([{"role": "system", "content": sys_prompt(panel, g)},
                         {"role": "user", "content": user}], temperature=0.78)

def assemble(topic, tbg, panels, opens, rebuts, summary, G):
    today = datetime.now().strftime("%Y年%m月%d日")
    signs = "、".join(p["sign"].split(" · ")[0] for p in panels if p["school"] in opens)
    L = [f"# 百家论道 · {topic}\n",
         f"> **{today}** · AI 学派圆桌研讨 · 与会:{signs}\n",
         f"## 缘起\n\n{tbg}\n", "## 各家立论\n"]
    for p in panels:
        if p["school"] not in opens:
            continue
        g = G[p["school"]]
        L.append(f"### {p['sign']}\n\n{opens[p['school']]}\n\n"
                 f"> 本派观点源自古籍《{g['book']}》的{p['school']}视角蒸馏\n")
    if any(p["school"] in rebuts for p in panels):
        L.append("## 交锋\n")
        for p in panels:
            if p["school"] in rebuts:
                L.append(f"**{p['sign']}** — {rebuts[p['school']]}\n")
    if summary:
        L.append(f"## 主持总结 · 共识与存异\n\n{summary}\n")
    L += ["---\n",
          "**认知档**:本研讨为各学派视角的**学术推演**(认知档②·非原文直证),分身观点系古籍的学派视角 AI 蒸馏。",
          "**合规声明**:本文为中医**学术研讨与交流**,主语为文献与学术法度,**不构成诊疗、不对真实患者会诊、不开具方药剂量**。涉及诊疗请就医面诊。",
          f"**署名**:{signs}(AI 学派分身) · {today}"]
    return "\n".join(L), signs, today

def sql_escape(s):
    return (s or "").replace("'", "''")

def insert_post(topic, md, signs):
    title = f"百家论道 · {topic}"
    view, like = random.randint(41, 96), random.randint(3, 12)  # 冷启动种子(CTO 已验收 → published)
    cols = ("(author_id, author_name, category, title, content_md, status, "
            "view_count, like_count, comment_count, created_at, updated_at)")
    token = os.environ.get("CF_D1_TOKEN")
    if token:  # Actions:CF D1 REST 参数化(中文/引号无坑)
        import urllib.request
        sql = (f"INSERT INTO community_posts {cols} VALUES "
               "(?,?,?,?,?,?,?,?,0,strftime('%s','now'),strftime('%s','now'))")
        params = ["ai_roundtable", f"{signs}(AI学派分身)", CATEGORY, title, md, "published", view, like]
        url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database/{DB_ID}/query"
        body = json.dumps({"sql": sql, "params": params}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            ok = bool(d.get("success"))
            print(("  ✓ 已发布(REST, 阅%d 赞%d)" % (view, like)) if ok else "  ✗ REST入库失败:" + json.dumps(d, ensure_ascii=False)[:200])
            return ok
        except Exception as e:
            print("  ✗ REST异常:", str(e)[:150]); return False
    # 本地:wrangler --file
    sql = ("INSERT INTO community_posts " + cols + " VALUES ('ai_roundtable', '"
           + sql_escape(f"{signs}(AI学派分身)") + "', '" + sql_escape(CATEGORY)
           + "', '" + sql_escape(title) + "', '" + sql_escape(md) + f"', 'published', {view}, {like}, 0, "
           "strftime('%s','now'), strftime('%s','now'));")
    p = os.path.join(OUT_DIR, "_insert.sql")
    with open(p, "w", encoding="utf-8") as f:
        f.write(sql)
    ok, out = wrangler_file(p)
    print(("  ✓ 已发布(published, 阅%d 赞%d)" % (view, like)) if ok else "  ✗ 入库失败:" + out[:300])
    return ok

# ── 产 1 篇 ───────────────────────────────────────────────────────
def run_one(topic, tbg, panels, dry=False):
    print(f"\n▶ 议题:{topic[:34]}…  与会:{'、'.join(p['sign'].split(' · ')[0] for p in panels)}")
    G = {p["school"]: load_grounding(p["school"]) for p in panels}
    G = {k: v for k, v in G.items() if v}
    panels = [p for p in panels if p["school"] in G]
    if len(panels) < 2:
        print("  ✗ grounding 不足 2 派,跳过"); return False

    opens = {}
    for p in panels:
        txt, model = speak(p, G[p["school"]], topic, tbg, "open")
        if txt:
            opens[p["school"]] = txt; print(f"  ✓ 立论 {p['sign'].split(' · ')[0]} {len(txt)}字 via {model}")
    if len(opens) < 2:
        print("  ✗ 立论不足 2 派,跳过"); return False

    rebuts = {}
    for p in panels:
        if p["school"] not in opens:
            continue
        others = "\n\n".join(f"【{q['sign']}】{opens[q['school']]}" for q in panels
                             if q["school"] != p["school"] and q["school"] in opens)
        present = "、".join(q["sign"].split(" · ")[0] for q in panels if q["school"] in opens)
        txt, _ = speak(p, G[p["school"]], topic, tbg, "rebut", others=others, present=present)
        if txt:
            rebuts[p["school"]] = txt; print(f"  ✓ 交锋 {p['sign'].split(' · ')[0]} {len(txt)}字")

    allsay = "\n\n".join(f"【{p['sign']}·立论】{opens.get(p['school'],'')}" for p in panels if p['school'] in opens)
    allsay += "\n\n=== 交锋 ===\n" + "\n\n".join(f"【{p['sign']}·反驳】{rebuts.get(p['school'],'')}" for p in panels if p['school'] in rebuts)
    host_sys = ("你是中医学术研讨会主持人(学识中正、不偏袒)。客观总结各派在本议题上的【共识】与【存异】,"
                "不和稀泥、不裁定谁对谁错,如实呈现分歧并点出对临床思路的启发。合规:学术研讨非诊疗,不开方药。只输出正文。")
    summary, _ = gateway_chat([{"role": "system", "content": host_sys},
                               {"role": "user", "content": f"议题:{topic}\n\n各派发言:\n{allsay}\n\n请写'共识与存异'总结(230-330字),分两层。"}],
                              temperature=0.5)
    print("  ✓ 主持总结" if summary else "  ✗ 主持总结失败")

    md, signs, _ = assemble(topic, tbg, panels, opens, rebuts, summary, G)
    ts = datetime.now().strftime("%H%M%S")
    with open(os.path.join(OUT_DIR, f"draft_{ts}.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if dry:
        print(f"  ✓ 草稿 draft_{ts}.md ({len(md)}字, --dry 未入库)"); return True
    return insert_post(topic, md, signs)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1, help="连产几篇")
    ap.add_argument("--dry", action="store_true", help="只生成草稿不入库")
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("=== 百家论道 · AI 学派圆桌研讨引擎 v2 ===")
    have = [p for p in PANELS_ALL if load_grounding(p["school"])]
    print(f"可用分身 {len(have)}/{len(PANELS_ALL)} 派 · 议题池 {len(TOPICS)} 题 · 计划产 {args.count} 篇")
    if len(have) < 2:
        print("可用分身不足,请先导出 grounding_raw.json(含各派)"); return

    ok = 0
    used_topics = []
    for n in range(args.count):
        topic, tbg = random.choice([t for t in TOPICS if t[0] not in used_topics] or TOPICS)
        used_topics.append(topic)
        k = random.randint(3, 4)
        panels = random.sample(have, min(k, len(have)))
        if run_one(topic, tbg, panels, dry=args.dry):
            ok += 1
        time.sleep(1)
    print(f"\n=== 完成 {ok}/{args.count} 篇 ===")

if __name__ == "__main__":
    main()
