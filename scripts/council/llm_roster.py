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

Cost posture -- FOUNDER-SET REDLINE, 2026-07-25 (read before adding a provider):
  EVERY seat here must come from an internal FREE pool. Not "cheap", not "inside
  a free allocation" -- free, with the additional property that running out
  STOPS SERVICE rather than silently billing us.

  What changed on 2026-07-25 and why:
    the roster used to carry four `@cf/*` Cloudflare Workers AI seats plus a CF
    judge, metered against a neuron budget. The 2026-07-25 run proves why that
    was wrong in practice, not just in principle: three of the four FREE seats
    were dead that run (xunfei answered AppIdNoAuthError, agnes answered 429,
    gemini was pointed at a retired model id) and CF quietly filled 4 of the 5
    chairs. So the pipeline was not "using a metered source as a last resort",
    it was running ON the metered source while our own free fleet sat broken.
    The founder's ruling is exactly that distinction: the sin is not spending,
    it is reaching for a billed source while the free ones are not even wired
    up. Fix the free fleet; do not buy your way past a wiring bug.

  Cloudflare Workers AI specifically: it is SILENTLY METERED -- past the free
  10,000 neurons/day it keeps answering and bills the card. Every vendor on the
  bench below is the other kind: quota out = HTTP 429/402 = service stops. That
  difference in FAILURE MODE, not the price tag, is why CF is out and these are
  in. It burned ~$8 on 2026-07-09, `code-self-audit.yml` CI-bans `env.AI.run` /
  `@cf/*` inside guyaofang-web, and guyaofang-web's own gateway pulled all three
  CF providers out of its fallback chain the same evening. This roster now
  matches that posture.

  The CF transport (`_cf_chat`) is deliberately KEPT but has no reachable call
  path: `_bench()` never emits a cf seat and `call()` refuses kind=="cf". Same
  handling as `_providers.js`: "keep the feature, remove the billing part".
  Re-enabling is the founder's call, not the CTO's -- see CF_ENABLED below.

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

# Cloudflare Workers AI: DISABLED BY THE FOUNDER, 2026-07-25. Two independent
# locks, on purpose -- a single env var is too easy to flip by accident:
#   1. CF_ENABLED is a source constant, not an env var. Restoring it is a code
#      change that shows up in a diff and needs the founder's word.
#   2. the budget defaults to 0 neurons, so even a hand-built cf seat spends
#      nothing before BudgetExhausted stops it.
# `CF_NEURON_BUDGET` stays a module attribute because council.py prints it.
CF_ENABLED = False
CF_NEURON_BUDGET = float(os.environ.get("CF_NEURON_BUDGET", "0"))

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


def _openai_chat(base, key, model, system, user, max_tokens, temperature, timeout,
                 extra=None):
    """OpenAI-compatible /chat/completions -- every seat on the free bench
    speaks this (zhipu, agnes, gemini's compat endpoint, nvidia NIM, modelscope,
    sensenova, longcat, cerebras, siliconflow).

    `extra` is merged into the request body at top level, mirroring
    guyaofang-web `_providers.js` `extra_body`. Two seats need it and both were
    learned the hard way in production, so do not "simplify" them away:
      - modelscope: {"enable_thinking": false} or the answer arrives polluted
        with chain-of-thought;
      - gemini-2.5-flash: {"reasoning_effort": "low"} or thinking eats the
        token budget and content comes back empty (the nested google:{} form
        is a 400 on the OpenAI-compat endpoint)."""
    payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if extra:
        payload.update(extra)
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


