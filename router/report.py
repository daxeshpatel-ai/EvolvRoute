#!/usr/bin/env python3
"""report.py — a spend/savings dashboard built straight from the ledger.

Reads ledger.jsonl `call` rows, recomputes each row's NOTIONAL cost from the
same rate table delegate.sh uses (MODEL_REGISTRY.md), and reports:

  - total notional spend and per-lane breakdown (calls, spend, fallbacks, latency)
  - REALIZED SAVINGS vs frontier: what those same delegated calls would have
    cost at the opus-class frontier rate, minus what they actually cost — the
    money delegation saved
  - verdict coverage (the learning-loop health metric) and accept/edit/reject mix

NOTIONAL only — workers bill flat subscriptions; these are metered-equivalent
reference prices, not real billing. Stdlib only. Importable: summarize(calls,
verdicts) -> dict. CLI:
  python3 router/report.py [--ledger PATH] [--json]
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(SCRIPT_DIR, "..", "ledger.jsonl")
MEDIA_FLAT = float(os.environ.get("MEDIA_FLAT_USD", "0.04"))

# Frontier (opus-class) reference rate — the price delegated work would cost if
# it had stayed on the frontier controller. Mirrors MODEL_REGISTRY.md.
FRONTIER_IN, FRONTIER_OUT = 15.0, 75.0


def model_rate(cli, model):
    """(IN, OUT) USD per 1M tokens — verbatim from delegate.sh model_rate()."""
    if cli == "codex":
        if model in ("gpt-5.5", "gpt-5.4"):
            return (1.25, 10.0)
        if model in ("gpt-5.4-mini", "mini"):
            return (0.25, 2.0)
        return (1.0, 5.0)
    if cli == "agy":
        if model in ("auto", "-", "", "flash"):
            return (0.30, 2.50)
        if model == "sonnet":
            return (3.0, 15.0)
        if model == "opus":
            return (15.0, 75.0)
        if model == "gpt-oss-120b":
            return (0.10, 0.50)
        return (1.0, 5.0)
    if cli == "grok":
        if model in ("grok-build", "-", "", "text"):
            return (3.0, 15.0)
        return (1.0, 5.0)
    return (1.0, 5.0)


def _tokens(call):
    return int(call.get("est_in_tokens") or 0), int(call.get("out_chars") or 0)


def call_cost(call):
    if call.get("modality") in ("image", "video"):
        return MEDIA_FLAT
    in_tok, out_chars = _tokens(call)
    ir, orate = model_rate(call.get("cli", ""), call.get("model", ""))
    return in_tok / 1e6 * ir + (out_chars / 4) / 1e6 * orate


def frontier_cost(call):
    """What this call would have cost on the frontier controller instead."""
    if call.get("modality") in ("image", "video"):
        return MEDIA_FLAT  # media has no frontier substitute — cost-neutral
    in_tok, out_chars = _tokens(call)
    return in_tok / 1e6 * FRONTIER_IN + (out_chars / 4) / 1e6 * FRONTIER_OUT


def load_ledger(path):
    calls, verdicts = {}, {}
    if not os.path.exists(path):
        return calls, verdicts
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("id")
            if rid is None:
                continue
            if row.get("type") == "call":
                calls[rid] = row
            elif row.get("type") == "verdict":
                verdicts[rid] = row
    return calls, verdicts


def summarize(calls, verdicts):
    spent = frontier = 0.0
    by_lane = {}
    acc = edt = rej = 0
    for rid, c in calls.items():
        cli = c.get("cli", "?")
        cc, fc = call_cost(c), frontier_cost(c)
        spent += cc
        frontier += fc
        L = by_lane.setdefault(
            cli, {"calls": 0, "spent": 0.0, "fallbacks": 0, "latency_sum": 0.0})
        L["calls"] += 1
        L["spent"] += cc
        L["latency_sum"] += float(c.get("latency_s") or 0)
        if c.get("fallback_used") is True:
            L["fallbacks"] += 1
        v = verdicts.get(rid)
        if v:
            vv = v.get("verdict")
            if vv == "accept":
                acc += 1
            elif vv == "edit":
                edt += 1
            elif vv == "reject":
                rej += 1
    n = len(calls)
    verdicted = acc + edt + rej
    for L in by_lane.values():
        L["avg_latency"] = (L["latency_sum"] / L["calls"]) if L["calls"] else 0.0
    savings = frontier - spent
    return {
        "n_calls": n,
        "spent": spent,
        "frontier_equiv": frontier,
        "savings": savings,
        "savings_pct": (savings / frontier * 100.0) if frontier else 0.0,
        "by_lane": by_lane,
        "verdict_cov": (verdicted / n * 100.0) if n else 0.0,
        "accept": acc, "edit": edt, "reject": rej,
        "pending": n - verdicted,
    }


def print_dashboard(s):
    print("EvolvRoute cost dashboard (notional)")
    if s["n_calls"] == 0:
        print("  no delegated calls in the ledger yet")
        return
    print("  delegated calls : %d" % s["n_calls"])
    print("  notional spend  : $%.4f" % s["spent"])
    print("  frontier-equiv  : $%.4f  (cost if kept on the frontier)" % s["frontier_equiv"])
    print("  realized savings: $%.4f  (%.1f%% vs frontier)"
          % (s["savings"], s["savings_pct"]))
    print("  verdict coverage: %.0f%%  (accept=%d edit=%d reject=%d pending=%d)"
          % (s["verdict_cov"], s["accept"], s["edit"], s["reject"], s["pending"]))
    print("  per-lane:")
    for cli in sorted(s["by_lane"], key=lambda k: -s["by_lane"][k]["spent"]):
        L = s["by_lane"][cli]
        print("    %-6s calls=%-3d spend=$%-8.4f fallbacks=%-2d avg_lat=%.1fs"
              % (cli, L["calls"], L["spent"], L["fallbacks"], L["avg_latency"]))


def main():
    ap = argparse.ArgumentParser(description="ledger spend/savings dashboard")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    calls, verdicts = load_ledger(args.ledger)
    s = summarize(calls, verdicts)
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print_dashboard(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
