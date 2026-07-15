# 真实古籍页面 -> 宣传短视频(海外流失古籍回归计划系列)
# 取材:123网盘真实页面扫描图(不是R2、不是AI生成幻想画面)。
# 效果:逐页缓慢缩放平移(Ken Burns)+ 上下文字条,FFmpeg合成,零AI幻觉风险。
import os, sys, json, subprocess, urllib.request

PAN = os.environ.get("PAN_BASE", "https://open-api.123pan.com")
PCID = os.environ["PAN_CID"]; PSEC = os.environ["PAN_SEC"]
BOOK_ID = os.environ.get("BOOK_ID", "yxf07")
PAN_DIR_ID = os.environ.get("PAN_DIR_ID", "36780084")
BOOK_TITLE = os.environ.get("BOOK_TITLE", "醫心方")
TOP_TEXT = os.environ.get("TOP_TEXT", "海外流失古籍回归计划")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "8"))  # 短视频不用整本都上,取前几页够了

import requests
S = requests.Session()
_tok = {"v": None}


def token():
    if _tok["v"]:
        return _tok["v"]
    r = S.post(PAN + "/api/v1/access_token", headers={"Platform": "open_platform"},
               json={"clientID": PCID, "clientSecret": PSEC}, timeout=30)
    _tok["v"] = (r.json().get("data") or {}).get("accessToken")
    if not _tok["v"]:
        sys.exit("access_token failed: " + r.text[:300])
    return _tok["v"]


def pan_get(path):
    h = {"Authorization": "Bearer " + token(), "Platform": "open_platform"}
    r = S.get(PAN + path, headers=h, timeout=60)
    j = r.json()
    if j.get("code") != 0:
        sys.exit(f"api {path} failed: {j}")
    return j.get("data") or {}


def list_children(parent_id):
    out, last = [], 0
    while True:
        d = pan_get(f"/api/v2/file/list?parentFileId={parent_id}&limit=100&lastFileId={last}")
        for it in d.get("fileList") or []:
            if it.get("trashed") in (1, True):
                continue
            out.append(it)
        last = d.get("lastFileId")
        if last in (None, -1, 0, ""):
            return out


def download_url(file_id):
    d = pan_get(f"/api/v1/file/download_info?fileId={file_id}")
    return d.get("downloadUrl")


def main():
    kids = list_children(PAN_DIR_ID)
    # 页面图一般按文件名排序即页序(page_0001.webp 这类命名)
    kids = [k for k in kids if k.get("type") == 0]  # type 0 = 文件, 1 = 文件夹
    kids.sort(key=lambda k: k.get("filename", ""))
    kids = kids[:MAX_PAGES]
    if not kids:
        sys.exit(f"{PAN_DIR_ID} 下没找到页面文件,检查pan_dir_id是否正确指向册文件夹(不是书聚合层)")
    print(f"取到 {len(kids)} 页: {[k['filename'] for k in kids]}", flush=True)

    os.makedirs("frames", exist_ok=True)
    frame_paths = []
    for i, k in enumerate(kids):
        url = download_url(k["fileId"])
        if not url:
            print(f"跳过(拿不到下载链接): {k['filename']}", flush=True)
            continue
        ext = os.path.splitext(k["filename"])[1] or ".webp"
        fp = f"frames/page_{i:03d}{ext}"
        urllib.request.urlretrieve(url, fp)
        frame_paths.append(fp)
        print(f"下载完成: {fp}", flush=True)

    if not frame_paths:
        sys.exit("一页都没下下来,停")

    # 统一转成jpg(ffmpeg处理webp在部分环境需要额外解码器,jpg最稳)
    from PIL import Image
    jpg_paths = []
    for fp in frame_paths:
        im = Image.open(fp).convert("RGB")
        jp = fp.rsplit(".", 1)[0] + ".jpg"
        im.save(jp, "JPEG", quality=92)
        jpg_paths.append(jp)

    # concat demuxer + zoompan 做逐页缓慢放大效果,每页停留2.5秒
    DUR = 2.5
    FPS = 30
    concat_txt = "frames/concat.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for jp in jpg_paths:
            f.write(f"file '{os.path.abspath(jp)}'\n")
            f.write(f"duration {DUR}\n")
        f.write(f"file '{os.path.abspath(jpg_paths[-1])}'\n")  # concat demuxer要求最后一帧再重复一次

    raw_video = "raw_slideshow.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-vf", f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,zoompan=z='min(zoom+0.0015,1.15)':d={int(DUR*FPS)}:s=1080x1920:fps={FPS}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", raw_video,
    ], check=True)

    # 上下文字条(drawtext),竖屏9:16,顶部品牌语+底部书名
    out_video = f"out_{BOOK_ID}.mp4"
    top_esc = TOP_TEXT.replace(":", r"\:").replace("'", r"\'")
    bottom_esc = f"{BOOK_TITLE} · 十二世纪抄本 · 日本東京國立博物館藏".replace(":", r"\:").replace("'", r"\'")
    drawtext = (
        f"drawtext=text='{top_esc}':fontsize=52:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=20:"
        f"x=(w-text_w)/2:y=80,"
        f"drawtext=text='{bottom_esc}':fontsize=38:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=16:"
        f"x=(w-text_w)/2:y=h-160"
    )
    subprocess.run([
        "ffmpeg", "-y", "-i", raw_video, "-vf", drawtext,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", out_video,
    ], check=True)

    print(f"=== 完成: {out_video} ===", flush=True)
    os.makedirs("out", exist_ok=True)
    os.replace(out_video, f"out/{out_video}")


if __name__ == "__main__":
    main()