def _cf_chat(base, key, model, system, user, max_tokens, temperature, timeout,
             extra=None):
    """Cloudflare Workers AI REST. `base` is the account id. METERED.

    DEAD CODE ON PURPOSE (2026-07-25): nothing calls this -- `_bench()` emits no
    cf seat and `call()` refuses kind=="cf". Kept, not deleted, because the
    founder's standing instruction for billed integrations is "keep the
    feature, remove the billing part" -- so a future re-enable is a one-line
    decision with the transport already proven, rather than a rewrite. Turning
    it back on takes CF_ENABLED = True plus a real CF_NEURON_BUDGET, and that
    is the founder's call."""
    if not CF_ENABLED:
        raise RuntimeError("cf transport disabled by founder's order 2026-07-25")
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
    # two shapes in the wild: the older models answer {"response": "..."},
    # the newer ones (gpt-oss, glm-5.2, qwen3) answer OpenAI-style choices[]
    # with no "response" key at all. Reading only the first shape silently
    # turned the strongest models into "empty response" failures.
    txt = res.get("response") or ""
    if not txt and res.get("choices"):
        msg = (res["choices"][0] or {}).get("message") or {}
        txt = msg.get("content") or msg.get("reasoning_content") or ""
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
#
# EVERY entry below is a free pool of the "runs out = stops serving" kind, and
# every base/model pair is copied from a config already proven in production
# (guyaofang-web `functions/api/gateway/_providers.js`, cross-checked against the
# internal free-vs-paid model ledger dated 2026-07-12). Do not invent endpoints
# here: every one of these was picked by measurement, not by reading a docs page.
#
# NOT on this bench, deliberately:
#   - any `@cf/*` seat -- see the module docstring.
#   - xunfei MaaS chat (`xopqwen36v35b` et al). It reads free, and the council
#     used to seat it, but the ledger classes the MaaS chat flagship as PAID
#     (the founder topped that account up with 100 CNY on 2026-07-10) and
#     `_providers.js` writes "never put it in the fallback chain (it bills!)".
#     Swapping metered CF for metered xunfei would miss the point entirely. It
#     also never actually answered: AppIdNoAuthError on both key pools, cloud
#     2026-07-25 and local re-probe the same evening. Xunfei's FREE products
#     (embeddings, OCR) are untouched and stay in use elsewhere in this repo.
#
# "house" = who trained the weights, not who hosts them. Two hosts serving the
# same family is not a race. Note cerebras serves `zai-glm-4.7`, which is a GLM
# model, i.e. the SAME house as zhipu -- labelled honestly so the diversity
# invariant cannot be fooled by picking a different vendor's front door.
_FREE_BENCH = [
    # (id, zh vendor-label key, house, model, base, env var(s) with the key, extra, probe_tokens)
    ("sensenova", "v_sensenova", "deepseek", "deepseek-v4-flash",
     "https://token.sensenova.cn/v1", ("SENSENOVA_API_KEY",), None, 512),
    ("longcat", "v_longcat", "longcat", "LongCat-2.0",
     "https://api.longcat.chat/openai/v1", ("LONGCAT_API_KEY",), None, 512),
    ("zhipu", "v_zhipu", "zhipu", "glm-4-flash",
     "https://open.bigmodel.cn/api/paas/v4", ("ZHIPU_API_KEY",), None, 8),
    ("agnes", "v_agnes", "agnes", "agnes-2.0-flash",
     "https://apihub.agnes-ai.com/v1", ("AGNES_API_KEY",), None, 8),
    ("gemini", "v_gemini", "google", "gemini-2.5-flash",
     "https://generativelanguage.googleapis.com/v1beta/openai", ("GEMINI_API_KEY",),
     {"reasoning_effort": "low"}, 512),
    ("nvidia", "v_nvidia", "qwen", "qwen/qwen3-next-80b-a3b-instruct",
     "https://integrate.api.nvidia.com/v1", ("NVIDIA_API_KEY",), None, 8),
    ("modelscope", "v_modelscope", "qwen", "Qwen/Qwen3-8B",
     "https://api-inference.modelscope.cn/v1", ("MODELSCOPE_API_KEY",),
     {"enable_thinking": False}, 8),
    ("cerebras", "v_cerebras", "zhipu", "zai-glm-4.7",
     "https://api.cerebras.ai/v1", ("CEREBRAS_API_KEY",), None, 1024),
    ("siliconflow", "v_siliconflow", "qwen", "Qwen/Qwen2.5-7B-Instruct",
     "https://api.siliconflow.cn/v1", ("SILICONFLOW_KEY", "SILICONFLOW_API_KEY"), None, 8),
]


def _bench():
    """Seats whose credentials are actually present, in priority order.

    A vendor with no secret is simply absent -- no error, no placeholder. That
    is what makes adding a pool a pure secrets operation: `gh secret set
    LONGCAT_API_KEY` and the seat appears on the next run with no code change.

    Endpoint and model are overridable per seat without a deploy, same idea as
    the gateway's GW_<PROVIDER>_BASE / _MODEL: <ID>_API_BASE overrides the base,
    <ID>_MODEL or <ID>_TEXT_MODEL overrides the model. Those exact names are why
    the agnes seat keeps honouring the AGNES_API_BASE / AGNES_TEXT_MODEL secrets
    this repo already carries -- the defaults below happen to match today, and a
    vendor renaming a model must stay a secret edit, not a code push."""
    seats = []
    for sid, vkey, house, model, base, env_names, extra, ptok in _FREE_BENCH:
        raw = ""
        for name in env_names:
            raw = os.environ.get(name, "").strip()
            if raw:
                break
        if not raw:
            continue
        # comma/newline separated pools are supported everywhere: several of
        # these vendors are two founder accounts sharing one secret slot, and
        # one dead key must not sink a whole vendor (see probe()).
        keys = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
        up = sid.upper()
        seats.append({
            "id": sid, "vendor": t(vkey), "house": house,
            "model": (os.environ.get(up + "_MODEL", "").strip()
                      or os.environ.get(up + "_TEXT_MODEL", "").strip() or model),
            "kind": "openai", "keys": keys,
            "base": os.environ.get(up + "_API_BASE", "").strip() or base,
            "extra": extra, "probe_tokens": ptok,
        })
    return seats


