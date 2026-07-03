# -*- coding: utf-8 -*-
# Social short-video generator (programmatic, cloud render). Public-repo: source is CJK-free (prompt/topic base64).
# gateway script -> edge-tts voice+subs -> R2 rare-book images -> ffmpeg 9:16 burned subs -> R2 upload.
import os, re, base64, json, time, subprocess, urllib.request, ssl, boto3

NOVA_URL = "https://nova-gateway.hosonzuo.workers.dev"
NOVA_KEY = os.environ["NOVA_KEY"]
S_EP = os.environ["S_EP"]; S_AK = os.environ["S_AK"]; S_SK = os.environ["S_SK"]; S_BUCKET = os.environ["S_BUCKET"]
BOOK = os.environ.get("BOOK", "ylgc_2")
PAGES = [int(x) for x in os.environ.get("PAGES", "1,2,3,4,5,6").split(",")]
PROMPT_B64 = os.environ.get("PROMPT_B64", "5Li65oqW6Z+zL+Wwj+e6ouS5puWGmeS4gOadoTE1LTE456eS5Lit5Yy75Y+k57GN56eR5pmu5Y+j5pKt6ISa5pysLOS4u+mimDp7VH3jgILliY0z56eS5by66ZKp5a2Q6K6+5oKs5b+1LOS4remXtOWPjei9rOaPreecn+ebuCznu5PlsL7kuIDlj6Xoh6rnhLbokL3liLDjgIzlj6TmlrlBSeaYn+WbvsK35aSa5a62QUnkuIDotbfop6Por7vlj6TnsY3jgI3jgILnuq/lj6Pmkq3mloflrZc4MC0xMTDlrZcs5Y+j6K+t5YyW5pyJ572R5oSfLOS4jeimgWVtb2pp44CB5LiN6KaB5Lu75L2V5YiG6ZWcL+aXgeeZvS/moIfngrnlpJbnmoTmoIfms6gs55u05o6l57uZ6IO95b+155qE6K+d44CC")
TOPIC_B64 = os.environ.get("TOPIC_B64", "546L5riF5Lu744CK5Yy75p6X5pS56ZSZ44CL5Lqy6Ieq6Kej5YmW5bC45L2TLOe6oOato+ayv+eUqOWNg+W5tOeahOS6uuS9k+iEj+iFkemUmeivrw==")
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36"
s3 = boto3.client("s3", endpoint_url=S_EP, aws_access_key_id=S_AK, aws_secret_access_key=S_SK, region_name="auto")


def gen_script():
    topic = base64.b64decode(TOPIC_B64).decode("utf-8")
    p = base64.b64decode(PROMPT_B64).decode("utf-8").replace("{T}", topic)
    body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": p}],
                       "max_tokens": 600, "temperature": 0.8}).encode()
    r = urllib.request.urlopen(urllib.request.Request(NOVA_URL + "/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + NOVA_KEY, "Content-Type": "application/json", "User-Agent": UA}),
        timeout=70, context=CTX)
    t = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿]", "", t)
    return re.sub(r"\s+", "", t)


def sh(cmd):
    print("+ " + " ".join(str(c) for c in cmd[:6]) + " ...", flush=True)
    subprocess.run(cmd, check=True)


def tts(text):
    sh(["edge-tts", "--voice", "zh-CN-XiaoxiaoNeural", "--rate", "+10%",
        "--text", text, "--write-media", "voice.mp3", "--write-subtitles", "voice.vtt"])


def fetch_images():
    out = []
    for i, p in enumerate(PAGES):
        key = "book/%s/page_%04d.webp" % (BOOK, p)
        try:
            b = s3.get_object(Bucket=S_BUCKET, Key=key)["Body"].read()
            fn = "img%d.webp" % i; open(fn, "wb").write(b); out.append(fn)
        except Exception as e:
            print("img miss", key, str(e)[:40], flush=True)
    return out


def dur(f):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", f], capture_output=True, text=True)
    return float(o.stdout.strip())


def render(imgs):
    ad = dur("voice.mp3"); n = len(imgs); seg = max(2.2, ad / n)
    clips = []
    for i, im in enumerate(imgs):
        c = "clip%d.mp4" % i
        sh(["ffmpeg", "-y", "-loop", "1", "-t", "%.2f" % seg, "-i", im,
            "-vf", ("scale=1350:2400:force_original_aspect_ratio=increase,crop=1350:2400,"
                    "zoompan=z='min(zoom+0.0006,1.12)':d=%d:s=1080x1920:fps=30,setsar=1" % int(seg * 30)),
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", c])
        clips.append(c)
    open("concat.txt", "w").write("\n".join("file '%s'" % c for c in clips))
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", "slide.mp4"])
    style = ("FontName=Noto Sans CJK SC,FontSize=17,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&HAA000000,BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=150")
    sh(["ffmpeg", "-y", "-i", "slide.mp4", "-i", "voice.mp3",
        "-vf", "subtitles=voice.vtt:force_style='%s'" % style,
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-pix_fmt", "yuv420p", "out.mp4"])


if __name__ == "__main__":
    text = gen_script(); print("SCRIPT chars:", len(text), flush=True)   # no CJK to public log
    tts(text)
    imgs = fetch_images()
    if not imgs:
        raise SystemExit("no images fetched")
    render(imgs)
    key = "_video/%s_%d.mp4" % (BOOK, int(time.time()))
    s3.put_object(Bucket=S_BUCKET, Key=key, Body=open("out.mp4", "rb").read(), ContentType="video/mp4")
    print("UPLOADED", key, os.path.getsize("out.mp4"), "bytes", flush=True)
