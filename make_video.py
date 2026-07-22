# -*- coding: utf-8 -*-
# make_video.py v2 (2026-07-20) — audio-first pipeline, fixes the old "floating subtitle" problem.
#
# Pipeline: real expert-persona API (52 grounded historical-doctor personas, /api/ai/expert on the
# live production site) -> nova-gateway rewrite into a short spoken script -> edge-tts TTS that
# ALSO returns real word-level timestamps (WordBoundary events) -> ASS karaoke subtitles built from
# those exact timestamps (word highlights precisely when it is actually spoken, not a guessed/evenly
# -split overlay) -> real rare-book page images (fetched straight from 123 pan via a D1 lookup, see
# fetch_images()) with varied Ken-Burns (not one identical zoom for every clip) + xfade cross-fades
# between clips (not a hard cut) -> burn subs -> mux
# voice -> automated quality gate (duration / black-frame / non-empty subtitle checks) before it is
# allowed to land in R2 `_video/`.
#
# 2026-07-21: render_body_clips() now joins clips with ffmpeg's xfade filter (fade/fadeblack/
# dissolve/wipeleft, rotated by clip index -- see TRANSITIONS below) instead of the old "-f concat
# -c copy" hard cut. Clip durations are padded up front to exactly compensate for the overlap xfade
# introduces, so the total slide.mp4 length (and therefore subtitle/audio sync via burn_and_mux's
# "-shortest" mux) is unchanged from the hard-cut version. See render_body_clips() for the offset math.
#
# Free-only resource stack: nova-gateway (proxies modelscope/sensenova/agnes/gemini/groq/nvidia/
# siliconflow — verified via GET / health probe, no Cloudflare Workers AI anywhere) + the production
# /api/ai/expert endpoint (uses guyaofang-web's own functions/api/gateway/_providers.js free
# fallback chain when called with a non-paid model) + edge-tts (free, Microsoft). No paid API keys
# used in this script.
#
# Source images: fetched directly from 123 pan (D1 books_assets_v2.pan_dir_id -> 123 file list ->
# download), NOT through guyaofang-web's public /api/reader endpoint -- that endpoint serves real
# paying readers behind a paywall gate and carries real production traffic already (~86k/month 403
# hits per a 2026-07-20 CF traffic check); an internal batch job like this has no business adding
# load there. This mirrors the D1-query shape already used in this repo's sync.py and the 123
# list/download sequence already used in guyaofang-web/functions/api/_lib/pan123.js, rather than
# inventing a new access pattern.
import os, re, sys, json, time, random, subprocess, urllib.request, urllib.error, ssl, asyncio
import boto3
import edge_tts

NOVA_URL = "https://nova-gateway.hosonzuo.workers.dev"
NOVA_KEY = os.environ["NOVA_KEY"]
EXPERT_API = "https://www.gufangai.com/api/ai/expert"

# S_* is R2 -- only used for the *output* video upload now (_video/ writes), never for reading
# source page images (see fetch_images() below: those come straight from 123 pan + a D1 lookup).
S_EP = os.environ["S_EP"]; S_AK = os.environ["S_AK"]; S_SK = os.environ["S_SK"]; S_BUCKET = os.environ["S_BUCKET"]
BOOK = os.environ.get("BOOK", "ylgc_2")
# 5 pages is just a sane clip length default now, not a platform gate (fetch_images() no longer
# goes through the guest-metered public reader API -- see the 2026-07-20 note on fetch_images()).
PAGES = [int(x) for x in os.environ.get("PAGES", "1,2,3,4,5").split(",")]
VOICE = os.environ.get("VOICE", "zh-CN-XiaoxiaoNeural")
# cross-fade duration between body clips (render_body_clips, 2026-07-21). Kept short/subtle on
# purpose to suit solemn classical-text narration -- see render_body_clips() for the offset math
# and why this is always safe against the seg=max(2.4, ...) per-clip floor below.
TRANS_DUR = float(os.environ.get("TRANS_DUR", "0.5"))

# ---- platform / aspect adaptation (2026-07-23) ------------------------------------------------
# RATIO lets one narration be published to different social platforms without re-recording:
#   "vertical"   -> 9:16  1080x1920  (Douyin / Kuaishou / WeChat Channels -- the primary battlefield;
#                                     byte-for-byte the pipeline's original hard-coded shape)
#   "horizontal" -> 16:9  1920x1080  (Bilibili)
# Everything downstream (Ken-Burns canvas, ASS PlayRes, the R2 output key) is now derived from
# VID_W/VID_H instead of the old literal 1080/1920, so adding a ratio never alters the vertical
# path's output. BURN_SUB toggles whether the karaoke subtitles are burned into the pixels
# (default on; a "clean master" with no burned subs is sometimes wanted so a creator can add their
# own per-platform captions).
RATIO = os.environ.get("RATIO", "vertical").strip().lower()
if RATIO in ("horizontal", "landscape", "h", "16:9", "169"):
    RATIO = "horizontal"; VID_W, VID_H = 1920, 1080
else:
    RATIO = "vertical"; VID_W, VID_H = 1080, 1920
BURN_SUB = os.environ.get("BURN_SUB", "true").strip().lower() not in ("false", "0", "no", "off")

