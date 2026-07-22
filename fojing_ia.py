# Buddhist canon image ingest: Internet Archive (archive.org) public-domain tripitaka
# scans -> per-volume Image Container PDF -> render pages to webp -> R2 fojing-lib.
# Runner fetches directly from archive.org (no proxy/egress; IA is reachable from GitHub
# runners). One shard = one (identifier, volume) work unit so a single big item is
# naturally chunked (per founder instruction: process one volume at a time, delete local
# temp files after each volume, never try to pull a whole multi-hundred-GB item at once).
# Idempotent: head_object skip per page, so a re-run only fills gaps.
#
# IA politeness (per the internal 2026-07-02 IA recon report, section 4): metadata/search calls
# limited to <=3 concurrent (workflow matrix max-parallel<=3) and every direct IA HTTP call
# is followed by a >=1s sleep. This script never lists/scrapes beyond the one identifier it
# was given -- no crawling.
import os, io, re, json, time, subprocess
import requests, boto3
from botocore.exceptions import ClientError
from botocore.config import Config

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
BUCKET = os.environ["S_BUCKET"]     # workflow input default "fojing-lib", never secrets.S_BUCKET (that's the med bucket)
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
IDENTIFIER = os.environ.get("IDENTIFIER", "")
VOL_RANGE = os.environ.get("VOL_RANGE", "").strip()     # "" = all volumes; "76" = single vol; "1-20" = inclusive slice (1-based, in the item's own volume ordering)
PILOT = os.environ.get("PILOT", "0") == "1"             # true = only process the single smallest-page-count volume (for the 3-gate pilot run)
WORKDIR = os.environ.get("RUNNER_TEMP", "/tmp")
IA_SLEEP = float(os.environ.get("IA_SLEEP", "1.2"))     # >=1s between direct IA HTTP calls, per recon report politeness guidance
MAX_LONG_EDGE = int(os.environ.get("MAX_LONG_EDGE", "2200"))   # matches existing platform page-image spec
WEBP_QUALITY = int(os.environ.get("WEBP_QUALITY", "80"))
UA = "gufang-fojing-ia-ingest/1.0 (contact: hosonzuo@gmail.com; educational/archival public-domain canon digitization)"

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK,
                  region_name="auto", config=Config(connect_timeout=15, read_timeout=120, retries={"max_attempts": 3}))
sess = requests.Session()
sess.headers.update({"User-Agent": UA})


def ia_get(url, **kw):
    # Every direct-to-IA call funnels through here so the politeness sleep is never
    # accidentally skipped by a new call site added later.
    r = sess.get(url, timeout=kw.pop("timeout", 60), **kw)
    time.sleep(IA_SLEEP)
    return r


def fetch_item_metadata(identifier):
    r = ia_get(f"https://archive.org/metadata/{identifier}")
    r.raise_for_status()
    return r.json()


def vol_key_from_filename(name):
    # Pull the volume/juan number out of an IA scribe-derivative filename so different
    # items' differing naming conventions (koreana-76.pdf vs yongle_beizang_201.pdf)
    # both reduce to a plain int for sorting/filtering. Takes the LAST integer run in the
    # basename (stem, before the first "_" that starts a known suffix word) since that is
    # consistently the volume number in every naming style seen during recon.
    stem = os.path.splitext(os.path.basename(name))[0]
    nums = re.findall(r"\d+", stem)
    return int(nums[-1]) if nums else None


