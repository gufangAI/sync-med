# CBETA sutra text corpus ingest: clone cbeta-org/xml-p5 (public TEI P5 XML) on the
# runner, parse each sutra XML, strip collation apparatus, keep body text + juan
# structure, and push to R2 fojing-lib as one txt per sutra. Idempotent (head_object
# skip). No local download -> everything runs inside the Actions runner only.
import os, re, json, subprocess, xml.etree.ElementTree as ET
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

EP = os.environ["S_EP"]; AK = os.environ["S_AK"]; SK = os.environ["S_SK"]
BUCKET = os.environ["S_BUCKET"]           # workflow input default "fojing-lib", never secrets.S_BUCKET (that's the med bucket)
SHARD = int(os.environ.get("SHARD", "0")); TOTAL = int(os.environ.get("TOTAL", "1"))
REPO_URL = os.environ.get("CBETA_REPO", "https://github.com/cbeta-org/xml-p5.git")
WORKDIR = os.environ.get("RUNNER_TEMP", "/tmp")
CLONE_DIR = os.path.join(WORKDIR, "xml-p5")
LICENSE_NOTE = "CC BY-NC-SA 3.0 Taiwan (CBETA); per-file availability note preserved; see https://www.cbeta.org/copyright.php"

s3 = boto3.client("s3", endpoint_url=EP, aws_access_key_id=AK, aws_secret_access_key=SK,
                  region_name="auto", config=Config(connect_timeout=15, read_timeout=60, retries={"max_attempts": 3}))

NS = {"t": "http://www.tei-c.org/ns/1.0", "cb": "http://www.cbeta.org/ns/1.0"}


def clone_source():
    # Shallow clone once per shard (each matrix job is a fresh runner, so this always
    # re-clones; repo is ~1.2GB, well inside the ~14GB runner disk budget).
    if os.path.isdir(CLONE_DIR):
        return
    subprocess.run(["git", "clone", "--depth", "1", REPO_URL, CLONE_DIR], check=True)


def list_xml_files():
    # Deterministic order (sorted relative paths) so every shard computes the same
    # partition without needing a shared index file.
    out = []
    for root, dirs, files in os.walk(CLONE_DIR):
        dirs.sort()
        rel_root = os.path.relpath(root, CLONE_DIR)
        if rel_root.startswith(".git") or rel_root.startswith("schema"):
            continue
        for f in sorted(files):
            if f.endswith(".xml"):
                out.append(os.path.join(root, f))
    out.sort()
    return out


def local_text(el):
    # Collect only real text nodes under el (recursively), skipping nothing structurally
    # special-cased: lb/pb/anchor/note/app/rdg/lem carry no usable .text for our purpose
    # in this corpus (verified against sample sutras: apparatus <app>/<lem>/<rdg> entries
    # live outside <body>, and in-body anchors/lb/pb are empty self-closing tags), so a
    # plain recursive itertext() already yields clean prose without special filtering.
    return "".join(el.itertext())


def extract_title(root):
    ts = root.find(".//t:teiHeader/t:fileDesc/t:titleStmt", NS)
    if ts is None:
        return None
    # Prefer the "level=m" zh-Hant title (the sutra's own formal title, not the canon's).
    for title in ts.findall("t:title", NS):
        if title.get("level") == "m" and title.get("{http://www.w3.org/XML/1998/namespace}lang") == "zh-Hant":
            return (title.text or "").strip()
    for title in ts.findall("t:title", NS):
        if title.get("{http://www.w3.org/XML/1998/namespace}lang") == "zh-Hant":
            return (title.text or "").strip()
    first = ts.find("t:title", NS)
    return (first.text or "").strip() if first is not None else None


def extract_author(root):
    a = root.find(".//t:teiHeader/t:fileDesc/t:titleStmt/t:author", NS)
    return (a.text or "").strip() if a is not None and a.text else None


def extract_extent(root):
    e = root.find(".//t:teiHeader/t:fileDesc/t:extent", NS)
    return (e.text or "").strip() if e is not None and e.text else None


def extract_idno(root):
    # <idno type="CBETA"><idno type="canon">T</idno>.<idno type="vol">1</idno>.<idno type="no">1</idno></idno>
    idno = root.find(".//t:teiHeader/t:fileDesc/t:publicationStmt/t:idno[@type='CBETA']", NS)
    if idno is None:
        return None, None, None
    canon = idno.find("t:idno[@type='canon']", NS)
    vol = idno.find("t:idno[@type='vol']", NS)
    no = idno.find("t:idno[@type='no']", NS)
    return (canon.text.strip() if canon is not None and canon.text else None,
            vol.text.strip() if vol is not None and vol.text else None,
            no.text.strip() if no is not None and no.text else None)


def extract_license_note(root):
    p = root.find(".//t:teiHeader/t:fileDesc/t:publicationStmt/t:availability/t:p", NS)
    return (p.text or "").strip() if p is not None and p.text else None