def free_bench_ids():
    """Every vendor this council knows how to seat, wired or not -- so the run
    log can say which pools are missing credentials instead of leaving the
    founder to guess why only three houses showed up."""
    return [row[0] for row in _FREE_BENCH]

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
    # belt to `_bench()`'s braces: even a hand-built cf seat stops here, so no
    # future edit can reintroduce a metered call path by accident.
    if seat.get("kind") == "cf" and not CF_ENABLED:
        raise RuntimeError("cf seat %s refused: disabled by founder's order 2026-07-25"
                           % seat.get("id"))
    last = None
    for attempt in range(retries + 1):
        n = _reserve_call()
        key = _pick_key(seat)
        t0 = time.time()
        try:
            fn = _cf_chat if seat["kind"] == "cf" else _openai_chat
            txt, usage = fn(seat["base"], key, seat["model"], system, user,
                            max_tokens, temperature, timeout, seat.get("extra"))
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
                # free pools rate-limit per MINUTE, so a 3s nap just buys a
                # second 429. Measured 2026-07-25: agnes and zhipu both answer
                # 429 under burst while the key itself is perfectly healthy.
                is_429 = isinstance(e, urllib.error.HTTPError) and e.code == 429
                time.sleep((20 if is_429 else 3) + attempt * 4)
    raise RuntimeError("seat %s failed: %s" % (seat["id"], last))


def probe(seat, timeout=45, retries=2):
    """Liveness check. Costs real calls against the cap on purpose -- a probe
    that lies about the budget is worse than no probe.

    For a multi-key seat every key is probed and the seat is narrowed IN PLACE
    to the keys that actually answered: several of these pools are two founder
    accounts sharing one secret slot, so one dead key must not sink a whole
    vendor.

    Two things this got wrong before and must not get wrong again:
      - `max_tokens=8` for everyone. Reasoning-family seats (sensenova's
        deepseek, longcat, cerebras' GLM-4.7, gemini-2.5-flash) spend the whole
        allowance inside the reasoning channel and return EMPTY content, which
        `call()` correctly treats as failure -- so a perfectly healthy vendor
        probed as dead. Hence per-seat `probe_tokens`. Probe tokens are free
        here; a mis-benched vendor is not.
      - one retry with a 6s nap. Free pools rate-limit per minute; 6s lands in
        the same window. Now 2 retries with a widening wait.
    """
    live = []
    ptok = seat.get("probe_tokens") or 8
    for key in seat["keys"]:
        one = dict(seat)
        one["keys"] = [key]
        for attempt in range(retries + 1):
            try:
                call(one, "reply with OK", "OK?", max_tokens=ptok, temperature=0.0,
                     timeout=timeout, stage="probe", tag=seat["id"], retries=0)
                live.append(key)
                break
            except BudgetExhausted:
                return bool(live)
            except Exception:
                if attempt < retries:
                    time.sleep(12 + attempt * 12)
    if live:
        seat["keys"] = live
        return True
    return False


def assemble(n_racers=4):
    """Probe the bench in priority order and seat `n_racers` distinct houses,
    then seat a judge from a house that is preferably not racing.

    Returns (racers, judge, bench_report)."""
    bench = _bench()
    report, racers, houses, rest = [], [], set(), []
    for seat in bench:
        if len(racers) >= n_racers:
            rest.append(seat)          # unprobed: may yet be needed as referee
            continue
        if seat["house"] in houses:
            report.append({**seat, "alive": None, "role": "bench",
                           "reason": "house already seated"})
            continue
        alive = probe(seat)
        report.append({**seat, "alive": alive, "role": "racer" if alive else "bench",
                       "reason": "" if alive else "probe failed"})
        if alive:
            racers.append(seat)
            houses.add(seat["house"])

    # referee: a house that is NOT racing, so it is not grading its own homework.
    judge = None
    for seat in rest:
        if seat["house"] in houses:
            report.append({**seat, "alive": None, "role": "bench",
                           "reason": "house already seated"})
            continue
        if probe(seat):
            judge = seat
            report.append({**seat, "alive": True, "role": "judge", "reason": ""})
            break
        report.append({**seat, "alive": False, "role": "bench", "reason": "probe failed"})
    if judge is None and len(racers) >= 3:
        # Only enough live houses to fill the grid. Trade the LOWEST-priority
        # racer for a real referee rather than keep a full grid and let a
        # contestant grade its own plan -- a race of 3 with an impartial judge
        # beats a race of 4 whose winner is picked by one of the four. Never
        # drops below 2 racers: below that there is no argument left to judge.
        judge = racers.pop()
        houses.discard(judge["house"])
        for row in report:
            if row.get("id") == judge["id"] and row.get("role") == "racer":
                row["role"] = "judge"
                row["reason"] = "moved off the grid to referee independently"
    if judge is None and racers:
        # last resort: the strongest racer also referees, and the report says so
        judge = dict(racers[0])
        judge["id"] = racers[0]["id"] + "-judge"
        report.append({**judge, "alive": True, "role": "judge",
                       "reason": "fallback: no independent judge available"})
    return racers, judge, report