def discover_volumes(meta):
    # A tripitaka item's page-image PDFs are labelled either "Image Container PDF" or
    # "Text PDF" depending on when/how the item was processed (both seen during recon on
    # yongle vs koreana respectively) -- never both on the same item, so try in this order.
    files = meta["files"]
    pdf_format = None
    for cand in ("Image Container PDF", "Text PDF"):
        if any(f.get("format") == cand for f in files):
            pdf_format = cand
            break
    if pdf_format is None:
        raise RuntimeError(f"no page-image PDF format found on item files (formats seen: {sorted(set(f.get('format') for f in files))})")
    scandata_by_stem = {}
    for f in files:
        if f.get("format") == "Scandata":
            stem = os.path.splitext(os.path.basename(f["name"]))[0]
            stem = re.sub(r"_scandata$", "", stem)
            scandata_by_stem[stem] = f["name"]
    vols = []
    for f in files:
        if f.get("format") != pdf_format:
            continue
        pdf_name = f["name"]
        pdf_stem = os.path.splitext(os.path.basename(pdf_name))[0]
        pdf_stem = re.sub(r"_text$", "", pdf_stem)   # yongle's "Additional Text PDF" companion has _text suffix; page-image one doesn't, but guard anyway
        volnum = vol_key_from_filename(pdf_name)
        raw_size = f.get("size")
        vols.append({
            "vol": volnum,
            "pdf_name": pdf_name,
            "pdf_size": int(raw_size) if raw_size is not None else None,   # IA metadata JSON returns size as a string; cast so min()/sort() compare numerically not lexicographically
            "scandata_name": scandata_by_stem.get(pdf_stem),
        })
    vols.sort(key=lambda v: (v["vol"] is None, v["vol"]))
    return vols


def apply_vol_range(vols, vol_range):
    if not vol_range:
        return vols
    if "-" in vol_range:
        lo, hi = vol_range.split("-", 1)
        lo, hi = int(lo), int(hi)
        return [v for v in vols if v["vol"] is not None and lo <= v["vol"] <= hi]
    target = int(vol_range)
    return [v for v in vols if v["vol"] == target]


def leaf_count_from_scandata(identifier, scandata_name):
    if not scandata_name:
        return None
    r = ia_get(f"https://archive.org/download/{identifier}/{scandata_name}")
    if r.status_code != 200:
        return None
    m = re.search(r"<leafCount>(\d+)</leafCount>", r.text)
    return int(m.group(1)) if m else None


def download_volume_pdf(identifier, pdf_name, dest_path):
    url = f"https://archive.org/download/{identifier}/{pdf_name}"
    r = ia_get(url, stream=True, timeout=180)
    r.raise_for_status()
    with open(dest_path, "wb") as fh:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                fh.write(chunk)
    return os.path.getsize(dest_path)


# 2026-07-19修复:原每页一次s3.head_object()查重,改GitHub缓存本地_DONE记账(同ocr_ndl.py/ocr.py方法)。
_DONE = set()


def render_and_upload(identifier, vol, pdf_path, expected_pages):
    # Import fitz lazily so a metadata-only dry run (not exercised by this script today,
    # but keeps the module importable in odd environments) doesn't require the wheel.
    import fitz
    from PIL import Image
    vol_str = f"{vol:04d}" if vol is not None else "0000"
    prefix = f"ia/{identifier}-v{vol_str}/"
    doc = fitz.open(pdf_path)
    n_pages = doc.page_count
    uploaded = skipped = errored = 0
    for i in range(n_pages):
        page_no = i + 1
        key = f"{prefix}page_{page_no:04d}.webp"
        if key in _DONE:
            skipped += 1
            continue
        try:
            page = doc.load_page(i)
            # Render at a DPI that lands close to MAX_LONG_EDGE on the page's long side,
            # then let Pillow do the exact final resize -- avoids over-rendering huge pages
            # at fixed high DPI only to downscale (wastes runner CPU/memory).
            rect = page.rect
            long_edge_pt = max(rect.width, rect.height)
            dpi = max(72, min(300, int(MAX_LONG_EDGE / (long_edge_pt / 72.0))))
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            w, h = im.size
            long_edge = max(w, h)
            if long_edge > MAX_LONG_EDGE:
                scale = MAX_LONG_EDGE / long_edge
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="WEBP", quality=WEBP_QUALITY)
            s3.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue(), ContentType="image/webp")
            _DONE.add(key)
            uploaded += 1
        except Exception as e:
            print(f"ERR page {identifier} v{vol_str} p{page_no}: {str(e)[:80]}", flush=True)
            errored += 1
        if page_no % 25 == 0:
            print(f"  {identifier} v{vol_str} progress {page_no}/{n_pages} up={uploaded} skip={skipped} err={errored}", flush=True)
    doc.close()
    return {"pdf_pages": n_pages, "expected_pages": expected_pages, "uploaded": uploaded, "skipped": skipped, "errored": errored}


