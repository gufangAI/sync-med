# Model fleet for the formula refine pipeline.
#
# Founder, 2026-07-26: "every model on every platform should be wired up --
# name them NVIDIA-QWEN3, NVIDIA-QWEN3.5 and so on, not just one NVIDIA".
# Before this, one platform meant one hard-coded model: 771 models on the books,
# 17 actually reachable, 2 in the pipeline.
#
# How this list was built (all measured, nothing assumed):
#   1. GET /api/gateway/models per platform -> 771 listed
#   2. 1-token probe on every text model    -> 75 answer
#   3. race all 75 on the SAME 8 real rows  -> 27 with zero hallucination
#   4. keep those scoring >=4/8            -> the fleet below
#
# A hallucination is disqualifying, not a deduction: one invented herb poisons
# the library's core promise (every character traceable to the source text).
# Note two findings that contradict intuition -- bigger is not better here
# (Qwen3-235B scored 1/8, gemma-4-31b scored 7/8), and every llama variant on
# nvidia hallucinated.

FLEET = [
    # 7/8 verified, 5s
    {"id": "cerebras-gemma-4-31b", "supplier": "cerebras", "model": "gemma-4-31b"},
    # 7/8 verified, 34s
    {"id": "nvidia-gemma-4-31b", "supplier": "nvidia", "model": "google/gemma-4-31b-it"},
    # 6/8 verified, 6s
    {"id": "nvidia-mistral-nemotron", "supplier": "nvidia", "model": "mistralai/mistral-nemotron"},
    # 6/8 verified, 6s
    {"id": "xf-qwen-qwen36v35b", "supplier": "xf_qwen", "model": "xopqwen36v35b"},
    # 5/8 verified, 6s
    {"id": "agnes-agnes-2.0-flash", "supplier": "agnes", "model": "agnes-2.0-flash"},
    # 4/8 verified, 8s
    {"id": "modelscope-qwen3-14b", "supplier": "modelscope", "model": "Qwen/Qwen3-14B"},
    # 4/8 verified, 11s
    {"id": "nvidia-gemma-3n-e4b", "supplier": "nvidia", "model": "google/gemma-3n-e4b-it"},
    # 4/8 verified, 51s
    {"id": "zhipu-glm-5.1", "supplier": "zhipu", "model": "glm-5.1"},
]

FLEET_IDS = [f["id"] for f in FLEET]


def resolve(fleet_id):
    """fleet id -> (supplier, model). Unknown id raises rather than silently"""
    """falling back to a platform default, which would hide a typo as a quality drop."""
    for f in FLEET:
        if f["id"] == fleet_id:
            return f["supplier"], f["model"]
    raise KeyError("unknown fleet id: %s" % fleet_id)
