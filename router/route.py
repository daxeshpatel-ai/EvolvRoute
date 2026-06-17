#!/usr/bin/env python3
"""route.py — WP2 geometric router for EvolvRoute.

Usage:
  python3 router/route.py "<task text>" [--task-type T] [--size-class S]
                          [--modality M] [--risk low|medium|high] [--json]

Flow: embed(task text + profile suffix) -> hard-filter handlers -> score
survivors -> (optionally) recommend the agy->codex relay -> print table or
JSON -> log one row to the invocations table.

HARD FILTERS (claude_keep is the floor — it is NEVER excluded):
  - modality: image/video tasks keep only handlers whose output_types include
    that modality (grok_media); text tasks exclude grok_media.
  - escalation: risk==high OR task_type in (architecture, review, planning)
    -> only claude_keep survives.
  - quota: ledger.jsonl call rows per owner within window (codex 5h cap
    CODEX_CAP=40; agy 24h AGY_CAP=800; grok 24h GROK_CAP=150 — same
    env-overridable caps as delegate.sh). Owner at/over cap is excluded.
  - stop-cells: router/stopcells.json (written by digest.py v2) lists
    (handler_owner, task_type) cells with bad verdict history; the owner's
    text handlers are excluded for that task_type.

SCORING (confidence-weighted; all terms in [0, 1]):
  score = 0.45 * cosine(query, handler_centroid)
        + 0.25 * max_i( cosine(query, outcome_vec_i) * quality_score_i )
                 # proven-outcome term; 0 if the handler has no outcomes
        + 0.20 * smoothed_success
        + 0.05 * cost_pref      (free_quota=1.0, subscription=0.6, claude_tokens=0.1)
        + 0.05 * latency_pref   (low=1.0, medium=0.7, high=0.4)

  smoothed_success = (sum_i w_i*success_i + 2*0.7) / (sum_i w_i + 2)
    success_i = 1 if quality_score_i >= 0.7 else 0
    w_i = 1.0 for outcomes <= 30 days old, 0.5 for older ones (the only
          recency effect — no separate recency term yet)
    Bayesian prior 0.7 with pseudo-count 2; with n=0 this is 0.7 (neutral).

RELAY: task_type in (code, refactor, test) AND size_class == large ->
mode="relay" with stage1=agy_relay (context compression) and stage2 = the
top-scoring surviving codex handler. codex_mini is not a stage2 candidate:
relay only fires for size_class=large and codex_mini's card caps it at a
small module, so stage2 is the flagship codex lane.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import ingest  # noqa: E402  (embed, cosine, DB_PATH, now_iso)

LEDGER = os.path.join(SCRIPT_DIR, "..", "ledger.jsonl")
STOPCELLS = os.path.join(SCRIPT_DIR, "stopcells.json")
POLICIES = os.path.join(SCRIPT_DIR, "policies.json")

ESCALATE_TASK_TYPES = ("architecture", "review", "planning")
RELAY_TASK_TYPES = ("code", "refactor", "test")

# Stateful-policy defaults — used verbatim when policies.json is missing/unreadable.
POLICY_DEFAULTS = {
    "recent_window_secs": 900,
    "escalate_after_failure": True,
    "escalate_task_types": ["code", "refactor", "test"],
    "lane_cooldown_errors": 2,
}

COST_PREF = {"free_quota": 1.0, "subscription": 0.6, "claude_tokens": 0.1}
LATENCY_PREF = {"low": 1.0, "medium": 0.7, "high": 0.4}

# Quota windows/caps — mirrors delegate.sh quota_window_secs/quota_cap.
QUOTA = {
    "codex": (5 * 3600, int(os.environ.get("CODEX_CAP", "40"))),
    "agy": (24 * 3600, int(os.environ.get("AGY_CAP", "800"))),
    "grok": (24 * 3600, int(os.environ.get("GROK_CAP", "150"))),
}

# stop-cell owner -> text handler ids (grok_media / claude_keep never stopped).
OWNER_STOP_HANDLERS = {
    "codex": ["codex_mini", "codex_55"],
    "agy": ["agy_flash"],
    "grok": ["grok_text"],
}


def parse_ts(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def count_recent_calls(owner, now):
    """Count ledger 'call' rows for this owner (cli) within its quota window."""
    window, _cap = QUOTA[owner]
    n = 0
    if not os.path.exists(LEDGER):
        return 0
    with open(LEDGER) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "call" or row.get("cli") != owner:
                continue
            ts = parse_ts(row.get("ts"))
            if ts and (now - ts).total_seconds() <= window:
                n += 1
    return n


def load_stopcells():
    """Return set of (handler_id, task_type) pairs from router/stopcells.json."""
    pairs = set()
    if not os.path.exists(STOPCELLS):
        return pairs
    try:
        with open(STOPCELLS) as fh:
            cells = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return pairs
    for cell in cells:
        tt = cell.get("task_type")
        if "handler_id" in cell:
            pairs.add((cell["handler_id"], tt))
            continue
        for hid in OWNER_STOP_HANDLERS.get(cell.get("handler_owner", ""), []):
            pairs.add((hid, tt))
    return pairs


def load_policies():
    """Tunable params from router/policies.json; POLICY_DEFAULTS if missing/bad."""
    pol = dict(POLICY_DEFAULTS)
    if not os.path.exists(POLICIES):
        return pol
    try:
        with open(POLICIES) as fh:
            pol.update(json.load(fh))
    except (json.JSONDecodeError, OSError):
        pass
    return pol


def ledger_now():
    """'now' = max ts in the ledger (deterministic-time convention, like digest.py;
    falls back to wall-clock UTC only when the ledger has no parseable ts)."""
    latest = None
    if os.path.exists(LEDGER):
        with open(LEDGER) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = parse_ts(json.loads(line).get("ts"))
                except json.JSONDecodeError:
                    continue
                if ts and (latest is None or ts > latest):
                    latest = ts
    return latest or datetime.now(timezone.utc)


def recent_call_rows(now, window):
    """Ledger 'call' rows whose ts is within `window` secs of `now`."""
    rows = []
    if not os.path.exists(LEDGER):
        return rows
    with open(LEDGER) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "call":
                continue
            ts = parse_ts(row.get("ts"))
            if ts and (now - ts).total_seconds() <= window:
                rows.append(row)
    return rows


def stateful_policy_filter(handlers, survivors, task_type, policies, now,
                           task_text="", modality="text", size_class="unknown"):
    """Contextual rules over the recent ledger tail. Returns
    (new_survivors, applied) where applied is a list of
    {"rule": name, "reason": str}. claude_keep is NEVER excluded."""
    applied = []

    # Rule C — break-even floor (deterministic, ledger-independent): a tiny TEXT
    # task isn't worth a worker round-trip (codex burns ~15K fixed tokens/call),
    # so floor it to claude_keep. Estimate tokens as len//4; MEDIA is EXEMPT
    # (image/video have a real modality edge regardless of prompt size). An
    # explicit size_class of medium/large is ALSO exempt: the caller has already
    # declared the task non-trivial, so a terse-but-substantive spec (e.g. a
    # boilerplate task with a short prompt but a large expected artifact) is not
    # mistaken for throwaway work. Floor value from policies.min_delegate_tokens,
    # overridable via MIN_DELEGATE_TOKENS.
    floor = int(os.environ.get(
        "MIN_DELEGATE_TOKENS", policies.get("min_delegate_tokens", 1000)))
    exempt_size = size_class in ("medium", "large")
    if modality not in ("image", "video") and not exempt_size and floor > 0:
        est_tokens = len(task_text) // 4
        if est_tokens < floor:
            applied.append({
                "rule": "break_even",
                "reason": ("below_break_even: ~%d tokens < min_delegate_tokens=%d "
                           "-> claude_keep only" % (est_tokens, floor)),
            })
            return [hid for hid in survivors if hid == "claude_keep"], applied

    window = policies.get("recent_window_secs", 900)
    recent = recent_call_rows(now, window)
    if not recent:
        return survivors, applied

    def is_error(row):
        rc = row.get("rc")
        return rc not in (0, None) or (rc == 0 and row.get("out_chars") == 0)

    # Rule A — escalate-after-failure: any recent fallback/error on a risky
    # task_type collapses the field to claude_keep only.
    if (policies.get("escalate_after_failure")
            and task_type in policies.get("escalate_task_types", [])
            and any(r.get("fallback_used") in (True, "true") or is_error(r) for r in recent)):
        applied.append({
            "rule": "escalate_after_failure",
            "reason": ("recent failure/fallback on %s within %ds -> claude_keep only"
                       % (task_type, window)),
        })
        return [hid for hid in survivors if hid == "claude_keep"], applied

    # Rule B — lane cooldown: an owner with >= lane_cooldown_errors recent error
    # rows has its text handlers excluded (claude_keep is never excluded).
    threshold = policies.get("lane_cooldown_errors", 2)
    err_by_owner = {}
    for r in recent:
        if is_error(r):
            err_by_owner[r.get("cli")] = err_by_owner.get(r.get("cli"), 0) + 1
    cooled = set()
    for owner, n in err_by_owner.items():
        if n >= threshold:
            for hid in OWNER_STOP_HANDLERS.get(owner, []):
                cooled.add(hid)
    if cooled:
        kept = [hid for hid in survivors if hid == "claude_keep" or hid not in cooled]
        if len(kept) != len(survivors):
            applied.append({
                "rule": "lane_cooldown",
                "reason": ("excluded %s (>= %d recent errors within %ds)"
                           % (", ".join(sorted(cooled)), threshold, window)),
            })
        return kept, applied

    return survivors, applied


def load_handlers(con):
    handlers = {}
    for hid, owner, meta_json in con.execute(
        "SELECT id, owner, metadata_json FROM handlers WHERE enabled=1"
    ):
        meta = json.loads(meta_json)
        centroid = ingest.get_vector(con, "handler_centroid", hid)
        if centroid is None:  # fall back to declared embedding
            centroid = ingest.get_vector(con, "handler", hid)
        handlers[hid] = {"id": hid, "owner": owner, "meta": meta, "centroid": centroid}
    return handlers


def load_outcomes(con):
    by_handler = {}
    for oid, hid, q, created_at in con.execute(
        "SELECT id, handler_id, quality_score, created_at FROM outcomes"
    ):
        vec = ingest.get_vector(con, "outcome", oid)
        by_handler.setdefault(hid, []).append(
            {"id": oid, "quality": q, "created_at": created_at, "vec": vec}
        )
    return by_handler


def hard_filters(handlers, task_type, modality, risk, now):
    """Return (survivor_ids, exclusions {hid: reason}). claude_keep always survives."""
    excl = {}
    quota_counts = {}
    stop_pairs = load_stopcells()
    escalate = risk == "high" or task_type in ESCALATE_TASK_TYPES
    for hid, h in handlers.items():
        if hid == "claude_keep":
            continue  # the floor — never excluded
        out_types = h["meta"].get("output_types", [])
        if modality in ("image", "video"):
            if modality not in out_types:
                excl[hid] = "modality (%s task; output_types lack it)" % modality
                continue
        elif hid == "grok_media":
            excl[hid] = "modality (text task; grok_media is media-only)"
            continue
        if escalate:
            excl[hid] = (
                "escalation (risk=high)" if risk == "high"
                else "escalation (task_type=%s -> claude_keep only)" % task_type
            )
            continue
        owner = h["owner"]
        if owner in QUOTA:
            if owner not in quota_counts:
                quota_counts[owner] = count_recent_calls(owner, now)
            window, cap = QUOTA[owner]
            if quota_counts[owner] >= cap:
                excl[hid] = "quota (%s %d/%d in %dh)" % (
                    owner, quota_counts[owner], cap, window // 3600)
                continue
        if (hid, task_type) in stop_pairs:
            excl[hid] = "stop-cell (%s x %s)" % (hid, task_type)
            continue
    survivors = [hid for hid in handlers if hid not in excl]
    return survivors, excl


def smoothed_success(outcomes, now):
    wsum, ssum = 0.0, 0.0
    for o in outcomes:
        q = o["quality"]
        if q is None:
            continue
        ts = parse_ts(o["created_at"])
        w = 1.0 if (ts and now - ts <= timedelta(days=30)) else 0.5
        wsum += w
        ssum += w * (1.0 if q >= 0.7 else 0.0)
    return (ssum + 2 * 0.7) / (wsum + 2)


def score_handler(h, outcomes, qvec, now):
    sim = ingest.cosine(qvec, h["centroid"]) if h["centroid"] else 0.0
    outcome_term = 0.0
    for o in outcomes:
        if o["vec"] is None or o["quality"] is None:
            continue
        outcome_term = max(
            outcome_term, ingest.cosine(qvec, o["vec"]) * max(0.0, o["quality"]))
    succ = smoothed_success(outcomes, now)
    cost = COST_PREF.get(h["meta"].get("cost_band"), 0.5)
    lat = LATENCY_PREF.get(h["meta"].get("latency_band"), 0.7)
    score = 0.45 * sim + 0.25 * outcome_term + 0.20 * succ + 0.05 * cost + 0.05 * lat
    return score, {"sim": sim, "outcome": outcome_term, "success": succ,
                   "cost": cost, "latency": lat}


def log_invocation(con, task_text, chosen, score):
    inv_id = "%d-%d" % (int(time.time()), os.getpid())
    con.execute(
        "INSERT OR REPLACE INTO invocations(id, task_text, chosen_handler_id,"
        " router_score, status, created_at) VALUES(?,?,?,?,?,?)",
        (inv_id, task_text[:500], chosen, score, "routed", ingest.now_iso()),
    )
    con.commit()
    return inv_id


def decide(con, task, task_type="unknown", size_class="unknown",
           modality="text", risk="low"):
    """Pure routing decision over an open DB connection. Returns a dict with
    mode / chosen / score / runner_up / relay / reasons / policy plus the raw
    `scored` and `exclusions` for callers that want detail. No side effects
    (it does NOT log the invocation — that's main()'s job), so it is safe to
    call from tests and the benchmark without mutating the DB."""
    routing_text = "%s task_type=%s size_class=%s modality=%s" % (
        task, task_type, size_class, modality)
    qvec = ingest.embed(routing_text)
    now = datetime.now(timezone.utc)

    handlers = load_handlers(con)
    outcomes = load_outcomes(con)
    survivors, exclusions = hard_filters(handlers, task_type, modality, risk, now)
    reasons = []

    # Stateful policy hook: contextual rules over the recent ledger tail.
    # Uses ledger-derived 'now' (deterministic-time convention, like digest.py),
    # applied after the hard filters and before scoring. claude_keep never dies.
    policies = load_policies()
    policy_now = ledger_now()
    survivors, policy_applied = stateful_policy_filter(
        handlers, survivors, task_type, policies, policy_now,
        task_text=task, modality=modality, size_class=size_class)
    for p in policy_applied:
        reasons.append("policy %s: %s" % (p["rule"], p["reason"]))

    scored = []
    for hid in survivors:
        s, parts = score_handler(handlers[hid], outcomes.get(hid, []), qvec, now)
        scored.append((s, hid, parts))
        reasons.append(
            "%s: score=%.4f (sim=%.3f outcome=%.3f success=%.3f cost=%.1f lat=%.1f)"
            % (hid, s, parts["sim"], parts["outcome"], parts["success"],
               parts["cost"], parts["latency"]))
    for hid, why in exclusions.items():
        reasons.append("%s: excluded — %s" % (hid, why))
    scored.sort(key=lambda t: t[0], reverse=True)

    mode, relay = "single", None
    chosen_score, chosen, _ = scored[0]
    if task_type in RELAY_TASK_TYPES and size_class == "large":
        codex_cands = [t for t in scored
                       if handlers[t[1]]["owner"] == "codex" and t[1] != "codex_mini"]
        if codex_cands:
            mode = "relay"
            relay = {"stage1": "agy_relay", "stage2": codex_cands[0][1]}
            chosen, chosen_score = codex_cands[0][1], codex_cands[0][0]
            reasons.append(
                "relay: %s large task -> stage1=agy_relay (compress), stage2=%s"
                % (task_type, relay["stage2"]))

    runner_up = next((t[1] for t in scored if t[1] != chosen), None)
    return {
        "routing_text": routing_text,
        "mode": mode, "chosen": chosen, "score": chosen_score,
        "runner_up": runner_up, "relay": relay, "reasons": reasons,
        "scored": scored, "exclusions": exclusions,
        "policy": {
            "applied": [pa["rule"] for pa in policy_applied],
            "rules": policy_applied,
        },
    }


def main():
    ap = argparse.ArgumentParser(description="geometric router")
    ap.add_argument("task")
    ap.add_argument("--task-type", default="unknown")
    ap.add_argument("--size-class", default="unknown")
    ap.add_argument("--modality", default="text")
    ap.add_argument("--risk", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(ingest.DB_PATH)
    try:
        d = decide(con, args.task, args.task_type, args.size_class,
                   args.modality, args.risk)
        log_invocation(con, d["routing_text"], d["chosen"], d["score"])
    finally:
        con.close()

    mode, chosen, chosen_score = d["mode"], d["chosen"], d["score"]
    relay, runner_up = d["relay"], d["runner_up"]
    scored, exclusions = d["scored"], d["exclusions"]
    policy_applied = d["policy"]["rules"]

    if args.json:
        print(json.dumps({
            "mode": mode, "chosen": chosen, "score": round(chosen_score, 4),
            "reasons": d["reasons"], "runner_up": runner_up, "relay": relay,
            "policy": d["policy"],
        }, indent=2))
        return

    print("route: %s" % d["routing_text"])
    print("mode=%s chosen=%s score=%.4f runner_up=%s" % (mode, chosen, chosen_score, runner_up))
    if relay:
        print("relay: stage1=%s stage2=%s" % (relay["stage1"], relay["stage2"]))
    for pa in policy_applied:
        print("policy %s: %s" % (pa["rule"], pa["reason"]))
    print("%-12s %-8s %s" % ("handler", "score", "detail"))
    for s, hid, parts in scored:
        print("%-12s %-8.4f sim=%.3f outcome=%.3f success=%.3f cost=%.1f lat=%.1f%s"
              % (hid, s, parts["sim"], parts["outcome"], parts["success"],
                 parts["cost"], parts["latency"],
                 "   <- chosen" if hid == chosen else ""))
    for hid, why in exclusions.items():
        print("%-12s %-8s excluded — %s" % (hid, "-", why))


if __name__ == "__main__":
    main()