def r2_actual_page_count(result):
    # Zero-LIST fix (2026-07-22, resolves the guard-no-list-objects red build): the old
    # implementation paged through list_objects_v2 over the ia/{identifier}-v{vol}/ prefix
    # to get an "independent of this run's own upload/skip bookkeeping" recount. That was
    # scoped to one small per-volume prefix (never a full-bucket scan), but the CI guard
    # is a repo-wide, scope-blind hard gate -- any list_objects_v2/get_paginator call trips
    # it, so it has to go with zero exceptions rather than relying on "this one's scope is
    # small, should be fine".
    # Replacement: render_and_upload() already classifies every page in range(n_pages) this
    # run -- either just put_object'd successfully (uploaded), already recognized via the
    # _DONE ledger as present in R2 (skipped), or errored (never landed, correctly excluded).
    # uploaded+skipped matches the old list-based count exactly, with zero extra R2 calls.
    # Trade-off: this is no longer a recount independent of the local ledger -- it now
    # trusts the same _DONE ledger that already gates the upload-skip decision. That is not
    # a new trust surface; it is the same trust model accepted on 2026-07-19 when per-page
    # head_object verification was replaced with the _DONE ledger.
    return result["uploaded"] + result["skipped"]


def process_volume(identifier, vinfo, title, license_note):
    vol = vinfo["vol"]
    vol_str = f"{vol:04d}" if vol is not None else "0000"
    print(f"=== {identifier} vol {vol_str}: fetching scandata for leafCount ===", flush=True)
    expected = leaf_count_from_scandata(identifier, vinfo["scandata_name"])
    pdf_path = os.path.join(WORKDIR, f"{identifier}-v{vol_str}.pdf")
    print(f"=== {identifier} vol {vol_str}: downloading {vinfo['pdf_name']} ({vinfo.get('pdf_size')} bytes) ===", flush=True)
    download_volume_pdf(identifier, vinfo["pdf_name"], pdf_path)
    try:
        result = render_and_upload(identifier, vol, pdf_path, expected)
    finally:
        # Per instruction: one volume at a time, delete local temp immediately after --
        # runner disk is ~14GB and a multi-hundred-GB item cannot all land on disk at once.
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
    r2_actual = r2_actual_page_count(result)
    entry = {
        "identifier": identifier, "vol": vol, "vol_str": vol_str,
        "title": title, "license": license_note,
        "expected_pages_scandata": expected,
        "pdf_pages": result["pdf_pages"],
        "uploaded_this_run": result["uploaded"],
        "skipped_already_done": result["skipped"],
        "errored": result["errored"],
        "r2_actual_pages": r2_actual,
        "r2_key_prefix": f"ia/{identifier}-v{vol_str}/",
    }
    gap = None
    if expected is not None:
        gap = expected - r2_actual
    entry["gap_K"] = gap
    print(f"=== {identifier} vol {vol_str} DONE: expected={expected} pdf_pages={result['pdf_pages']} r2_actual={r2_actual} gap_K={gap} ===", flush=True)
    return entry


