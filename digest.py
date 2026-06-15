#!/usr/bin/env python3
"""digest.py — summarize cli-orchestra/ledger.jsonl into DIGEST.md + stdout.

Joins "call" rows with "verdict" rows by id and reports per-lane and overall
delegation/outcome stats. Stdlib only. Derives the generated-on timestamp from
the latest ts found in the ledger (never datetime.now).

v2: adds a per-lane x task_type cell table (lifetime AND last-30-days numbers,
"now" = max ts in the ledger), flags STOP-CELLS (n_verdicted>=3 AND
(reject%>30 OR avg edit_pct>35)) and writes them to router/stopcells.json
for route.py's hard filter. `--ledger PATH` runs against a fixture."""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(SCRIPT_DIR, "ledger.jsonl")
DIGEST = os.path.join(SCRIPT_DIR, "DIGEST.md")
STOPCELLS = os.path.join(SCRIPT_DIR, "router", "stopcells.json")

LANES = ["codex", "agy", "grok"]


def load_rows(path):
    calls = {}
    verdicts = {}
    latest_ts = None
    if not os.path.exists(path):
        return calls, verdicts, latest_ts
    with open(path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            rid = row.get("id")
            rtype = row.get("type")
            if rtype == "call" and rid is not None:
                calls[rid] = row  # last call row for an id wins
            elif rtype == "verdict" and rid is not None:
                verdicts[rid] = row  # last verdict for an id wins
    return calls, verdicts, latest_ts


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def quality_of(v):
    """Same verdict->quality mapping as router/ingest.py."""
    vv = v.get("verdict")
    if vv == "accept":
        return 1.0
    if vv == "edit":
        p = v.get("edit_pct")
        return 0.7 if p is None else 1.0 - float(p) / 100.0
    if vv == "reject":
        return 0.0
    return None


def cutoff_30d(latest_ts):
    """ISO cutoff string 30 days before the max ts in the ledger (never now())."""
    if not latest_ts:
        return None
    try:
        dt = datetime.strptime(latest_ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return (dt - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_cell():
    return {"calls": 0, "verdicted": 0, "accept": 0, "edit": 0, "reject": 0,
            "edit_pcts": [], "qualities": []}


def cell_stats(c):
    n = c["verdicted"]
    avg_edit = (sum(c["edit_pcts"]) / len(c["edit_pcts"])) if c["edit_pcts"] else 0.0
    avg_q = (sum(c["qualities"]) / len(c["qualities"])) if c["qualities"] else 0.0
    return {
        "calls": c["calls"], "n": n,
        "acc": pct(c["accept"], n), "edt": pct(c["edit"], n),
        "rej": pct(c["reject"], n), "avg_edit": avg_edit, "avg_q": avg_q,
    }


def build_cells(calls, verdicts, cutoff):
    """cells[(cli, task_type)] = {"life": acc, "recent": acc} accumulators."""
    cells = {}
    for rid, c in calls.items():
        key = (c.get("cli", "?"), c.get("task_type") or "unknown")
        if key not in cells:
            cells[key] = {"life": new_cell(), "recent": new_cell()}
        spans = [cells[key]["life"]]
        if cutoff and (c.get("ts") or "") >= cutoff:
            spans.append(cells[key]["recent"])
        v = verdicts.get(rid)
        for acc in spans:
            acc["calls"] += 1
            if v:
                vv = v.get("verdict")
                if vv in ("accept", "edit", "reject"):
                    acc["verdicted"] += 1
                    acc[vv] += 1
                    if vv == "edit" and v.get("edit_pct") is not None:
                        acc["edit_pcts"].append(float(v["edit_pct"]))
                    q = quality_of(v)
                    if q is not None:
                        acc["qualities"].append(q)
    return cells


def stop_flag(life):
    """STOP-CELL rule: n_verdicted>=3 AND (reject%>30 OR avg edit_pct>35)."""
    s = cell_stats(life)
    if s["n"] >= 3 and (s["rej"] > 30 or s["avg_edit"] > 35):
        why = []
        if s["rej"] > 30:
            why.append("reject%%=%.0f>30" % s["rej"])
        if s["avg_edit"] > 35:
            why.append("avg_edit_pct=%.0f>35" % s["avg_edit"])
        return " AND ".join(why)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default=LEDGER,
                    help="ledger path (default: %(default)s)")
    args = ap.parse_args()
    calls, verdicts, latest_ts = load_rows(args.ledger)

    if not calls:
        print("no data yet")
        with open(DIGEST, "w") as fh:
            fh.write("# EvolvRoute Digest\n\n")
            fh.write("_generated-on: n/a_\n\n")
            fh.write("no data yet\n")
        return

    # Per-lane accumulators
    lane = {
        c: {
            "calls": 0,
            "verdicted": 0,
            "accept": 0,
            "edit": 0,
            "reject": 0,
            "latency_sum": 0.0,
            "out_sum": 0,
            "fallback": 0,
        }
        for c in LANES
    }
    # ensure lanes seen but not in LANES are captured too
    overall = {
        "total": 0,
        "est_in_tokens": 0,
        "verdicted": 0,
        "fallbacks": 0,
        "timeouts": 0,
    }

    for rid, c in calls.items():
        cli = c.get("cli", "?")
        if cli not in lane:
            lane[cli] = {
                "calls": 0,
                "verdicted": 0,
                "accept": 0,
                "edit": 0,
                "reject": 0,
                "latency_sum": 0.0,
                "out_sum": 0,
                "fallback": 0,
            }
        L = lane[cli]
        L["calls"] += 1
        L["latency_sum"] += float(c.get("latency_s", 0) or 0)
        L["out_sum"] += int(c.get("out_chars", 0) or 0)
        if c.get("fallback_used") is True:
            L["fallback"] += 1
            overall["fallbacks"] += 1
        if int(c.get("rc", 0) or 0) == 124:
            overall["timeouts"] += 1
        overall["total"] += 1
        overall["est_in_tokens"] += int(c.get("est_in_tokens", 0) or 0)

        v = verdicts.get(rid)
        if v:
            L["verdicted"] += 1
            overall["verdicted"] += 1
            vv = v.get("verdict")
            if vv in ("accept", "edit", "reject"):
                L[vv] += 1

    # Build per-lane table
    header = (
        "| lane | calls | verdict-cov% | accept% | edit% | reject% | "
        "avg latency_s | avg out_chars | fallbacks | flag |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    rows = []
    stdout_lines = []
    for cli in sorted(lane.keys(), key=lambda x: (x not in LANES, x)):
        L = lane[cli]
        if L["calls"] == 0:
            continue
        vd = L["verdicted"]
        cov = pct(vd, L["calls"])
        acc = pct(L["accept"], vd)
        edt = pct(L["edit"], vd)
        rej = pct(L["reject"], vd)
        avg_lat = (L["latency_sum"] / L["calls"]) if L["calls"] else 0.0
        avg_out = (L["out_sum"] / L["calls"]) if L["calls"] else 0.0
        flag = ""
        if rej > 30 and vd >= 3:
            flag = "⚠ HIGH REJECT"
        rows.append(
            "| {cli} | {calls} | {cov:.0f}% | {acc:.0f}% | {edt:.0f}% | {rej:.0f}% | "
            "{lat:.1f} | {out:.0f} | {fb} | {flag} |".format(
                cli=cli, calls=L["calls"], cov=cov, acc=acc, edt=edt, rej=rej,
                lat=avg_lat, out=avg_out, fb=L["fallback"], flag=flag,
            )
        )
        stdout_lines.append(
            "  {cli:<6} calls={calls} cov={cov:.0f}% acc={acc:.0f}% edit={edt:.0f}% "
            "rej={rej:.0f}% avg_lat={lat:.1f}s avg_out={out:.0f} fb={fb}{flag}".format(
                cli=cli, calls=L["calls"], cov=cov, acc=acc, edt=edt, rej=rej,
                lat=avg_lat, out=avg_out, fb=L["fallback"],
                flag=("  " + flag if flag else ""),
            )
        )

    overall_cov = pct(overall["verdicted"], overall["total"])
    # Unverdicted "call" ids — the coverage gap that teaches the router nothing.
    # Mirrors `delegate.sh verdicts-pending`.
    pending = sum(1 for rid in calls if rid not in verdicts)

    # --- v2: per-lane x task_type cells (lifetime / last-30d from max ts) ---
    cutoff = cutoff_30d(latest_ts)
    cells = build_cells(calls, verdicts, cutoff)
    cell_header = (
        "| lane | task_type | calls (life/30d) | n (life/30d) | accept% | edit% "
        "| reject% | avg edit_pct | avg quality | flag |"
    )
    cell_sep = "|---|---|---|---|---|---|---|---|---|---|"
    cell_rows = []
    cell_stdout = []
    stopcells = []
    for (cli, tt) in sorted(cells.keys()):
        life = cell_stats(cells[(cli, tt)]["life"])
        rec = cell_stats(cells[(cli, tt)]["recent"])
        reason = stop_flag(cells[(cli, tt)]["life"])
        flag = "STOP-CELL (%s)" % reason if reason else ""
        if reason:
            stopcells.append(
                {"handler_owner": cli, "task_type": tt, "reason": reason})
        cell_rows.append(
            "| {cli} | {tt} | {c}/{c30} | {n}/{n30} | {acc:.0f}%/{acc30:.0f}% | "
            "{edt:.0f}%/{edt30:.0f}% | {rej:.0f}%/{rej30:.0f}% | "
            "{ae:.0f}/{ae30:.0f} | {q:.2f}/{q30:.2f} | {flag} |".format(
                cli=cli, tt=tt, c=life["calls"], c30=rec["calls"],
                n=life["n"], n30=rec["n"], acc=life["acc"], acc30=rec["acc"],
                edt=life["edt"], edt30=rec["edt"], rej=life["rej"],
                rej30=rec["rej"], ae=life["avg_edit"], ae30=rec["avg_edit"],
                q=life["avg_q"], q30=rec["avg_q"], flag=flag))
        cell_stdout.append(
            "  {cli:<6} x {tt:<8} calls={c}/{c30} n={n}/{n30} "
            "acc={acc:.0f}%/{acc30:.0f}% rej={rej:.0f}%/{rej30:.0f}% "
            "avg_edit={ae:.0f}/{ae30:.0f} avg_q={q:.2f}/{q30:.2f}{flag}".format(
                cli=cli, tt=tt, c=life["calls"], c30=rec["calls"],
                n=life["n"], n30=rec["n"], acc=life["acc"], acc30=rec["acc"],
                rej=life["rej"], rej30=rec["rej"], ae=life["avg_edit"],
                ae30=rec["avg_edit"], q=life["avg_q"], q30=rec["avg_q"],
                flag=("  " + flag if flag else "")))

    # Machine-readable stop-cells for route.py (always rewritten, may be []).
    with open(STOPCELLS, "w") as fh:
        json.dump(stopcells, fh, indent=2)
        fh.write("\n")

    # Write DIGEST.md
    with open(DIGEST, "w") as fh:
        fh.write("# EvolvRoute Digest\n\n")
        fh.write("_generated-on: {}_\n\n".format(latest_ts or "n/a"))
        fh.write("## Per-lane\n\n")
        fh.write(header + "\n")
        fh.write(sep + "\n")
        for r in rows:
            fh.write(r + "\n")
        fh.write("\n## Per-lane x task_type (lifetime/last-30d, now = max ledger ts)\n\n")
        fh.write(cell_header + "\n")
        fh.write(cell_sep + "\n")
        for r in cell_rows:
            fh.write(r + "\n")
        fh.write("\nstop-cells written to router/stopcells.json: {}\n".format(
            len(stopcells)))
        fh.write("\n## Overall\n\n")
        fh.write("- total delegations: {}\n".format(overall["total"]))
        fh.write("- est_in_tokens offloaded: {}\n".format(overall["est_in_tokens"]))
        fh.write("- verdict coverage: {:.0f}%\n".format(overall_cov))
        fh.write("- verdicts pending (unverdicted calls): {}\n".format(pending))
        fh.write("- total fallbacks: {}\n".format(overall["fallbacks"]))
        fh.write("- timeouts (rc=124): {}\n".format(overall["timeouts"]))

    # Compact stdout summary
    print("EvolvRoute digest (generated-on: {})".format(latest_ts or "n/a"))
    print("per-lane:")
    for ln in stdout_lines:
        print(ln)
    print("per-cell (lane x task_type, lifetime/30d):")
    for ln in cell_stdout:
        print(ln)
    print("stop-cells: {} (router/stopcells.json)".format(len(stopcells)))
    print("overall:")
    print(
        "  delegations={total} est_in_tokens={tok} verdict_cov={cov:.0f}% "
        "pending={pend} fallbacks={fb} timeouts={to}".format(
            total=overall["total"], tok=overall["est_in_tokens"],
            cov=overall_cov, pend=pending, fb=overall["fallbacks"],
            to=overall["timeouts"],
        )
    )
    print("wrote {}".format(DIGEST))


if __name__ == "__main__":
    main()
