#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容工厂公共底座 —— D1 访问 + 免费池调用 + 宽容 JSON 解析

**为什么要有这个文件**(2026-08-02 血证):同一个「JSON 解析」根因一天绊了三次,
  根子是仓里同类逻辑有多份副本、我只改手边那份:
    · herb_factory 的对象解析器改成 raw_decode 后,`expand_topics` 里**另一份**数组解析器
      仍用 rfind(']'),扩题稳定失败 → 降级回 OCR 碎词 → 90 个候选 84 个被判否;
    · 同一文件还自带一份 CJK 区间字面量,被 CI 的 test_cjk_charset 抓出来。
  所以:解析/调用这类会被多处复用的逻辑,**只准有一份**,新脚本一律 import 这里,
  不许再抄一份进自己文件。

铁律(与产线一致):
  · 只走内部免费池:内部网关 + OpenCode 免密端点,**严禁任何按量计费源**
  · OpenCode 只能从 GitHub Actions(独立 IP)或本机调,CF Workers 出口固定 429
  · 零 R2:一切素材从 D1 取,绝不 list/读 R2
"""
import os, sys, json, uuid, urllib.request

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
D1_DB      = os.environ.get("D1_DATABASE_ID", "")
D1_TOKEN   = os.environ.get("D1_API_TOKEN", "")
GATEWAY    = os.environ.get("GW_URL", "https://gufangai.com/api/gateway/chat")
# 免费池经网关轮到某些**思考型**模型时,会把整个 token 预算烧在复述任务要求上,
# 答案根本没写出来(2026-08-02 日志实证)。OpenCode 的这两个实测直接给结果。
OPENCODE_MODEL     = os.environ.get("CF_OPENCODE_MODEL", "deepseek-v4-flash-free")
OPENCODE_MODEL_ALT = os.environ.get("CF_OPENCODE_MODEL_ALT", "big-pickle")
# 网关调用必须带浏览器 UA —— 裸 python-urllib 的 UA 会被 403(2026-08-02 差点误判成"池子全挂")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126"


def d1(sql, params=None):
    if not (D1_TOKEN and CF_ACCOUNT and D1_DB):
        sys.exit("缺 D1_API_TOKEN / CF_ACCOUNT_ID / D1_DATABASE_ID")
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


def q(v):
    """SQL 字面量转义。列表/字典自动转 JSON 串。"""
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    return "'" + str(v).replace("'", "''") + "'"


def ask(system, user, timeout=120, max_tokens=2600, supplier=None,
        source="content_factory", json_mode=True, temperature=None, no_fallback=False,
        model=None):
    """走内部免费池网关。supplier 可点名某家(失败仍按容错链兜)。

    【2026-08-05 实测抓到的大 bug】`json` 此前是**写死 True** 的,而网关看到它就注入
      `response_format: {type: json_object}` —— **强制模型只能输出 JSON**。
      内容工厂要 JSON,没问题;但**改进者要的是提示词全文(纯文本)**,
      被强制成 JSON 模式后只能吐空 —— 赛马实测:nvidia / dashscope / openrouter
      三家全是「产出为空或过短」,而 zhipu 吐了 5613 字(它没严格遵守 json 约束)。
      **不是模型写不出来,是我把它的嘴堵上了。**
      这很可能就是两个多月来「挑战者永远 0 分」的真因之一。
    所以加 json_mode 参数:默认 True 保持产线原样,要纯文本的显式传 False。
    """
    payload = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        # 【Phase 0 · A 锁随机】温度此前写死 0.3 —— 判分是确定性的,生成却是随机的,
        #   同一份提示词两轮考出 0.5198 / 0.5158。**尺子没坏,是被考生的随机性晃的。**
        #   评测态一律传 temperature=0;产线不传,保持原样 0.3。
        "temperature": 0.3 if temperature is None else float(temperature),
        "json": bool(json_mode), "source": source,
    }
    if no_fallback:
        # 评测期间禁止容错链切换 —— 换了供应商就等于换了考生,分数不可比。
        # 容错链在生产是优点,在评测是污染源(同 GPT 对评测路径的判决)。
        payload["fallback"] = False
    if supplier:
        payload["supplier"] = supplier
    if model:
        # 网关 chat.js 早就支持 body.model → model_override(同一 provider 换模型)。
        # 【2026-08-07 接上白名单】没有这个参数时,一个 supplier 只能跑它写死的那个
        # 模型 —— 于是 192 个注册模型里能被点名的只有十几个,而实测可用的有 90 个。
        # scripts/model_pool_whitelist.json 建成 13 天全仓零引用,根因就在这:
        # **不是没探活,是探活结果没有入口能喂进来。**
        payload["model"] = model
    req = urllib.request.Request(
        GATEWAY, method="POST", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    txt = j.get("text") or ""
    if not txt and j.get("choices"):
        txt = (j["choices"][0].get("message") or {}).get("content", "")
    return txt, (j.get("model") or j.get("supplier") or "")


def ask_opencode(system, user, timeout=120, max_tokens=3000, need="["):
    """直连 OpenCode 免密端点(免费池内的一员,零成本)。

    两个实测要点:
      · **带 system 消息**时 deepseek 会进思考模式(content 空、正文落在 reasoning_content,
        而且是一段自然语言推理);把 system 并进单条 user 消息它就直接给结果。
      · 正文里没有期望的起始符就换 big-pickle 再来一次,**不拿思考文本充数**。
    认证靠五个身份头,缺一即 401 —— 它不用 API key。
    """
    merged = (system or "") + "\n\n" + (user or "")
    last = ""
    for model in (OPENCODE_MODEL, OPENCODE_MODEL_ALT):
        body = json.dumps({
            "model": model, "max_tokens": max_tokens, "temperature": 0.3,
            "messages": [{"role": "user", "content": merged}],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://opencode.ai/zen/v1/chat/completions", method="POST", data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "opencode/1.0.0",
                "x-opencode-client": "opencode",
                "x-opencode-project": "default",
                "x-opencode-session": str(uuid.uuid4()),
                "x-opencode-request": str(uuid.uuid4()),
            })
        j = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        m = (j.get("choices") or [{}])[0].get("message") or {}
        txt = (m.get("content") or "").strip()
        if need in txt:
            return txt, model
        last = txt or (m.get("reasoning_content") or "").strip()
    return last, OPENCODE_MODEL_ALT


def ask_best(system, user, **kw):
    """先 OpenCode,不通再回落网关。产线默认走这个。"""
    need = kw.pop("need", "[")
    try:
        return ask_opencode(system, user, need=need, **{k: v for k, v in kw.items()
                                                        if k in ("timeout", "max_tokens")})
    except Exception:
        return ask(system, user, **{k: v for k, v in kw.items()
                                    if k in ("timeout", "max_tokens", "supplier", "source")})


def _unfence(t):
    """剥掉 ```json 围栏。对象与数组两个解析器共用,别再各写一份。"""
    t = (t or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        t = parts[1] if len(parts) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    return t


def parse_json(t):
    """取出**第一个完整**的 JSON 对象。

    治实测最大失败源:模型吐完 `{"invalid": true, ...}` 后面还跟解释文字或第二个对象,
    老写法用 rfind('}') 把尾巴一起吞进来 → "Extra data"。raw_decode 到第一个对象闭合就收手。
    """
    t = _unfence(t)
    a = t.find("{")
    if a < 0:
        raise ValueError("模型输出里没有 JSON 对象:" + t[:80])
    try:
        obj, _ = json.JSONDecoder().raw_decode(t, a)
        return obj
    except json.JSONDecodeError:
        b = t.rfind("}")
        if b > a:
            return json.loads(t[a:b + 1])
        raise


def parse_json_array(t, quiet=False):
    """取出 JSON **数组**。失败返回空表。

    关键(2026-08-02 诊断实证):有的模型会把思考过程当答案输出,先复述一遍
      「硬性要求…每项形如 ["名称","学名或英文名"]」,真答案在最后面。
      只取第一个数组 → 取到的是复述里的**占位符示例** → 恒为 0。
    所以:扫描全文所有数组候选,**剔除嵌在别人里面的**(内层可能比外层长,不能简单取最长),
      在顶层候选里取最长;整体解析不了再逐项抢救。
    """
    t = _unfence(t)
    a = t.find("[")
    if a < 0:
        b = t.find("{")
        if b >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(t, b)
                if isinstance(obj, dict) and obj and "error" not in obj:
                    return [[str(k), str(v)] for k, v in obj.items() if k]
            except json.JSONDecodeError:
                pass
        if not quiet:
            print(f"  [解析] 模型没吐出 JSON 数组,原文前120字: {t[:120]!r}", flush=True)
        return []

    dec = json.JSONDecoder()
    cands = []
    i = a
    while i >= 0:
        try:
            cand, end = dec.raw_decode(t, i)
            if isinstance(cand, list):
                cands.append((i, end, cand))
        except json.JSONDecodeError:
            pass
        i = t.find("[", i + 1)
    top = [c for c in cands if not any(o[0] < c[0] and c[1] <= o[1] for o in cands)]
    if top:
        return max(top, key=lambda c: len(c[2]))[2]

    out, i = [], a + 1
    while i < len(t):
        while i < len(t) and t[i] in ' ,\n\r\t':
            i += 1
        if i >= len(t) or t[i] == ']':
            break
        try:
            obj, i = dec.raw_decode(t, i)
            out.append(obj)
        except json.JSONDecodeError:
            break
    if out and not quiet:
        print(f"  [解析] 数组不完整,逐项抢救出 {len(out)} 条", flush=True)
    return out
