#!/usr/bin/env python3
"""lane_contract.py — the versioned contract a lane's capability card must meet.

A "lane" is just a capability card in handlers.json plus the small touch-points
in docs/ADDING_A_LANE.md. This module makes the card half of that contract
explicit and machine-checkable, so a malformed lane fails fast at sync/doctor
time instead of mis-routing silently later.

Two severities:
  - ERRORS   — structural problems that would break routing or ingest (missing
               id/owner, wrong types, duplicate ids). `validate()` callers
               should refuse to proceed.
  - WARNINGS — values the engine tolerates but that signal a likely mistake
               (an unrecognized cost_band/latency_band/risk_level falls back to
               a neutral default; an unknown modality token; a missing routing
               prior). Surfaced, not fatal.

Stdlib only. Importable: validate(cards) -> (errors, warnings). CLI:
  python3 router/lane_contract.py [--handlers PATH]   # exit 1 if any ERROR
"""

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HANDLERS_JSON = os.path.join(SCRIPT_DIR, "handlers.json")

# Engine-recognized vocabularies. Keep in sync with route.py COST_PREF /
# LATENCY_PREF and the modality gate; unknown values degrade gracefully (hence
# warnings, not errors).
COST_BANDS = {"free_quota", "subscription", "claude_tokens"}
LATENCY_BANDS = {"low", "medium", "high"}
RISK_LEVELS = {"low", "medium", "high"}

# (field, python type, human label) — required and structurally enforced.
REQUIRED = [
    ("id", str, "string"),
    ("owner", str, "string"),
    ("handler_type", str, "string"),
    ("input_types", list, "list"),
    ("output_types", list, "list"),
    ("enabled", bool, "bool"),
]
# Recommended fields — warn if absent (they shape the routing prior).
RECOMMENDED = ["strong_at", "weak_at", "routing_tags",
               "cost_band", "latency_band", "risk_level"]


def validate_card(card, index):
    """Return (errors, warnings) for a single card. `index` is for messages
    when the card has no usable id."""
    errors, warnings = [], []
    if not isinstance(card, dict):
        return ["handler #%d is not a JSON object" % index], []
    cid = card.get("id") if isinstance(card.get("id"), str) else "#%d" % index

    for field, typ, label in REQUIRED:
        if field not in card:
            errors.append("%s: missing required field '%s'" % (cid, field))
            continue
        val = card[field]
        # bool is a subclass of int; check it precisely.
        if typ is bool:
            if not isinstance(val, bool):
                errors.append("%s: '%s' must be %s" % (cid, field, label))
        elif not isinstance(val, typ):
            errors.append("%s: '%s' must be %s" % (cid, field, label))
        elif typ is str and not val.strip():
            errors.append("%s: '%s' must be non-empty" % (cid, field))
        elif typ is list and not val:
            errors.append("%s: '%s' must be non-empty" % (cid, field))

    for field in RECOMMENDED:
        if field not in card:
            warnings.append("%s: missing recommended field '%s'" % (cid, field))

    if card.get("cost_band") not in COST_BANDS and "cost_band" in card:
        warnings.append("%s: cost_band '%s' not recognized %s (defaults to 0.5)"
                        % (cid, card["cost_band"], sorted(COST_BANDS)))
    if card.get("latency_band") not in LATENCY_BANDS and "latency_band" in card:
        warnings.append("%s: latency_band '%s' not in %s (defaults to medium)"
                        % (cid, card["latency_band"], sorted(LATENCY_BANDS)))
    if card.get("risk_level") not in RISK_LEVELS and "risk_level" in card:
        warnings.append("%s: risk_level '%s' not in %s"
                        % (cid, card["risk_level"], sorted(RISK_LEVELS)))
    # input_types/output_types are free-form descriptors (the modality gate only
    # keys on the image/video tokens), so the only structural rule is that the
    # entries are strings.
    for field in ("input_types", "output_types"):
        for t in card.get(field, []):
            if not isinstance(t, str):
                errors.append("%s: %s entries must be strings" % (cid, field))
                break
    return errors, warnings


def validate(cards):
    """Validate a list of cards. Returns (errors, warnings)."""
    errors, warnings = [], []
    if not isinstance(cards, list):
        return ["handlers must be a JSON list"], []
    seen = {}
    for i, card in enumerate(cards):
        e, w = validate_card(card, i)
        errors.extend(e)
        warnings.extend(w)
        cid = card.get("id") if isinstance(card, dict) else None
        if isinstance(cid, str):
            if cid in seen:
                errors.append("duplicate handler id '%s'" % cid)
            seen[cid] = True
    return errors, warnings


def load_and_validate(path=HANDLERS_JSON):
    with open(path) as fh:
        doc = json.load(fh)
    cards = doc["handlers"] if isinstance(doc, dict) else doc
    return validate(cards)


def main():
    ap = argparse.ArgumentParser(description="validate handlers.json lane cards")
    ap.add_argument("--handlers", default=HANDLERS_JSON)
    args = ap.parse_args()
    try:
        errors, warnings = load_and_validate(args.handlers)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print("lane_contract: cannot read %s: %s" % (args.handlers, exc),
              file=sys.stderr)
        return 1
    for w in warnings:
        print("WARN  %s" % w)
    for e in errors:
        print("ERROR %s" % e, file=sys.stderr)
    if errors:
        print("lane_contract: %d error(s), %d warning(s) — INVALID"
              % (len(errors), len(warnings)), file=sys.stderr)
        return 1
    print("lane_contract: OK (%d warning(s))" % len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
