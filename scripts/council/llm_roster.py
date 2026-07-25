# -*- coding: utf-8 -*-
"""Multi-vendor model roster for the SueAI council -- the "Hermes layer" that
lets one pipeline drive many vendors instead of being welded to one API.

Why a roster and not a fixed list of 4:
  a live probe on 2026-07-25 found xunfei answering AppIdNoAuthError on one key
  and agnes answering 429 (free-tier rate limit) *at the same moment*. A hard
  4-model list would have produced a 2-model "race", i.e. a fake council. So
  seats are filled at run time from a priority bench, and the only invariant
  enforced is VENDOR DIVERSITY: no two racers may come from the same vendor,
  because 4 seats of the same house is groupthink wearing four hats.

Cost posture (this repo has scar tissue here -- read before adding a provider):
  - xunfei / zhipu / agnes are free-quota pools; they cost nothing.
  - Cloudflare Workers AI is METERED. It burned ~$8 on 2026-07-09 when wired
    into production request paths, and `code-self-audit.yml` now CI-bans
    `env.AI.run` / `@cf/*` inside guyaofang-web for exactly that reason. That
    redline is about the *production request path*; a once-a-day batch job is a
    different animal, but "different animal" is not "free". So every CF call
    here is metered against a hard neuron budget read from the response's own
    `neurons` field, and CF is cut off mid-run the moment the budget is spent.
    Measured 2026-07-25: 1644 in / 376 out on llama-3.3-70b-fp8-fast = 120.85
    neurons, and the free allocation is 10,000 neurons/day -- so the default
    6000-neuron budget keeps a full council run inside the free tier with room
    to spare, and the report prints the number actually spent.

Every call is logged (seat, stage, latency, tokens, ok/err) so the report can
state real usage instead of an estimate.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from zh import t

# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------
MAX_CALLS = int(os.environ.get("MAX_LLM_CALLS", "60"))
CF_NEURON_BUDGET = float(os.environ.get("CF_NEURON_BUDGET", "6000"))

_lock = threading.Lock()
_state = {"calls": 0, "neurons": 0.0, "fails": 0}
CALL_LOG = []


class BudgetExhausted(RuntimeError):
    pass


def budget_snapshot():
    with _lock:
        return dict(_state)


def _reserve_call():
    with _lock:
        if _state["calls"] >= MAX_CALLS:
            raise BudgetExhausted("LLM call cap reached: %d" % MAX_CALLS)
        _state["calls"] += 1
        return _state["calls"]


def _cf_budget_left():
    with _lock:
        return CF_NEURON_BUDGET - _state["neurons"]


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"^\s*<think>.*", re.DOTALL | re.IGNORECASE)


def strip_think(txt):
    """deepseek-r1 / qwq style reasoning models emit <think>...</think> before
    the answer; an unterminated block (hit max_tokens mid-thought) means the
    answer never arrived, so that degrades to empty rather than to raw CoT."""
    if not txt:
        return ""
    txt = _THINK_RE.sub("", txt)
    if _OPEN_THINK_RE.match(txt):
        return ""
    return txt.strip()


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read().decode("utf-8")), resp.headers


def _openai_chat(base, key, model, system, user, max_tokens, temperature, timeout):
    """OpenAI-compatible /chat/completions -- xunfei, zhipu, agnes, gemini's
    compat endpoint and nvidia all speak this."""
    payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    d, _ = _post_json(base.rstrip("/") + "/chat/completions", payload,
                      {"Authorization": "Bearer " + key,
                       "Content-Type": "application/json"}, timeout)
    msg = d["choices"][0]["message"]
    txt = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
    usage = d.get("usage") or {}
    return strip_think(txt), {
        "in": usage.get("prompt_tokens", 0),
        "out": usage.get("completion_tokens", 0),
        "neurons": 0.0,
    }


def _cf_chat(base, key, model, system, user, max_tokens, temperature, timeout):
    """Cloudflare Workers AI REST. `base` is the account id. Metered."""
    left = _cf_budget_left()
    if left <= 200:                       # one call has never measured under ~120
        raise BudgetExhausted("CF neuron budget spent (%.0f/%.0f)"
                              % (CF_NEURON_BUDGET - left, CF_NEURON_BUDGET))
    url = ("https://api.cloudflare.com/client/v4/accounts/%s/ai/run/%s" % (base, model))
    payload = {"messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "max_tokens": max_tokens, "temperature": temperature}
    d, hdrs = _post_json(url, payload, {"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"}, timeout)
    if not d.get("success", True):
        raise RuntimeError("cf: " + str(d.get("errors"))[:120])
    res = d.get("result") or {}
    txt = res.get("response") or ""
    usage = res.get("usage") or {}
    neurons = float(usage.get("neurons") or hdrs.get("cf-ai-neurons") or 0.0)
    with _lock:
        _state["neurons"] += neurons
    return strip_think(txt), {
        "in": usage.get("prompt_tokens", 0),
        "out": usage.get("completion_tokens", 0),
        "neurons": neurons,
    }


# ---------------------------------------------------------------------------
# seat bench
# ---------------------------------------------------------------------------
# vendor = the house that MADE the weights (not who hosts them): two seats that
# both resolve to Qwen are not a real race even via two different hosts.
def _bench():
    xf_keys = [k.strip() for k in os.environ.get("XF_KEYS", "").replace("\n", ",").split(",") if k.strip()]
    cf_acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    cf_tok = (os.environ.get("CLOUDFLARE_API_TOKEN", "")
              or os.environ.get("CF_AI_TOKEN", "")).strip()
    seats = []
    if xf_keys:
        seats.append({"id": "xf", "vendor": t("v_xf"), "house": "qwen",
                      "model": os.environ.get("XF_MODEL", "xopqwen36v35b"),
                      "kind": "openai", "keys": xf_keys,
                      "base": "https://maas-api.cn-huabei-1.xf-yun.com/v2"})
    if os.environ.get("ZHIPU_API_KEY", "").strip():
        seats.append({"id": "zhipu", "vendor": t("v_zhipu"), "house": "zhipu",
                      "model": os.environ.get("ZHIPU_MODEL", "glm-4-flash"),
                      "kind": "openai", "keys": [os.environ["ZHIPU_API_KEY"].strip()],
                      "base": "https://open.bigmodel.cn/api/paas/v4"})
    if os.environ.get("AGNES_API_KEY", "").strip() and os.environ.get("AGNES_TEXT_MODEL", "").strip():
        seats.append({"id": "agnes", "vendor": "agnes", "house": "agnes",
                      "model": os.environ["AGNES_TEXT_MODEL"].strip(),
                      "kind": "openai", "keys": [os.environ["AGNES_API_KEY"].strip()],
                      "base": os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com/v1")})
    if cf_acc and cf_tok:
        seats.append({"id": "cf-llama70b", "vendor": "Meta Llama-3.3-70B (CF)", "house": "meta",
                      "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                      "kind": "cf", "keys": [cf_tok], "base": cf_acc})
    if os.environ.get("GEMINI_API_KEY", "").strip():
        seats.append({"id": "gemini", "vendor": "Google Gemini", "house": "google",
                      "model": os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
                      "kind": "openai", "keys": [os.environ["GEMINI_API_KEY"].strip()],
                      "base": "https://generativelanguage.googleapis.com/v1beta/openai"})
    if cf_acc and cf_tok:
        # bench only: different house (Mistral) so it can legally replace a
        # dead seat without breaking the vendor-diversity invariant
        seats.append({"id": "cf-mistral", "vendor": "Mistral-Small-24B (CF)", "house": "mistral",
                      "model": "@cf/mistralai/mistral-small-3.1-24b-instruct",
                      "kind": "cf", "keys": [cf_tok], "base": cf_acc})
    if os.environ.get("NVIDIA_API_KEY", "").strip():
        seats.append({"id": "nvidia", "vendor": "NVIDIA NIM", "house": "nvidia",
                      "model": os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
                      "kind": "openai", "keys": [os.environ["NVIDIA_API_KEY"].strip()],
                      "base": "https://integrate.api.nvidia.com/v1"})
    return seats


# judge preference: a house that is NOT racing, so the referee is not grading
# its own homework. Falls back down the list until one probes alive.
JUDGE_PREF = [
    {"id": "cf-gptoss120b", "vendor": "OpenAI gpt-oss-120b (CF)", "house": "openai",
     "model": "@cf/openai/gpt-oss-120b", "kind": "cf"},
    {"id": "cf-llama70b", "vendor": "Meta Llama-3.3-70B (CF)", "house": "meta",
     "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast", "kind": "cf"},
]

_key_rr = {}


def _pick_key(seat):
    """Round-robin within a seat's key pool (xunfei ships several keys and they
    do not all carry the same model grants)."""
    with _lock:
        i = _key_rr.get(seat["id"], 0)
        _key_rr[seat["id"]] = i + 1
    return seat["keys"][i % len(seat["keys"])]


def call(seat, system, user, max_tokens=1200, temperature=0.6, timeout=150,
         stage="", tag="", retries=1):
    """One logged, budgeted LLM call. Raises on final failure."""
    last = None
    for attempt in range(retries + 1):
        n = _reserve_call()
        key = _pick_key(seat)
        t0 = time.time()
        try:
            fn = _cf_chat if seat["kind"] == "cf" else _openai_chat
            txt, usage = fn(seat["base"], key, seat["model"], system, user,
                            max_tokens, temperature, timeout)
            dt = time.time() - t0
            if not txt.strip():
                raise RuntimeError("empty response")
            CALL_LOG.append({"n": n, "seat": seat["id"], "stage": stage, "tag": tag,
                             "sec": round(dt, 1), "ok": True, **usage})
            return txt
        except BudgetExhausted:
            raise
        except Exception as e:
            dt = time.time() - t0
            detail = "%s:%s" % (type(e).__name__, str(e)[:90])
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail += " " + e.read().decode("utf-8", "replace")[:120]
                except Exception:
                    pass
            last = detail
            with _lock:
                _state["fails"] += 1
            CALL_LOG.append({"n": n, "seat": seat["id"], "stage": stage, "tag": tag,
                             "sec": round(dt, 1), "ok": False, "err": detail,
                             "in": 0, "out": 0, "neurons": 0.0})
            if attempt < retries:
                time.sleep(3 + attempt * 4)
    raise RuntimeError("seat %s failed: %s" % (seat["id"], last))


def probe(seat, timeout=45):
    """Cheap liveness check. Costs one call against the cap on purpose -- a
    probe that lies about the budget is worse than no probe."""
    try:
        call(seat, "reply with OK", "OK?", max_tokens=8, temperature=0.0,
             timeout=timeout, stage="probe", tag=seat["id"], retries=0)
        return True
    except Exception:
        return False


def assemble(n_racers=4):
    """Probe the bench in priority order and seat `n_racers` distinct houses,
    then seat a judge from a house that is preferably not racing.

    Returns (racers, judge, bench_report)."""
    bench = _bench()
    report, racers, houses = [], [], set()
    for seat in bench:
        if len(racers) >= n_racers:
            report.append({**seat, "alive": None, "role": "bench", "reason": "seats full"})
            continue
        if seat["house"] in houses:
            report.append({**seat, "alive": None, "role": "bench", "reason": "house already seated"})
            continue
        alive = probe(seat)
        report.append({**seat, "alive": alive, "role": "racer" if alive else "bench",
                       "reason": "" if alive else "probe failed"})
        if alive:
            racers.append(seat)
            houses.add(seat["house"])

    judge = None
    cf_acc = os.environ.get("CF_ACCOUNT_ID", "").strip()
    cf_tok = (os.environ.get("CLOUDFLARE_API_TOKEN", "") or os.environ.get("CF_AI_TOKEN", "")).strip()
    for cand in JUDGE_PREF:
        if not (cf_acc and cf_tok):
            break
        seat = {**cand, "keys": [cf_tok], "base": cf_acc}
        if probe(seat):
            judge = seat
            report.append({**seat, "alive": True, "role": "judge", "reason": ""})
            break
        report.append({**seat, "alive": False, "role": "bench", "reason": "probe failed"})
    if judge is None and racers:
        # last resort: the strongest racer also referees, and the report says so
        judge = dict(racers[0])
        judge["id"] = racers[0]["id"] + "-judge"
        report.append({**judge, "alive": True, "role": "judge",
                       "reason": "fallback: no independent judge available"})
    return racers, judge, report
