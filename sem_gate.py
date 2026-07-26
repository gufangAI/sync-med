# coding: utf-8
# Semantic gate for the clean-text ingest (2026-07-27).
#
# The rule gate in clean_embed_ingest.py rejects text that is objectively
# degenerate -- repeated glyphs, empty rows, symbol soup. It cannot tell a
# formula from a colophon. Measured the same day on the index the site queries:
# the top hit for a Guizhi Tang query was a preface signature line. Correct
# Chinese, correct character ratio, zero retrieval value -- and stored twice.
#
# Deciding "is this actually medical content" is a semantic call, so it goes to
# a model. Three constraints make that safe to run unattended every six hours:
#
#   * The model only CLASSIFIES. It never rewrites, translates or summarises.
#     What gets embedded is always the original chunk, byte for byte.
#   * The reply must be a JSON array of 0/1 whose length equals the batch size.
#     Anything else -- a short array, stray prose, a refusal, a code fence with
#     commentary -- and the ENTIRE batch is kept. A malformed reply must never
#     be read as "delete these".
#   * Every failure path keeps the chunks. Discarding real text here is
#     unrecoverable; keeping a colophon costs one vector.
#
# The prompt also tells the model to answer 1 when unsure, so the bias runs the
# same direction at both ends of the pipe.
#
# Free pools only, per standing rule. NVIDIA leads because gemma-4-31b measured
# zero hallucinations on this corpus during the 2026-07-26 model race; Agnes and
# Cerebras sit behind it. No metered provider is listed here on purpose.
import json
import os
import re

import requests

GATE_ON = os.environ.get("SEM_GATE", "1") not in ("0", "false", "no", "")
BATCH = int(os.environ.get("SEM_BATCH", "10"))
MAX_CHARS = int(os.environ.get("SEM_MAX_CHARS", "320"))   # sent to the classifier only
TIMEOUT = int(os.environ.get("SEM_TIMEOUT", "90"))

LANES = [
    {"id": "nvidia", "base": "https://integrate.api.nvidia.com/v1",
     "model": os.environ.get("SEM_NVIDIA_MODEL", "google/gemma-4-31b-it"),
     "env": "NVIDIA_API_KEY"},
    {"id": "agnes",
     "base": os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1"),
     "model": os.environ.get("AGNES_TEXT_MODEL", "agnes-2.0-flash"),
     "env": "AGNES_API_KEY"},
    {"id": "cerebras", "base": "https://api.cerebras.ai/v1",
     "model": os.environ.get("SEM_CEREBRAS_MODEL", "gemma-4-31b"),
     "env": "CEREBRAS_KEYS"},
]

