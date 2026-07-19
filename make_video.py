# -*- coding: utf-8 -*-
# make_video.py v2 (2026-07-20) — audio-first pipeline, fixes the old "floating subtitle" problem.
#
# Pipeline: real expert-persona API (52 grounded historical-doctor personas, /api/ai/expert on the
# live production site) -> nova-gateway rewrite into a short spoken script -> edge-tts TTS that
# ALSO returns real word-level timestamps (WordBoundary events) -> ASS karaoke subtitles built from
# those exact timestamps (word highlights precisely when it is actually spoken, not a guessed/evenly
# -split overlay) -> real R2 rare-book page images with varied Ken-Burns (not one identical zoom for
# every clip) -> burn subs -> mux voice -> automated quality gate (duration / black-frame / non-empty
# subtitle checks) before it is allowed to land in R2 `_video/`.
#
# Free-only resource stack: nova-gateway (proxies modelscope/sensenova/agnes/gemini/groq/nvidia/
# siliconflow — verified via GET / health probe, no Cloudflare Workers AI anywhere) + the production
# /api/ai/expert endpoint (uses guyaofang-web's own functions/api/gateway/_providers.js free
# fallback chain when called with a non-paid model) + edge-tts (free, Microsoft). No paid API keys
# used in this script.
import os, re, sys, json, time, random, subprocess, urllib.request, urllib.error, ssl, asyncio
import boto3
import edge_tts

NOVA_URL = "https://nova-gateway.hosonzuo.workers.dev"
NOVA_KEY = os.environ["NOVA_KEY"]
EXPERT_API = "https://www.gufangai.com/api/ai/expert"

S_EP = os.environ["S_EP"]; S_AK = os.environ["S_AK"]; S_SK = os.environ["S_SK"]; S_BUCKET = os.environ["S_BUCKET"]
BOOK = os.environ.get("BOOK", "ylgc_2")
PAGES = [int(x) for x in os.environ.get("PAGES", "1,2,3,4,5,6").split(",")]
VOICE = os.environ.get("VOICE", "zh-CN-XiaoxiaoNeural")

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
        "④纯口播文字90-130字,不要emoji、不要分行、不要任何分镜/旁白标注,直接给能念出来的话,标点只保留逗号和句号。\n\n"
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
    if len(body) > 110:
        cut = body[:120]
        m = re.search(r"^(.*[。!?])", cut)
        body = m.group(1) if m else (cut + "。")
    mid = f"{expert_name}认为,{body}"
    if fname:
        mid += f"可参{fname}" + (f",出自{source}。" if source else "。")
    text = f"这则医案,{expert_name}会怎么判?" + mid + "古方AI星图,请AI专家分身为你解读古籍。"
    return text


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
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", text)   # strip emoji
    text = re.sub(r"[ \t\n]+", "", text)
    text = re.sub(r"[\"'“”]", "", text)
    return text[:220]   # hard safety cap regardless of source


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
def fetch_images():
    out = []
    for i, p in enumerate(PAGES):
        key = f"book/{BOOK}/page_{p:04d}.webp"
        try:
            b = s3.get_object(Bucket=S_BUCKET, Key=key)["Body"].read()
            fn = f"img{i}.webp"; open(fn, "wb").write(b); out.append(fn)
        except Exception as e:
            print("img miss", key, str(e)[:80], flush=True)
    return out


def dur(f):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", f], capture_output=True, text=True)
    return float(o.stdout.strip() or 0)


# varied Ken-Burns: cycle through distinct motion presets so clips are NOT identical (2026-07-20,
# founder feedback: don't template every image the same way).
def kb_expr(preset_i, d, fps):
    if preset_i % 4 == 0:      # center push-in
        z = f"1.0+0.16*on/{d}"; x = "iw/2-(iw/zoom/2)"; y = "ih/2-(ih/zoom/2)"
    elif preset_i % 4 == 1:    # top-left corner push-in
        z = f"1.0+0.14*on/{d}"; x = "0"; y = "0"
    elif preset_i % 4 == 2:    # bottom-right corner push-in
        z = f"1.0+0.14*on/{d}"; x = "iw-iw/zoom"; y = "ih-ih/zoom"
    else:                      # fixed zoom, slow pan left -> right
        z = "1.12"; x = f"(iw-iw/zoom)*on/{d}"; y = "ih/2-(ih/zoom/2)"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={d}:s=1080x1920:fps={fps}"


def render_body_clips(imgs, total_audio_s):
    fps = 30
    n = len(imgs)
    seg = max(2.4, total_audio_s / n)
    clips = []
    for i, im in enumerate(imgs):
        c = f"clip{i}.mp4"
        d = int(seg * fps)
        vf = ("scale=1350:2400:force_original_aspect_ratio=increase,crop=1350:2400," + kb_expr(i, d, fps) + ",setsar=1")
        sh(["ffmpeg", "-y", "-loop", "1", "-t", f"{seg:.2f}", "-i", im,
            "-vf", vf, "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", c])
        clips.append(c)
    open("concat.txt", "w").write("\n".join(f"file '{c}'" for c in clips))
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "slide.mp4"])
    return seg * n


# ---------- 5. burn karaoke subs + mux voice (with plain-subtitle fallback if karaoke burn errors) ----------
def burn_and_mux(ass_path, plain_ass_path, voice_path="voice.mp3", out_path="out.mp4"):
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
    print(f"[run] book={BOOK} pages={PAGES} expert={EXPERT_KEY}/{EXPERT_NAME}", flush=True)

    analysis = call_expert(TOPIC)
    script_text = gen_script_from_expert(analysis, EXPERT_NAME)
    print("[script] chars:", len(script_text), flush=True)   # content stays out of public CI log by not printing it verbatim
    with open("script.txt", "w", encoding="utf-8") as f:
        f.write(script_text)

    words = tts_with_word_timestamps(script_text)
    n_lines = build_ass(words, "subs.ass")
    build_plain_ass(words, "subs_plain.ass")

    imgs = fetch_images()
    if not imgs:
        raise SystemExit("no images fetched from R2 — aborting (no visual asset to render)")

    audio_s = dur("voice.mp3")
    render_body_clips(imgs, audio_s)
    burn_mode = burn_and_mux("subs.ass", "subs_plain.ass")

    ok, reasons = quality_gate("out.mp4", audio_s, n_lines)
    print(f"[gate] ok={ok} burn_mode={burn_mode} reasons={reasons}", flush=True)

    if not ok:
        # keep the reject visible for debugging but never let it land in the real _video/ namespace
        key = f"_video/_rejected/{BOOK}_{EXPERT_KEY}_{int(time.time())}.mp4"
        try:
            upload("out.mp4", key)
        except Exception as e:
            print("reject-upload also failed:", e, flush=True)
        raise SystemExit(f"QUALITY GATE FAILED: {reasons}")

    key = f"_video/{BOOK}_{EXPERT_KEY}_{int(time.time())}.mp4"
    upload("out.mp4", key)
    print("MANIFEST", json.dumps({
        "key": key, "book": BOOK, "expert": EXPERT_KEY, "engine": analysis.get("engine"),
        "duration_s": round(audio_s, 1), "subtitle_lines": n_lines, "burn_mode": burn_mode,
    }, ensure_ascii=False), flush=True)