# Curated rotation so the cron'd (unattended) runs don't produce the same clip every day.
# Each tuple is (expert_key matching /api/ai/expert's EXPERTS map, display name, a symptom/pulse
# -pattern CASE phrasing — /api/ai/expert's prompt contract wants something diagnosable, not a bare
# historical-trivia sentence). expert_key values verified against the live GET /api/ai/expert roster.
ROTATION = [
    ("zhongjing", "张仲景", "患者恶寒发热、无汗而喘、头项强痛、脉浮紧,试参阅古籍方证分析此案"),
    ("yetianshi", "叶天士", "患者身热夜甚、口渴不欲多饮、舌绛少苔、脉细数,试参阅古籍方证分析此温病案"),
    ("zhudanxi", "朱丹溪", "患者形瘦颧红、五心烦热、盗汗、舌红少津、脉细数,试参阅古籍方证分析此阴虚案"),
    ("lidongyuan", "李东垣", "患者神疲肢倦、少气懒言、食后腹胀、大便溏薄、脉虚弱,试参阅古籍方证分析此脾胃气虚案"),
    ("lishizhen", "李时珍", "患者久咳痰多色白、胸闷纳呆、舌苔白腻、脉滑,试从本草与脾胃痰湿角度参阅古籍方证分析此案"),
]

_ek = os.environ.get("EXPERT_KEY", "auto")
if _ek and _ek != "auto":
    EXPERT_KEY = _ek
    EXPERT_NAME = os.environ.get("EXPERT_NAME") or "张仲景"
    TOPIC = os.environ.get("TOPIC") or ROTATION[0][2]   # `or`, not .get(default) — the workflow
    # always sets the TOPIC env var (possibly to ""), so a plain .get() default would never trigger.
else:
    import datetime as _dt
    _idx = _dt.date.today().timetuple().tm_yday % len(ROTATION)
    EXPERT_KEY, EXPERT_NAME, TOPIC = ROTATION[_idx]
    print(f"[rotation] EXPERT_KEY=auto -> day-of-year pick #{_idx}: {EXPERT_KEY}/{EXPERT_NAME}", flush=True)

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"
s3 = boto3.client("s3", endpoint_url=S_EP, aws_access_key_id=S_AK, aws_secret_access_key=S_SK, region_name="auto")

# free-only models accepted by /api/ai/expert (must avoid its PAID set, which needs login/vipcode)
FREE_EXPERT_MODELS = ["sensenova", "longcat", "zhipu", "agnes", "cerebras", "modelscope"]


def sh(cmd, check=True):
    print("+ " + " ".join(str(c) for c in cmd[:8]) + (" ..." if len(cmd) > 8 else ""), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout[-2000:], flush=True)
    if r.returncode != 0:
        print("STDERR TAIL:", (r.stderr or "")[-3000:], flush=True)
        if check:
            raise SystemExit(f"command failed ({r.returncode}): {' '.join(str(c) for c in cmd[:4])}")
    return r


