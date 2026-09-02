# -*- coding: utf-8 -*-
"""
Direct-connect model fleet. Each provider is called on its own endpoint.

Why this replaces the gateway path (founder, 2026-07-26 21:2x:
"when will the gateway actually work? all that free compute sitting idle"):

Every batch job was pointed at our own /api/gateway/chat -- a Cloudflare Pages
Function built to serve the site's visitors. Two consequences, both measured:

  * 48 concurrent refine workers flattened it. Five providers reported 503 at once,
    which I misread as "five vendors went down" more than once that evening. Same
    moment, direct connection: 0.9s and fine.
  * It collapses every upstream error into GATEWAY_ALL_FAILED, so a rate limit, an
    exhausted balance and a wrong model name all look identical. Several wrong calls
    that day trace back to exactly that.

The standing rule already said batch work belongs on Actions, not on our own
front-end. Routing batch traffic back through the site's gateway broke it in spirit.

Cost note that matters more than any key (measured the same day): the same DeepSeek
model costs 12/24 yuan per million tokens via Xunfei versus 3/6 official -- and
v4-flash is 1/2 with 50x the concurrency. Always check the price table before
picking a lane.

Credentials come from env (GitHub secrets). Nothing is hard-coded.
"""
import json, os, random, re, time, urllib.error, urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def _keys(name):
    raw = os.environ.get(name, "")
    return [k.strip() for k in raw.split(",") if k.strip()]

# \u6bcf\u6761 lane = \u4e00\u4e2a\u53ef\u72ec\u7acb\u8c03\u5ea6\u7684\u5355\u5143(\u521b\u59cb\u4eba\u94a6\u5b9a\u7684\u547d\u540d\u6cd5:\u5e73\u53f0-\u578b\u53f7)\u3002
# concurrency / timeout \u6309\u5404\u5bb6\u5b9e\u6d4b\u813e\u6c14\u5206\u522b\u7ed9,\u4e0d\u518d\u4e00\u5957\u53c2\u6570\u6253\u5929\u4e0b\u3002
LANES = [
    {"id": "deepseek-v4-flash", "base": "https://api.deepseek.com",
     "model": "deepseek-v4-flash", "env": "DEEPSEEK_API_KEY",
     "concurrency": 12, "timeout": 120,
     "note": "1/2 yuan per M tokens, 2500 concurrent. The batch workhorse."},

    {"id": "nvidia-gemma-4-31b", "base": "https://integrate.api.nvidia.com/v1",
     "model": "google/gemma-4-31b-it", "env": "NVIDIA_KEYS",
     "concurrency": 4, "timeout": 120,
     "note": "Free. Zero hallucinations in the race."},

    {"id": "nvidia-mistral-nemotron", "base": "https://integrate.api.nvidia.com/v1",
     "model": "mistralai/mistral-nemotron", "env": "NVIDIA_KEYS",
     "concurrency": 4, "timeout": 120},

    {"id": "nvidia-gemma-3n-e4b", "base": "https://integrate.api.nvidia.com/v1",
     "model": "google/gemma-3n-e4b-it", "env": "NVIDIA_KEYS",
     "concurrency": 4, "timeout": 120,
     "note": "Highest real-world formula rate so far: 89% over 4144 rows."},

    {"id": "agnes-2.0-flash", "base": "https://apihub.agnes-ai.com/v1",
     "model": "agnes-2.0-flash", "env": "AGNES_API_KEY",
     "concurrency": 4, "timeout": 90},

    {"id": "zhipu-glm-4-flash", "base": "https://open.bigmodel.cn/api/paas/v4",
     "model": "glm-4-flash", "env": "ZHIPU_KEY",
     "concurrency": 3, "timeout": 120,
     "note": "Free tier. glm-5.x is paid -- do not switch it silently."},

    {"id": "cerebras-gemma-4-31b", "base": "https://api.cerebras.ai/v1",
     "model": "gemma-4-31b", "env": "CEREBRAS_KEYS",
     "concurrency": 2, "timeout": 90,
     "note": "Sits behind Cloudflare: a default python UA gets 403 error 1010, a "
             "browser UA gets through. Rate limits are tight, so keep concurrency low."},

    {"id": "sensenova-flash", "base": "https://token.sensenova.cn/v1",
     "model": "deepseek-v4-flash", "env": "SENSENOVA_KEYS",
     "concurrency": 3, "timeout": 120},
]