# Kept escaped, not literal: this repository is public.
SYS = ("\u4f60\u662f\u4e2d\u533b\u53e4\u7c4d\u8bed\u6599\u7b5b\u9009\u5458\u3002\u4e0b\u9762\u7ed9\u4f60\u82e5\u5e72\u6bb5\u53e4\u7c4d\u6587\u672c\uff0c\u9010\u6bb5\u5224\u65ad\u5b83\u662f\u5426\u503c\u5f97\u8fdb\u5165\u4e2d\u533b\u68c0\u7d22\u5e93\u3002\n\u503c\u5f97(\u8f93\u51fa1)\uff1a\u65b9\u5242\u7ec4\u6210\u3001\u836f\u7269\u529f\u6548\u3001\u75c5\u75c7\u8bba\u8ff0\u3001\u533b\u6848\u75c5\u4f8b\u3001\u9488\u7078\u7ecf\u7edc\u3001\u8109\u8bca\u820c\u8bca\u3001\u70ae\u5236\u65b9\u6cd5\u7b49\u5b9e\u8d28\u533b\u5b66\u5185\u5bb9\u3002\n\u4e0d\u503c\u5f97(\u8f93\u51fa0)\uff1a\u5e8f\u8a00\u8dcb\u6587\u3001\u9898\u8bcd\u8d5e\u8bed\u3001\u76ee\u5f55\u51e1\u4f8b\u3001\u724c\u8bb0\u7248\u6743\u3001\u85cf\u4e66\u5370\u8bb0\u3001\u6350\u8d60\u9898\u540d\u3001\u9875\u7709\u9875\u811a\u3001\u520a\u523b\u8005\u59d3\u540d\u7f57\u5217\u3001\u7eaf\u5e74\u53f7\u5e72\u652f\u3001\u4ee5\u53ca\u65e0\u6cd5\u8fa8\u8ba4\u7684\u6b8b\u7f3a\u6587\u5b57\u3002\n\u5224\u65ad\u4e0d\u51c6\u65f6\u8f93\u51fa1(\u5b81\u53ef\u591a\u7559\uff0c\u4e0d\u53ef\u8bef\u5220)\u3002\n\u53ea\u8f93\u51fa\u4e00\u4e2aJSON\u6570\u7ec4\uff0c\u5143\u7d20\u4e3a0\u62161\uff0c\u957f\u5ea6\u5fc5\u987b\u7b49\u4e8e\u8f93\u5165\u6bb5\u6570\uff0c\u4e0d\u8981\u4efb\u4f55\u89e3\u91ca\u3002\n\u4f8b\uff1a\u8f93\u51653\u6bb5 \u2192 \u8f93\u51fa [1,0,1]")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ARRAY = re.compile(r"\[[^\]]*\]")


def _key(lane):
    raw = (os.environ.get(lane["env"]) or "").strip()
    return raw.split(",")[0].strip() if raw else ""


def classify(lane, texts):
    """Ask one lane to classify a batch. Returns [0/1]*len(texts), or None when
    the reply cannot be trusted -- callers must treat None as 'keep all'."""
    key = _key(lane)
    if not key:
        return None
    numbered = "\n\n".join(
        "[%d] %s" % (i + 1, (t or "")[:MAX_CHARS]) for i, t in enumerate(texts))
    s = requests.Session()
    s.trust_env = False                      # never inherit a proxy from the runner
    r = s.post(lane["base"].rstrip("/") + "/chat/completions",
               headers={"Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "User-Agent": UA},
               json={"model": lane["model"], "temperature": 0, "max_tokens": 200,
                     "messages": [{"role": "system", "content": SYS},
                                  {"role": "user", "content": numbered}]},
               timeout=TIMEOUT)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    m = _ARRAY.search(txt or "")
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != len(texts):
        return None                          # length mismatch -> untrustworthy
    out = []
    for v in arr:
        sv = str(v).strip()
        if sv not in ("0", "1"):
            return None                      # unparseable element -> keep all
        out.append(int(sv))
    return out


def keep_flags(texts, log=print):
    """True = embed this chunk. Keeps everything on any failure: an unreachable
    classifier must not silently shrink the corpus."""
    if not GATE_ON or not texts:
        return [True] * len(texts)
    for lane in LANES:
        try:
            verdict = classify(lane, texts)
        except Exception as e:
            log("  [sem] lane %s failed: %s: %s"
                % (lane["id"], type(e).__name__, str(e)[:70]))
            continue
        if verdict is not None:
            return [bool(v) for v in verdict]
        log("  [sem] lane %s returned an unusable verdict -- keeping batch" % lane["id"])
    log("  [sem] no lane could classify -- keeping all %d chunk(s)" % len(texts))
    return [True] * len(texts)


def filter_chunks(pairs, log=print):
    """pairs = [(orig_index, text)]. Returns (kept_pairs, dropped_count).
    Batched so one request covers BATCH chunks instead of one each."""
    if not GATE_ON or not pairs:
        return list(pairs), 0
    kept = []
    for i in range(0, len(pairs), BATCH):
        batch = pairs[i:i + BATCH]
        flags = keep_flags([t for _, t in batch], log=log)
        kept.extend(p for p, f in zip(batch, flags) if f)
    return kept, len(pairs) - len(kept)