def http_json(method, url, body=None, headers=None, timeout=100):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={**(headers or {}), "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


def http_bytes(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


# ---------- 1. real grounded content: call the production expert-persona API ----------
def call_expert(q, expert=EXPERT_KEY):
    last_err = None
    for m in FREE_EXPERT_MODELS[:3]:   # cap attempts so one bad title doesn't eat the whole job budget
        try:
            status, j = http_json("POST", EXPERT_API, {"expert": expert, "model": m, "q": q}, timeout=100)
            if status == 200 and j.get("ok") and j.get("analysis"):
                print(f"[expert] ok via model={m} engine={j.get('engine')}", flush=True)
                return j
            last_err = f"{m}: http{status} {str(j)[:150]}"
        except Exception as e:
            last_err = f"{m}: {str(e)[:150]}"
        print("[expert] attempt failed:", last_err, flush=True)
    raise SystemExit(f"expert API all attempts failed: {last_err}")


def _rewrite_via_nova(analysis, expert_name):
    a = analysis["analysis"]
    fang = a.get("古籍方证") or []
    fang0 = fang[0] if fang else {}
    raw = (
        f"辨证研讨:{a.get('辨证研讨', '')}\n"
        f"病机阐释:{a.get('病机阐释', '')}\n"
        f"代表方:{fang0.get('方名', '')} 出处:{fang0.get('出处', '')} {fang0.get('文献所述', '')}"
    )[:1200]
    prompt = (
        "把下面这段中医专家对医案的辨证研讨文字,改写成一段适合15-25秒竖屏短视频口播的解说词。"
        "要求:①第一句要有钩子、制造悬念或反差,别用「大家好」这类开场白;"
        "②中间用现代白话讲清楚这个病案古籍怎么判、点一个具体方名和它的出处书名;"
        "③结尾自然带一句「古方AI星图,请AI专家分身为你解读古籍」;"
        "④纯口播文字90-130字,不要emoji、不要分行、不要任何分镜/旁白标注,直接给能念出来的话,标点只保留逗号和句号;"
        "⑤【硬性红线,一个字都不能犯】只准输出最终能直接照着念的口播正文本身,绝不能输出任何思考过程、"
        "自我检查、自我提示、写作步骤说明这类念不得的「元话术」——凡是「此处需」「需要校验」「需先核实」"
        "「接下来我」「让我来」「作为一个AI」「以上内容仅供参考」这类第一人称/自我指令/自检措辞,一个字都"
        "不该出现在输出里;这些是你自己的草稿念头,不是讲给观众听的话。\n\n"
        f"研讨者:{expert_name}\n{raw}"
    )
    body = {"model": "auto", "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500, "temperature": 0.7}
    status, j = http_json("POST", NOVA_URL + "/v1/chat/completions", body,
                           headers={"Authorization": "Bearer " + NOVA_KEY}, timeout=60)
    text = j["choices"][0]["message"]["content"].strip()
    if not text:
        raise RuntimeError("nova-gateway returned empty content")
    return text


def _local_compose_script(analysis, expert_name):
    """No-extra-AI-call fallback: compose the narration directly from the expert API's own
    grounded text (its 辨证研讨 field is already required by expert.js's prompt contract to be
    ≥150 chars of 现代白话/modern vernacular), so it still reads naturally aloud with zero extra
    network dependency. Used when nova-gateway is unreachable (see 2026-07-20 401 note below)."""
    a = analysis["analysis"]
    bz = (a.get("辨证研讨") or "").strip()
    fang = a.get("古籍方证") or []
    fang0 = fang[0] if fang else {}
    fname, source = fang0.get("方名", ""), fang0.get("出处", "")
    body = bz
    if len(body) > 60:
        cut = body[:65]
        # 改非贪婪:只截到窗口内第一个句末标点为止,不再贪到最后一个。
        # 2026-07-21事故(lishizhen/二陈汤那条run):bz的前两句都落在这65字窗口内,旧的贪婪`.*`
        # 会一路匹配到窗口里最后一个"。",把两句粘成一句——恰好第2句是模型自己漏出来的自检话术
        # ("此处需先校验痰湿证对应的药物是否准确"),原样混进了body。这里原本的意图就是只取第一句
        # (看下面紧跟着的"认为,{body}"措辞就知道)。下面的 _strip_meta_commentary() 是第二道、
        # 各自独立的防线,防的是元话术万一是第1句而不是第2句这种情况。
        m = re.search(r"^(.*?[。!?])", cut)
        body = m.group(1) if m else (cut + "。")
    mid = f"{expert_name}认为,{body}"
    if fname:
        mid += f"可参{fname}" + (f",出自{source}。" if source else "。")
    text = f"这则医案,{expert_name}会怎么判?" + mid + "古方AI星图,请AI专家分身为你解读古籍。"
    return text


# ---------- 第二道防线:不管上面哪条路径产出的文本(nova-gateway改写 或 本地fallback拼装),
# 只要漏进了AI自检/元话术句子,这里统一拦。真实事故:2026-07-21,expert_key=lishizhen/二陈汤那条run
# (R2 key _video/yxf07_lishizhen_1784495237.mp4),拼出来的口播是"...一派痰湿内盛之象。李时珍思维
# 强调先辨药后辨证，此处需先校验痰湿证对应的药物是否准确。可参二陈汤..."——中间那句不是专家人设该说
# 的话,是底层模型把自己"要不要按指令自检"的内心戏当正文说出来了(expert.js的TAIL系统提示词是一份
# 很长、条条框框很密的硬指标合同,免费档模型有时会把这种"合规自检"漏进内容字段本身;而
# _local_compose_script() 是直接切上游这个字段、根本没走LLM改写,所以单靠改prompt治不了这条路径——
# 上面那处非贪婪正则修复是针对同一起事故的第一道、更窄的防线)。_rewrite_via_nova() 里的prompt加固
# 是第一道防线(只覆盖nova改写这条路径,而且得模型真听话才有效);这里的逐句过滤是第二道防线,扎在
# 两条路径都必经的唯一卡口上,即使prompt失守,产线仍然安全。
_META_PATTERNS = re.compile("|".join([
    r"此处(应|需|须)", r"(需|须)(先|要)?(校验|核验|核实|确认)", r"接下来(我|,|，|要|将)",
    r"让我(来|们)?", r"我(认为|觉得)(需要|应该)", r"本段(应|需|须)",
    r"以上(内容|文字)(仅|只)供", r"作为(一个|一名)?(AI|人工智能|语言模型|助手)",
    r"^(好的|好,|好，|嗯,|嗯，)", r"字数(要求|限制)", r"根据(用户|上文|上述|题目)(要求|指示)",
    r"(需要|应)(重新)?(生成|改写|润色)", r"是否准确", r"是否合适", r"是否恰当",
    r"<think", r"思考过程",
]))


def _strip_meta_commentary(text):
    """按 。!? 切句(标点留在前一句尾),凡命中 _META_PATTERNS 的句子整句丢弃,剩下的重新拼接。
    返回 (清洗后文本, 被丢弃的句子列表)。绝不返回空字符串:万一所有句子都命中(正常情况下不该发生——
    这些pattern写得很窄),就整段原样放行、不清洗,好过上传一条空/近空的口播;但这种情况下返回空的
    dropped 列表,调用方那行日志就能看出"这次没清洗成功",不会悄无声息地放水。"""
    parts = [p for p in re.split(r"(?<=[。!?])", text) if p.strip()]
    kept, dropped = [], []
    for p in parts:
        (dropped if _META_PATTERNS.search(p) else kept).append(p)
    cleaned = "".join(kept).strip()
    if not cleaned:
        return text, []
    return cleaned, dropped


def gen_script_from_expert(analysis, expert_name):
    try:
        text = _rewrite_via_nova(analysis, expert_name)
        print("[script] source=nova-gateway-rewrite", flush=True)
    except Exception as e:
        # 2026-07-20: nova-gateway (nova-gateway.hosonzuo.workers.dev) is returning 401 Unauthorized
        # with the current NOVA_KEY secret (confirmed independently outside this run too — looks
        # like the worker's key rotated and this side-repo's secret was never updated). Rather than
        # fail the whole video on a side-gateway that isn't this task's real dependency, fall back
        # to composing the narration directly from the expert API's own grounded text.
        print("[script] nova-gateway rewrite failed, using local fallback composition:", str(e)[:200], flush=True)
        text = _local_compose_script(analysis, expert_name)
    text, dropped = _strip_meta_commentary(text)
    if dropped:
        print(f"[script] 元话术过滤器丢弃了{len(dropped)}句: {dropped}", flush=True)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)   # strip emoji
    text = re.sub(r"[ \t\n]+", "", text)
    text = re.sub(r"[\"'“”]", "", text)
    return text[:160]   # hard safety cap regardless of source (~25-30s of narration at this voice/rate)


# ---------- 2. audio-first: TTS that also returns real per-word timestamps ----------
def tts_with_word_timestamps(text, voice=VOICE):
    async def _run():
        communicate = edge_tts.Communicate(text, voice, rate="+8%", boundary="WordBoundary")
        audio = bytearray()
        words = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["text"], chunk["offset"] / 10_000_000, (chunk["offset"] + chunk["duration"]) / 10_000_000))
        return bytes(audio), words
    audio_bytes, words = asyncio.run(_run())
    if not audio_bytes or not words:
        raise SystemExit(f"edge-tts returned no audio/words (audio={len(audio_bytes)}B words={len(words)})")
    open("voice.mp3", "wb").write(audio_bytes)
    print(f"[tts] {len(audio_bytes)} bytes, {len(words)} word-boundary events, "
          f"last word ends {words[-1][2]:.2f}s", flush=True)
    return words