# Cross-check the list above against sueai/lanes.json, which lane-probe.yml
# refreshes daily and which calls itself the single source of truth.
#
# Before this hook, the two disagreed and nobody noticed: the probe had marked
# nvidia-gemma-4-31b / nvidia-mistral-nemotron / agnes-2.0-flash as `error` and
# nvidia-gemma-3n-e4b as `retired`, while this file kept dispatching to all four.
# The probe knew; the code that spends the quota did not read it.
#
# Observe-only by default -- it prints the disagreement on every run and drops
# nothing. Set LANES_ENFORCE=1 to actually drop the dead ones. Reason for not
# enforcing straight away: on the audit snapshot only 1 of 8 lanes was `active`,
# so enforcing blind would have taken this fleet from 8 lanes to 2 with no
# observation window. Watch a cycle of printed output first, then flip the flag.
#
# Fail-open throughout: unreadable lanes.json, or a lane absent from it, changes
# nothing. Unknown is not dead.
try:
    from lanes import filter_dead as _filter_dead_lanes
except ImportError:                                             # noqa: BLE001
    try:
        from scripts.lanes import filter_dead as _filter_dead_lanes
    except ImportError:
        _filter_dead_lanes = None

if _filter_dead_lanes is not None:
    LANES = _filter_dead_lanes(
        LANES,
        enforce=os.environ.get("LANES_ENFORCE", "") == "1",
        label="direct_fleet",
    )
else:
    print("[lanes] scripts/lanes.py not importable -- fleet unchanged", flush=True)

LANE_BY_ID = {l["id"]: l for l in LANES}

RATE_LIMITED = re.compile(r"rate.?limit|too many requests|quota|frequen|1010", re.I)
FATAL = re.compile(r"401|403|404|not found|no auth|unauthor|invalid.*key|insufficient|balance", re.I)


def call(lane_id, messages, temperature=0.1, max_tokens=500, max_retry=4):
    """Call one lane. Retries on rate limits and timeouts, gives up immediately on
    auth/balance errors -- retrying those just burns the clock and hides the cause."""
    lane = LANE_BY_ID[lane_id]
    keys = _keys(lane["env"])
    if not keys:
        raise RuntimeError("no key in env %s for lane %s" % (lane["env"], lane_id))
    timeout = lane["timeout"]
    last = None
    for attempt in range(max_retry):
        key = keys[attempt % len(keys)]        # \u591a\u628a key \u65f6\u8f6e\u6362,\u5355\u628a\u65f6\u5c31\u662f\u5b83\u81ea\u5df1
        body = json.dumps({"model": lane["model"], "messages": messages,
                           "temperature": temperature, "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(
            lane["base"].rstrip("/") + "/chat/completions", body,
            {"Authorization": "Bearer " + key, "Content-Type": "application/json",
             "User-Agent": UA, "Accept": "application/json"})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
            return r["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = e.read().decode("utf-8", "replace")[:200]
            except Exception: pass
            last = RuntimeError("HTTP %d %s" % (e.code, detail[:120]))
            blob = str(e.code) + detail
            if e.code == 429 or RATE_LIMITED.search(blob):
                time.sleep(min(45, 3 * (2 ** attempt)))
                continue
            if FATAL.search(blob):
                raise last                      # \u9274\u6743/\u4f59\u989d:\u91cd\u8bd5\u65e0\u610f\u4e49,\u76f4\u63a5\u66b4\u9732
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            last = e
            if "timed out" in str(e).lower():
                timeout = int(timeout * 1.5)
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("unknown failure on " + lane_id)


def healthy_lanes(probe=True):
    """Return lanes that have a key and (optionally) answer a 1-token probe.
    Probed serially on purpose: firing all lanes at once is how I kept manufacturing
    my own outages and then reading them as vendor failures."""
    out = []
    for lane in LANES:
        if not _keys(lane["env"]):
            continue
        if not probe:
            out.append(lane["id"]); continue
        try:
            call(lane["id"], [{"role": "user", "content": "hi"}], max_tokens=4, max_retry=1)
            out.append(lane["id"])
        except Exception:
            pass
        time.sleep(1)
    return out