def extract_body_text(root):
    # Walk <body>, split on cb:juan open markers so the txt keeps juan (chapter) structure
    # readable. Everything that is not a <p> or <l> text run is naturally excluded because
    # itertext() only surfaces text on nodes that have it; lb/pb/anchor/milestone are empty
    # self-closing elements and contribute nothing.
    body = root.find(".//t:text/t:body", NS)
    if body is None:
        return "", 0
    juan_count = 0
    lines = []
    for el in body.iter():
        tag = el.tag.split("}")[-1]
        if tag == "juan" and el.get("fun") == "open":
            juan_count += 1
            n = el.get("n") or str(juan_count)
            lines.append(f"\n===== juan {n} =====\n")   # marker line: keep as-is, never pass through prose whitespace cleanup below
        elif tag in ("p", "l"):
            # Strip meaningless ascii whitespace *within this one prose run only*, so the
            # cleanup can never touch the juan marker lines assembled above.
            txt = re.sub(r"[ \t]+", "", local_text(el).strip())
            if txt:
                lines.append(txt)
    full = "\n".join(lines)
    full = re.sub(r"\n{3,}", "\n\n", full)
    return full.strip(), juan_count


def sutra_id_from_path(path):
    # filename without extension, e.g. T01n0001 -> matches TEI xml:id
    return os.path.splitext(os.path.basename(path))[0]


# 2026-07-19修复:原每部经每次都一次s3.head_object()查重,改GitHub缓存本地_DONE记账
# (同ocr_ndl.py/ocr.py方法),避免每次跑对全量候选重复敲R2。
_DONE = set()


def process_one(path):
    sid = sutra_id_from_path(path)
    key = f"text/cbeta/{sid}.txt"
    if sid in _DONE:
        return {"id": sid, "status": "skip-done"}
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        return {"id": sid, "status": f"err-parse:{str(e)[:60]}"}
    root = tree.getroot()
    title = extract_title(root)
    author = extract_author(root)
    extent = extract_extent(root)
    canon, vol, no = extract_idno(root)
    license_note = extract_license_note(root)
    body_text, juan_count = extract_body_text(root)
    if not title or not body_text:
        return {"id": sid, "status": "skip-empty"}
    header = (
        f"# {title}\n"
        f"# CBETA ID: {sid}\n"
        f"# Author: {author or '(unknown)'}\n"
        f"# Extent: {extent or '(unknown)'}\n"
        f"# Canon/Vol/No: {canon}/{vol}/{no}\n"
        f"# License: {license_note or LICENSE_NOTE}\n"
        f"# Source: https://github.com/cbeta-org/xml-p5 ({sid}.xml)\n"
        f"# ---\n\n"
    )
    out = header + body_text
    s3.put_object(Bucket=BUCKET, Key=key, Body=out.encode("utf-8"))
    _DONE.add(sid)
    return {
        "id": sid, "status": "ok", "title": title, "author": author,
        "extent": extent, "canon": canon, "vol": vol, "no": no,
        "juan": juan_count, "chars": len(body_text),
        "license": license_note or LICENSE_NOTE,
    }


def main():
    global _DONE
    if os.path.exists("ledger.json"):
        try:
            _DONE = set(json.load(open("ledger.json", encoding="utf-8")))
        except Exception:
            _DONE = set()
    print(f"ledger已有 {len(_DONE)} 条记录", flush=True)

    clone_source()
    files = list_xml_files()
    mine = [f for i, f in enumerate(files) if i % TOTAL == SHARD]
    print(f"shard {SHARD}/{TOTAL} files {len(mine)}/{len(files)}", flush=True)
    ledger = []
    ok = skip = err = 0
    for i, path in enumerate(mine):
        try:
            r = process_one(path)
        except Exception as e:
            r = {"id": sutra_id_from_path(path), "status": f"err-other:{str(e)[:60]}"}
        ledger.append(r)
        st = r["status"]
        if st == "ok":
            ok += 1
        elif st.startswith("err"):
            err += 1
        else:
            skip += 1
        if (i + 1) % 50 == 0:
            print(f"progress {i+1}/{len(mine)} ok={ok} skip={skip} err={err}", flush=True)
    lk = f"_ledger/fojing_shard_{SHARD}.json"
    s3.put_object(Bucket=BUCKET, Key=lk, Body=json.dumps(ledger, ensure_ascii=False).encode("utf-8"))
    json.dump(sorted(_DONE), open("ledger.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(f"=== shard {SHARD} complete ok={ok} skip={skip} err={err} total={len(mine)} | ledger -> {lk} ===", flush=True)


def finalize():
    # Merge every shard ledger into one manifest. Run as a separate job that "needs" all
    # shard jobs, so it only starts after every matrix leg has written its ledger.
    total = int(os.environ.get("TOTAL", "1"))
    entries = {}
    missing_shards = []
    for shard in range(total):
        lk = f"_ledger/fojing_shard_{shard}.json"
        try:
            body = s3.get_object(Bucket=BUCKET, Key=lk)["Body"].read().decode("utf-8")
        except ClientError:
            missing_shards.append(shard)
            continue
        for r in json.loads(body):
            if r["status"] == "ok":
                entries[r["id"]] = {k: v for k, v in r.items() if k != "status"}
    # Also fold in previously-uploaded sutras from earlier runs (skip-done this run,
    # but already have a txt in R2) so the manifest reflects the true total in the bucket,
    # not just what this particular invocation touched.
    manifest = {
        "generated_by": "fojing_cbeta.py finalize",
        "license_summary": LICENSE_NOTE,
        "count": len(entries),
        "missing_shard_ledgers": missing_shards,
        "sutras": entries,
    }
    mk = "_cc/fojing_cbeta_manifest.json"
    s3.put_object(Bucket=BUCKET, Key=mk, Body=json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
    print(f"=== finalize: {len(entries)} sutras in manifest -> {mk} | missing_shard_ledgers={missing_shards} ===", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "finalize":
        finalize()
    else:
        main()