# ---------- 3. ASS karaoke subtitles built from the REAL timestamps above ----------
def ass_ts(t):
    t = max(0.0, t)
    hh = int(t // 3600); mm = int((t % 3600) // 60); ss = t % 60
    return f"{hh}:{mm:02d}:{ss:05.2f}"


def build_ass(word_timings, out_path="subs.ass", w=1080, h=1920, offset_s=0.0, max_chars=14):
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # PrimaryColour = &H0000D7FF (gold, BGR) = the "already spoken" highlight colour a \k switches TO.
        # SecondaryColour = &H00FFFFFF (white) = the "not yet spoken" colour shown before the wipe reaches it.
        "Style: KA,Noto Sans CJK SC,70,&H0000D7FF,&H00FFFFFF,&H00202020,&HB0000000,1,0,0,0,100,100,0,0,"
        "1,4,2,2,60,60,170,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (w, h)

    PUNCT = tuple("，。!?、,.!?")
    lines = []
    chunk = []
    chunk_chars = 0

    def flush(buf):
        if not buf:
            return None
        start = buf[0][1] + offset_s
        end = buf[-1][2] + offset_s + 0.15
        parts = []
        prev_end = buf[0][1]
        for (txt, ws, we) in buf:
            dur_cs = max(1, round((we - prev_end) * 100))
            safe_txt = txt.replace("{", "").replace("}", "")
            parts.append("{\\k%d}%s" % (dur_cs, safe_txt))
            prev_end = we
        return "Dialogue: 0,%s,%s,KA,,0,0,0,,%s" % (ass_ts(start), ass_ts(end), "".join(parts))

    for (txt, ws, we) in word_timings:
        chunk.append((txt, ws, we))
        chunk_chars += len(txt)
        if chunk_chars >= max_chars or txt.endswith(PUNCT):
            ln = flush(chunk)
            if ln:
                lines.append(ln)
            chunk, chunk_chars = [], 0
    if chunk:
        ln = flush(chunk)
        if ln:
            lines.append(ln)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    print(f"[ass] wrote {len(lines)} karaoke subtitle lines -> {out_path}", flush=True)
    return len(lines)


def build_plain_ass(word_timings, out_path="subs_plain.ass", w=1080, h=1920, offset_s=0.0, max_chars=14):
    """Fallback with no \\k tags at all — used only if the karaoke burn attempt fails."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: PL,Noto Sans CJK SC,70,&H00FFFFFF,&H00FFFFFF,&H00202020,&HB0000000,1,0,0,0,100,100,0,0,"
        "1,4,2,2,60,60,170,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (w, h)
    lines, chunk, chunk_chars = [], [], 0
    PUNCT = tuple("，。!?、,.!?")
    for (txt, ws, we) in word_timings:
        chunk.append((txt, ws, we)); chunk_chars += len(txt)
        if chunk_chars >= max_chars or txt.endswith(PUNCT):
            start = chunk[0][1] + offset_s; end = chunk[-1][2] + offset_s + 0.15
            text = "".join(c[0] for c in chunk).replace("{", "").replace("}", "")
            lines.append("Dialogue: 0,%s,%s,PL,,0,0,0,,%s" % (ass_ts(start), ass_ts(end), text))
            chunk, chunk_chars = [], 0
    if chunk:
        start = chunk[0][1] + offset_s; end = chunk[-1][2] + offset_s + 0.15
        text = "".join(c[0] for c in chunk).replace("{", "").replace("}", "")
        lines.append("Dialogue: 0,%s,%s,PL,,0,0,0,,%s" % (ass_ts(start), ass_ts(end), text))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return len(lines)


# ---------- 4. real rare-book page images (unique asset — keep this, it is our differentiator) ----------
# History (all 2026-07-20, same day): the old `book/{BOOK}/page_NNNN.webp` R2 key this used to read
# (raw boto3 get_object) 404s for every page -- that key prefix looks like a one-off manual mirror
# that just went stale, not the platform's real convention. Fix #1 routed image fetches through the
# public production reader API (/api/reader/{book_code}/page) instead -- worked, but the founder
# caught it as the wrong landing spot: that endpoint serves real paying readers (paywall gate,
# ~86k/month 403 hits on it already per a CF traffic check) and an internal batch job has no
# business adding load there. Fix #2 (this one): fetch straight from the real storage via a D1
# lookup, bypassing guyaofang-web's API entirely -- mirrors the D1-lookup pattern already used
# elsewhere in this repo (sync.py's _rebuild_pages_from_d1 for the HTTP API call shape) and the
# exact two-branch source logic guyaofang-web's functions/api/reader/[book_code]/page.js itself
# uses: pan_dir_id set -> fetch from 123 pan (same token/list/download_info/download sequence as
# functions/api/_lib/pan123.js's fetchPageFrom123, same PAN_CLIENT_ID/PAN_CLIENT_SECRET pair);
# pan_dir_id NOT set -> that book hasn't been migrated off R2 yet, read {webp_prefix}page_NNNN.webp
# from R2 instead (real 2026-07-20 D1 query: only ~88.5%, 46,361/52,402 upload_status='done' rows,
# have pan_dir_id set -- the R2-to-123 migration is real but not total, and this script's own
# default test book, ylgc_2, happens to be one of the ~6,000 not-yet-migrated ones). Mirroring both
# branches instead of assuming one keeps this correct for either kind of book.
#
# 2026-07-22 reverification (re-ran the same D1 query 2 days later, not a guess): migration crept
# from 88.5% to ~88.9% (46,595/52,402 upload_status='done' rows now have pan_dir_id; 5,807 still
# don't) -- "real but not total" above still holds, it did NOT flip to 100% in the interim. Breaking
# that gap down by top-level R2 prefix (books_assets_v2.webp_prefix) matters here because ylgc_2
# (this script's default BOOK) lives under gufang/ (domestic classics library), NOT book/ (the
# overseas-medical-books folder that other parts of this project describe as "already fully migrated
# off R2, bucket emptied 2026-07-17"): book/ 1,388/22,109 still NULL, gufang/ 917/946 still NULL
# (ylgc_2's own folder is barely migrated at all), naj/ 3,402/29,096 still NULL. So "the guyaofang-lib
# R2 bucket is already emptied" is not accurate as a blanket statement for any of these three folders
# yet, book/ included. Live proof, not just the D1 count: the 2026-07-22 07:44 UTC scheduled run of
# this very script (run 29901193285) read gufang/ylgc_2/page_000{1..5}.webp straight off R2 with zero
# "img miss" lines and produced a passing-quality-gate video -- those R2 objects are still there and
# still readable in production right now. Do not "clean up" the R2-fallback branch below on the
# assumption the bucket is empty; re-check books_assets_v2 pan_dir_id coverage before ever doing that.
PAN_BASE = "https://open-api.123pan.com"
_pan_tok = {"v": None}


def _d1_query(sql, params=None):
    acc = os.environ.get("CF_ACCOUNT_ID"); db = os.environ.get("D1_DATABASE_ID"); tok = os.environ.get("D1_API_TOKEN")
    if not (acc and db and tok):
        raise RuntimeError("missing CF_ACCOUNT_ID/D1_DATABASE_ID/D1_API_TOKEN for D1 lookup")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/d1/database/{db}/query"
    body = {"sql": sql, "params": params or []}
    status, j = http_json("POST", url, body, headers={"Authorization": "Bearer " + tok}, timeout=60)
    if not j.get("success"):
        raise RuntimeError(f"D1 query failed: {str(j.get('errors'))[:200]}")
    return (j.get("result") or [{}])[0].get("results") or []


def _pan_token():
    if _pan_tok["v"] is None:
        cid = os.environ["PAN_CLIENT_ID"]; csec = os.environ["PAN_CLIENT_SECRET"]
        status, j = http_json("POST", PAN_BASE + "/api/v1/access_token",
                               {"clientID": cid, "clientSecret": csec},
                               headers={"Platform": "open_platform"}, timeout=60)
        _pan_tok["v"] = (j.get("data") or {}).get("accessToken")
        if not _pan_tok["v"]:
            raise RuntimeError(f"123 access_token failed: {str(j)[:200]}")
    return _pan_tok["v"]


def _pan_get(path):
    headers = {"Platform": "open_platform", "Authorization": "Bearer " + _pan_token()}
    status, j = http_json("GET", PAN_BASE + path, headers=headers, timeout=60)
    return j.get("data") or {}


def _pan_find_file_id(pan_dir_id, filename):
    """Same list-and-match-by-filename sequence as fetchPageFrom123() in guyaofang-web."""
    last_file_id = 0
    for _ in range(20):
        d = _pan_get(f"/api/v2/file/list?parentFileId={pan_dir_id}&limit=100&lastFileId={last_file_id}")
        fl = d.get("fileList") or []
        hit = next((f for f in fl if f.get("filename") == filename), None)
        if hit:
            return hit.get("fileId") or hit.get("fileID")
        last_file_id = d.get("lastFileId")
        if last_file_id in (None, -1) or not fl:
            break
    return None


def _pan_download_bytes(file_id):
    d = _pan_get(f"/api/v1/file/download_info?fileId={file_id}")
    url = d.get("downloadUrl")
    if not url:
        return None
    return http_bytes(url, timeout=60)


def _lookup_book_source(book_id):
    """Mirrors page.js's own two-branch source logic exactly, rather than assuming every book is
    already migrated to 123: 2026-07-20 diagnostics (a real dispatch run's D1 query, not a guess)
    found only 46,361 of 52,402 upload_status='done' books_assets_v2 rows (~88.5%) have pan_dir_id
    set -- the migration is real but not total, and ylgc_2 (this script's own default/test book,
    under the gufang/ folder specifically) happens to be one of the ~6,000 not-yet-migrated ones,
    still correctly served from R2 in production (reverified 2026-07-22: still not migrated, still
    served fine by a real cron run -- see the longer fetch_images() comment above for the current
    numbers, incl. the book/ vs. gufang/ vs. naj/ folder breakdown). page.js's rule: pan_dir_id set
    -> 123; otherwise -> R2 at {webp_prefix}page_NNNN.webp. Mirror both branches so this keeps
    working regardless of which side any given book is on."""
    rows = _d1_query(
        "SELECT pan_dir_id, webp_prefix, upload_status FROM books_assets_v2 WHERE book_id = ? LIMIT 1",
        [book_id])
    if not rows:
        rows = _d1_query(
            "SELECT book_id, pan_dir_id, webp_prefix, upload_status FROM books_assets_v2 "
            "WHERE upload_status = 'done' AND book_id LIKE ? LIMIT 2",
            ["%" + book_id])
        if len(rows) != 1:
            rows = []
    if not rows:
        raise SystemExit(f"book_id={book_id!r} not found in books_assets_v2 (D1) at all")
    row = rows[0]
    print(f"[source] {book_id} -> pan_dir_id={row.get('pan_dir_id')!r} webp_prefix={row.get('webp_prefix')!r}", flush=True)
    return row.get("pan_dir_id"), row.get("webp_prefix")


def fetch_images():
    pan_dir_id, webp_prefix = _lookup_book_source(BOOK)
    out = []
    for i, p in enumerate(PAGES):
        page_str = f"{p:04d}"
        filename = f"page_{page_str}.webp"
        try:
            if pan_dir_id:
                file_id = _pan_find_file_id(pan_dir_id, filename)
                if not file_id:
                    print("img miss (not found in 123 folder)", filename, flush=True)
                    continue
                b = _pan_download_bytes(file_id)
                if not b:
                    print("img miss (download_info/download failed)", filename, flush=True)
                    continue
            elif webp_prefix:
                # not yet migrated to 123 -- same R2 key page.js's own R2-fallback branch reads
                key = f"{webp_prefix}{filename}"
                b = s3.get_object(Bucket=S_BUCKET, Key=key)["Body"].read()
            else:
                print("img miss (no pan_dir_id and no webp_prefix for this book)", filename, flush=True)
                continue
            fn = f"img{i}.webp"; open(fn, "wb").write(b); out.append(fn)
        except Exception as e:
            print("img miss", filename, str(e)[:80], flush=True)
    return out


def dur(f):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", f], capture_output=True, text=True)
    return float(o.stdout.strip() or 0)


# varied Ken-Burns: cycle through distinct motion presets so clips are NOT identical (2026-07-20,
# founder feedback: don't template every image the same way).
def kb_expr(preset_i, d, fps, w=1080, h=1920):
    if preset_i % 4 == 0:      # center push-in
        z = f"1.0+0.16*on/{d}"; x = "iw/2-(iw/zoom/2)"; y = "ih/2-(ih/zoom/2)"
    elif preset_i % 4 == 1:    # top-left corner push-in
        z = f"1.0+0.14*on/{d}"; x = "0"; y = "0"
    elif preset_i % 4 == 2:    # bottom-right corner push-in
        z = f"1.0+0.14*on/{d}"; x = "iw-iw/zoom"; y = "ih-ih/zoom"
    else:                      # fixed zoom, slow pan left -> right
        z = "1.12"; x = f"(iw-iw/zoom)*on/{d}"; y = "ih/2-(ih/zoom/2)"
    # x/y expressions are all relative to iw/ih/zoom, so they are dimension-agnostic; only the
    # output canvas s=WxH changes per ratio.
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={d}:s={w}x{h}:fps={fps}"


# tasteful, restrained cross-fade rotation to match the "classical text lecture" tone (2026-07-21) --
# deliberately NOT using the flashier/technical xfade transitions (radial, pixelize, squeeze*,
# wind*, etc.) that would clash with the calm Ken-Burns push-ins/pans above. Same "% len(...)"
# rotation-by-index pattern as kb_expr's preset_i % 4, so a 5-image clip (the default PAGES count)
# cycles through all four exactly once with no repeats.
TRANSITIONS = ["fade", "fadeblack", "dissolve", "wipeleft"]


def xfade_name(transition_i):
    return TRANSITIONS[transition_i % len(TRANSITIONS)]


def render_body_clips(imgs, total_audio_s):
    fps = 30
    n = len(imgs)
    seg = max(2.4, total_audio_s / n)
    # xfade transitions overlap (n-1) cuts by trans_dur seconds each; swapping "-f concat -c copy"
    # for xfade with no compensation would land the final slide.mp4 (n-1)*trans_dur seconds SHORTER
    # than before, and burn_and_mux()'s "-shortest" mux against voice.mp3 (sized off this very
    # total_audio_s) would then silently clip the tail of the narration/subtitles. Pad every clip by
    # its even share of that overlap up front so the POST-transition total still lands on the same
    # n*seg target the pipeline always used -- i.e. subtitle/audio sync is unaffected.
    trans_n = max(0, n - 1)
    trans_dur = min(TRANS_DUR, seg * 0.3) if trans_n else 0.0  # safety clamp: never eat >30% of a clip
    seg_padded = seg + (trans_n * trans_dur / n)
    d = int(seg_padded * fps)
    clips = []
    for i, im in enumerate(imgs):
        c = f"clip{i}.mp4"
        # IMPORTANT: no "-t" on the input here. "-loop 1 -t X -i img" makes the input itself emit
        # ~X seconds of frames (at the default 25fps), and zoompan's own d= then holds/expands EACH
        # of those input frames for d more frames -- a real double-multiplication bug that was
        # caught locally on 2026-07-20 (a 5-clip run was on track to render tens of thousands of
        # frames per clip and never finish inside the job timeout). Fix: infinite -loop 1 input +
        # -frames:v {d} as an OUTPUT cap gives exactly d output frames, i.e. exactly d/fps seconds.
        # This frame-count discipline is identical for both ratios below (the horizontal branch just
        # composites a blurred-pillarbox before the same zoompan+cap).
        if RATIO == "horizontal":
            # Portrait book pages -> 16:9 via a blurred-pillarbox composite: the whole page is fit
            # and centred over a blurred+darkened enlarged copy of itself, NOT centre-cropped --
            # cropping a tall leaf to 16:9 would slice it to a thin horizontal band and throw most of
            # the page away. Ken-Burns push-in is then applied to the finished 1920x1080 composite.
            # force_original_aspect_ratio=decrease on the foreground also safely handles the odd
            # landscape double-page scan (fits inside the box either way, never overflows the frame).
            fg_w = int(VID_W * 0.97) // 2 * 2
            fg_h = int(VID_H * 0.91) // 2 * 2
            fc = (
                "[0:v]split=2[bg][fg];"
                "[bg]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                "gblur=sigma=18,eq=brightness=-0.10:saturation=0.90,setsar=1[bgb];"
                "[fg]scale=%d:%d:force_original_aspect_ratio=decrease,setsar=1[fgs];"
                "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,%s,setsar=1[vout]"
            ) % (VID_W, VID_H, VID_W, VID_H, fg_w, fg_h, kb_expr(i, d, fps, VID_W, VID_H))
            sh(["ffmpeg", "-y", "-loop", "1", "-i", im,
                "-filter_complex", fc, "-map", "[vout]", "-frames:v", str(d),
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", c])
        else:
            # vertical 9:16 -- byte-for-byte the pipeline's original behaviour (portrait page into a
            # portrait frame; a light centre crop reads as full-bleed and right). The oversized
            # canvas (1.25x = 1350x2400) gives zoompan sharpness headroom for the push-in.
            cw = int(VID_W * 1.25) // 2 * 2
            ch = int(VID_H * 1.25) // 2 * 2
            vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d," % (cw, ch, cw, ch)
                  + kb_expr(i, d, fps, VID_W, VID_H) + ",setsar=1")
            sh(["ffmpeg", "-y", "-loop", "1", "-i", im,
                "-vf", vf, "-frames:v", str(d), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", c])
        clips.append(c)

    if n == 1:
        # nothing to cross-fade into -- keep the single clip as-is (matches the old
        # concat-demuxer-of-one behaviour, which was already just a pass-through).
        os.replace(clips[0], "slide.mp4")
        return seg * n

    # Chain xfade across all clips in ONE ffmpeg call (filter_complex), rather than N sequential
    # pairwise re-encodes. Clip lengths are uniform (every clip above was rendered at the same `d`
    # frames), which gives xfade's offset= chain a clean closed form: each successive stage starts
    # its transition trans_dur seconds before the RUNNING merged stream would otherwise end. Clips
    # carry no audio track of their own (voice.mp3 is muxed in later by burn_and_mux), so only the
    # video graph needs chaining -- no acrossfade/amix bookkeeping required.
    clip_s = d / fps
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    filters = []
    prev_label = "0:v"
    running_dur = clip_s
    for i in range(1, n):
        off = running_dur - trans_dur
        out_label = "vout" if i == n - 1 else f"v{i}"
        filters.append(
            f"[{prev_label}][{i}:v]xfade=transition={xfade_name(i - 1)}:duration={trans_dur:.3f}:"
            f"offset={off:.3f}[{out_label}]")
        running_dur = running_dur + clip_s - trans_dur
        prev_label = out_label
    sh(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", f"[{prev_label}]",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "slide.mp4"])
    return seg * n


# ---------- 5. burn karaoke subs + mux voice (with plain-subtitle fallback if karaoke burn errors) ----------
def burn_and_mux(ass_path, plain_ass_path, voice_path="voice.mp3", out_path="out.mp4", burn=True):
    if not burn:
        # clean master: mux narration only, no burned-in captions (creator adds per-platform subs).
        sh(["ffmpeg", "-y", "-i", "slide.mp4", "-i", voice_path,
            "-map", "0:v", "-map", "1:a", "-shortest",
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
            "-pix_fmt", "yuv420p", out_path])
        print("[burn] burn_sub disabled -> muxed clean master with no burned subtitles", flush=True)
        return "no_burn"
    r = subprocess.run(["ffmpeg", "-y", "-i", "slide.mp4", "-i", voice_path,
                         "-vf", f"subtitles={ass_path}",
                         "-map", "0:v", "-map", "1:a", "-shortest",
                         "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                         "-pix_fmt", "yuv420p", out_path], capture_output=True, text=True)
    if r.returncode == 0:
        print("[burn] karaoke ASS burned ok", flush=True)
        return "karaoke"
    print("[burn] karaoke ASS burn failed, falling back to plain subtitles. stderr tail:",
          (r.stderr or "")[-1500:], flush=True)
    sh(["ffmpeg", "-y", "-i", "slide.mp4", "-i", voice_path,
        "-vf", f"subtitles={plain_ass_path}",
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        "-pix_fmt", "yuv420p", out_path])
    return "plain_fallback"


# ---------- 6. quality gate — do not let broken output reach R2 ----------
def quality_gate(out_path, expected_audio_s, n_sub_lines):
    reasons = []
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 50_000:
        reasons.append(f"file missing or too small ({os.path.getsize(out_path) if os.path.exists(out_path) else 0} bytes)")
        return False, reasons
    real_dur = dur(out_path)
    if real_dur < 3:
        reasons.append(f"duration too short ({real_dur:.1f}s)")
    if abs(real_dur - expected_audio_s) > 5:
        reasons.append(f"duration mismatch: video={real_dur:.1f}s expected~={expected_audio_s:.1f}s")
    if n_sub_lines <= 0:
        reasons.append("zero subtitle lines")
    # black-frame scan
    bd = subprocess.run(["ffmpeg", "-i", out_path, "-vf", "blackdetect=d=1:pic_th=0.98",
                          "-an", "-f", "null", "-"], capture_output=True, text=True)
    black_total = 0.0
    for m in re.finditer(r"black_duration:([\d.]+)", bd.stderr or ""):
        black_total += float(m.group(1))
    if real_dur > 0 and black_total / real_dur > 0.5:
        reasons.append(f"mostly black frames ({black_total:.1f}s / {real_dur:.1f}s)")
    return (len(reasons) == 0), reasons


def upload(path, key):
    s3.put_object(Bucket=S_BUCKET, Key=key, Body=open(path, "rb").read(), ContentType="video/mp4")
    print("UPLOADED", key, os.path.getsize(path), "bytes", flush=True)


if __name__ == "__main__":
    print(f"[run] book={BOOK} pages={PAGES} expert={EXPERT_KEY}/{EXPERT_NAME} "
          f"ratio={RATIO} {VID_W}x{VID_H} burn_sub={BURN_SUB}", flush=True)

    analysis = call_expert(TOPIC)
    script_text = gen_script_from_expert(analysis, EXPERT_NAME)
    print("[script] chars:", len(script_text), flush=True)   # content stays out of public CI log by not printing it verbatim
    with open("script.txt", "w", encoding="utf-8") as f:
        f.write(script_text)

    words = tts_with_word_timestamps(script_text)
    n_lines = build_ass(words, "subs.ass", w=VID_W, h=VID_H)
    build_plain_ass(words, "subs_plain.ass", w=VID_W, h=VID_H)

    imgs = fetch_images()
    if not imgs:
        raise SystemExit("no images fetched from R2 — aborting (no visual asset to render)")

    audio_s = dur("voice.mp3")
    render_body_clips(imgs, audio_s)
    burn_mode = burn_and_mux("subs.ass", "subs_plain.ass", burn=BURN_SUB)

    # debug thumbnails so a human (or the agent driving this pipeline) can visually confirm
    # subtitle sync/quality from the GH Actions artifact without needing to play the full mp4.
    for ts in (1.0, max(2.0, audio_s * 0.5), max(3.0, audio_s - 1.0)):
        subprocess.run(["ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", "out.mp4", "-frames:v", "1",
                         f"thumb_{ts:.1f}.jpg"], capture_output=True, text=True)

    ok, reasons = quality_gate("out.mp4", audio_s, n_lines)
    print(f"[gate] ok={ok} burn_mode={burn_mode} reasons={reasons}", flush=True)

    if not ok:
        # keep the reject visible for debugging but never let it land in the real _video/ namespace
        key = f"_video/_rejected/{BOOK}_{EXPERT_KEY}_{RATIO}_{int(time.time())}.mp4"
        try:
            upload("out.mp4", key)
        except Exception as e:
            print("reject-upload also failed:", e, flush=True)
        raise SystemExit(f"QUALITY GATE FAILED: {reasons}")

    key = f"_video/{BOOK}_{EXPERT_KEY}_{RATIO}_{int(time.time())}.mp4"
    upload("out.mp4", key)
    print("MANIFEST", json.dumps({
        "key": key, "book": BOOK, "expert": EXPERT_KEY, "engine": analysis.get("engine"),
        "ratio": RATIO, "width": VID_W, "height": VID_H, "burn_sub": BURN_SUB,
        "duration_s": round(audio_s, 1), "subtitle_lines": n_lines, "burn_mode": burn_mode,
    }, ensure_ascii=False), flush=True)
