# -*- coding: utf-8 -*-
"""Single source of truth reader for sueai/lanes.json.

Why this file exists (2026-08-27 audit):

  lane-probe.yml runs every day, tests every lane, and writes the verdict back
  into sueai/lanes.json. That file's own header calls itself the single source
  of truth. As of the audit its verdict was:

      rank 1  nvidia-gemma-4-31b        error
      rank 2  nvidia-mistral-nemotron   error
      rank 3  cerebras-gemma-4-31b      no_credit
      rank 4  siliconflow-qwen72b       no_key
      rank 5  zhipu-glm-4-flash         active     <- the only live one
      rank 6  agnes-2.0-flash           error
      rank 7  sensenova-flash           no_credit
      rank 90 nvidia-gemma-3n-e4b       retired

  And direct_fleet.py -- the code that actually spends quota -- carried its own
  hard-coded copy of that list, including four lanes the probe had already
  marked error/retired. The probe knew they were dead; the caller kept calling
  them anyway.

  The same shape cost us elsewhere: one gateway provider was pinned to a model
  the vendor had archived, and it absorbed 1,489 consecutive 404s over ten days
  before anyone looked at the error string.

Design notes:

  * FAIL-OPEN. If lanes.json is missing, malformed, or a lane is absent from it,
    nothing is dropped. An unknown lane is not a dead lane. Silence is never
    treated as a verdict.
  * OBSERVE FIRST. filter_dead() only reports by default; it drops lanes for
    real when enforce=True (callers wire this to an env flag). Flipping a live
    fleet from 8 lanes to 2 on the strength of a config file is exactly the kind
    of change that should be watched for a cycle before it bites.
  * NO NETWORK, NO SECRETS. This module reads one JSON file. It never resolves a
    key, never prints one, and never calls out.
"""
import json
import os

# Mirrors sueai/lanes.js LANE_STATUS exactly. That module is the JS-side access
# layer for the same file, created 2026-07-27 after the same list had drifted
# across three places (SiliconFlow returned HTTP 402 and only one copy knew).
# Keeping a second, differently-shaped vocabulary here would rebuild that bug in
# a new language.
#
# Two things copied deliberately, not paraphrased:
#
#   WHITELIST, NOT BLACKLIST. lanes.js decides usability as
#       status === ACTIVE && hasCredential(lane)
#   My first draft here listed the DEAD statuses instead. Same answer today,
#   opposite answer tomorrow: add a sixth status and a blacklist calls it usable
#   while lanes.js calls it unusable. That divergence is the exact failure both
#   modules exist to prevent.
#
#   UNKNOWN STATUS IS AN ERROR, NOT A SHRUG. lanes.js throws on a status outside
#   the enum. Python here cannot throw (callers must keep running when the source
#   of truth is unreadable), so it says so loudly and treats the lane as unknown
#   -- never silently as alive.
LANE_STATUS = ("active", "no_credit", "no_key", "retired", "error")
STATUS_LIVE = ("active",)

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sueai", "lanes.json"
)


def load(path=None):
    """Return the parsed lanes.json, or None when it cannot be read.

    Never raises: a caller that cannot read the source of truth must still run.
    """
    p = path or _DEFAULT_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:                                    # noqa: BLE001
        print("[lanes] cannot read %s (%s) -- keeping caller's own list"
              % (p, type(exc).__name__), flush=True)
        return None


def status_map(path=None):
    """{lane_id: status}. Empty dict when the source cannot be read."""
    doc = load(path)
    if not doc:
        return {}
    out = {}
    for lane in doc.get("lanes") or []:
        lid = lane.get("id")
        if lid:
            out[lid] = str(lane.get("status") or "").strip().lower()
    return out


def is_usable(status):
    """Mirror of lanes.js usableLanes(): only ACTIVE counts.

    Credential presence is the caller's business here -- direct_fleet checks its
    own env keys -- so this is the status half of that predicate only.
    """
    return str(status or "").strip().lower() in STATUS_LIVE


def is_known(status):
    """False for a status outside lanes.js's enum. Such a lane is not judged."""
    return str(status or "").strip().lower() in LANE_STATUS


def is_dead(status):
    """Explicitly reported as unusable, and the report is one we understand.

    An unrecognised status is NOT dead -- it is unknown, and unknown lanes are
    left alone (see filter_dead).
    """
    st = str(status or "").strip().lower()
    return is_known(st) and not is_usable(st)


def filter_dead(lanes, id_key="id", enforce=False, path=None, label="fleet"):
    """Report (and optionally drop) lanes the daily probe has marked dead.

    Args:
        lanes:   caller's own list of lane dicts.
        id_key:  which key on those dicts holds the lane id.
        enforce: False = report only (default). True = actually drop.
        label:   name used in the printed report.

    Returns the (possibly filtered) list. Always prints what it saw, so a
    disagreement between the probe and the caller can never pass silently --
    that silence is the whole reason this module exists.
    """
    smap = status_map(path)
    if not smap:
        print("[lanes] no verdicts available -- %s unchanged (%d lanes)"
              % (label, len(lanes)), flush=True)
        return lanes

    dead, unknown, live = [], [], []
    for lane in lanes:
        lid = lane.get(id_key)
        st = smap.get(lid)
        if st is None:
            unknown.append(lid)                       # not in lanes.json at all
        elif not is_known(st):
            # lanes.js would throw here. We cannot, so we make noise and keep it:
            # guessing "probably fine" about a status nobody defined is how the
            # two sides drift apart again.
            print("[lanes] WARNING lane %s has status %r, which is not in "
                  "lanes.js LANE_STATUS %s -- treating as unknown, keeping it. "
                  "If this status is real, add it in BOTH places."
                  % (lid, st, list(LANE_STATUS)), flush=True)
            unknown.append(lid)
        elif is_usable(st):
            live.append(lid)
        else:
            dead.append((lid, st))

    print("[lanes] %s vs probe: live=%d dead=%d unknown=%d"
          % (label, len(live), len(dead), len(unknown)), flush=True)
    for lid, st in dead:
        print("[lanes]   DEAD %-28s probe says: %s" % (lid, st), flush=True)
    if unknown:
        print("[lanes]   not in lanes.json (kept, unknown != dead): %s"
              % ", ".join(str(u) for u in unknown), flush=True)

    if not enforce:
        if dead:
            print("[lanes] observe-only: %d dead lane(s) NOT dropped. "
                  "Set LANES_ENFORCE=1 to drop them." % len(dead), flush=True)
        return lanes

    kept = [l for l in lanes if not is_dead(smap.get(l.get(id_key)))]  # unknown kept
    if not kept:
        # Dropping everything is worse than calling a dead lane: the job would
        # fail with "no lanes" instead of a real upstream error we can read.
        print("[lanes] enforce would leave 0 lanes -- refusing, keeping all",
              flush=True)
        return lanes
    print("[lanes] enforced: %d -> %d lanes" % (len(lanes), len(kept)), flush=True)
    return kept


if __name__ == "__main__":
    smap = status_map()
    if not smap:
        raise SystemExit("lanes.json unreadable")
    print("%-30s %s" % ("lane", "status"))
    for lid, st in smap.items():
        mark = "live" if is_usable(st) else ("DEAD" if is_known(st) else "?unknown")
        print("%-30s %-10s %s" % (lid, st, mark))