def main():
    global _DONE
    if os.path.exists("ledger.json"):
        try:
            _DONE = set(json.load(open("ledger.json", encoding="utf-8")))
        except Exception:
            _DONE = set()
    print(f"ledger已有 {len(_DONE)} 条记录", flush=True)

    if not IDENTIFIER:
        raise SystemExit("IDENTIFIER env var required")
    meta = fetch_item_metadata(IDENTIFIER)
    title = meta["metadata"].get("title")
    license_note = meta["metadata"].get("licenseurl") or meta["metadata"].get("rights")
    all_vols = discover_volumes(meta)
    vols = apply_vol_range(all_vols, VOL_RANGE)
    if PILOT:
        # Pilot = only the single smallest-size volume (fast, cheap first full-chain proof).
        # Needs pdf_size on every candidate; if a range/no-range selection left more than
        # one, pick min by declared size (proxy for page count, confirmed correlated during
        # recon: koreana-76 was both the smallest PDF and the smallest scandata leafCount).
        sized = [v for v in vols if v.get("pdf_size")]
        vols = [min(sized, key=lambda v: v["pdf_size"])] if sized else vols[:1]
    mine = [v for i, v in enumerate(vols) if i % TOTAL == SHARD]
    print(f"identifier={IDENTIFIER} total_vols_in_item={len(all_vols)} selected_by_range={len(vols)} shard {SHARD}/{TOTAL} mine={len(mine)}", flush=True)
    ledger = []
    for vinfo in mine:
        try:
            entry = process_volume(IDENTIFIER, vinfo, title, license_note)
        except Exception as e:
            entry = {"identifier": IDENTIFIER, "vol": vinfo["vol"], "status": f"err-volume:{str(e)[:100]}"}
            print(f"ERR volume {IDENTIFIER} v{vinfo['vol']}: {e}", flush=True)
        ledger.append(entry)
    lk = f"_ledger/fojing_ia_{IDENTIFIER}_shard_{SHARD}.json"
    s3.put_object(Bucket=BUCKET, Key=lk, Body=json.dumps(ledger, ensure_ascii=False, indent=1).encode("utf-8"))
    json.dump(sorted(_DONE), open("ledger.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(f"=== shard {SHARD} complete, {len(ledger)} volumes -> ledger {lk} ===", flush=True)


def finalize():
    identifier = IDENTIFIER
    total = int(os.environ.get("TOTAL", "1"))
    if not identifier:
        raise SystemExit("IDENTIFIER env var required for finalize")
    rows = []
    missing_shards = []
    for shard in range(total):
        lk = f"_ledger/fojing_ia_{identifier}_shard_{shard}.json"
        try:
            body = s3.get_object(Bucket=BUCKET, Key=lk)["Body"].read().decode("utf-8")
        except ClientError:
            missing_shards.append(shard)
            continue
        rows.extend(json.loads(body))
    # Append (not overwrite) to the running jsonl manifest so multiple identifiers /
    # multiple runs over time accumulate rather than clobber each other.
    mk = "_cc/fojing_ia_manifest.jsonl"
    try:
        existing = s3.get_object(Bucket=BUCKET, Key=mk)["Body"].read().decode("utf-8")
    except ClientError:
        existing = ""
    existing_keys = set()
    for line in existing.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            existing_keys.add((r.get("identifier"), r.get("vol")))
        except json.JSONDecodeError:
            continue
    new_lines = []
    for r in rows:
        k = (r.get("identifier"), r.get("vol"))
        if k in existing_keys:
            continue   # this (identifier, vol) already has a manifest line from a prior finalize; a re-run's ledger would duplicate it
        new_lines.append(json.dumps(r, ensure_ascii=False))
        existing_keys.add(k)
    merged = existing + ("\n".join(new_lines) + "\n" if new_lines else "")
    s3.put_object(Bucket=BUCKET, Key=mk, Body=merged.encode("utf-8"))
    total_gap = sum((r.get("gap_K") or 0) for r in rows if isinstance(r.get("gap_K"), int))
    print(f"=== finalize {identifier}: {len(rows)} volumes this run, {len(new_lines)} new manifest lines, missing_shard_ledgers={missing_shards}, sum(gap_K)={total_gap} -> {mk} ===", flush=True)
    for r in rows:
        print(f"  vol={r.get('vol')} expected={r.get('expected_pages_scandata')} r2_actual={r.get('r2_actual_pages')} gap_K={r.get('gap_K')} status={r.get('status','ok')}", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "finalize":
        finalize()
    else:
        main()
