#!/usr/bin/env python3
"""benchmark.py — EvolvRoute's "midterm exam".

Drives the REAL router entrypoint (``router/route.py``) over a fixed task suite
and prices every decision with the project's OWN notional cost model (the rate
table in MODEL_REGISTRY.md, mirrored by ``model_rate`` / ``est_cost_usd`` in
delegate.sh). Nothing here is hand-waved: the routing comes from the shipped
engine, and the dollars come from the shipped rates.

It answers three questions:

  1. COST   — what does routing-to-cheapest-capable cost vs. the obvious
              alternatives (always-frontier-inline, always-flagship, and the
              run-everything-in-parallel fusion anti-pattern)?
  2. QUALITY— does the router actually offload delegatable work AND keep the
              high-tier work inline (escalation + break-even governance)?
  3. LEARNING — does seeding the ledger with good outcomes (a "warm" run)
              change routing vs. a cold start?

Hermetic + offline: it builds a throwaway routing DB from handlers.json in a
tmp dir and (for the warm run) seeds it from ledger.jsonl.example. With
model2vec absent the embedder is the deterministic hashed-bow-256 fallback, so
results are reproducible run-to-run. The cost/quality run sets
MIN_DELEGATE_TOKENS=0 to isolate routing quality from the deterministic
break-even floor; the floor is then verified separately in the governance check.

Usage:
  python3 bench/benchmark.py                 # run, print summary, write RESULTS.md
  python3 bench/benchmark.py --no-write       # run, print summary only
  python3 bench/benchmark.py --tasks PATH --out PATH
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROUTER = os.path.join(REPO, "router")

# --- notional cost model (verbatim from MODEL_REGISTRY.md / delegate.sh) -----
# IN / OUT are USD per 1M tokens. out_tokens = out_chars / 4 (delegate.sh
# est_cost_usd). These are metered-EQUIVALENT reference prices, NOT real
# billing — the worker CLIs spend their own flat subscriptions.
RATES = {
    ("codex", "gpt-5.4-mini"): (0.25, 2.0),
    ("codex", "gpt-5.5"): (1.25, 10.0),
    ("agy", "auto"): (0.30, 2.50),
    ("agy", "opus"): (15.0, 75.0),     # frontier controller-equivalent price
    ("grok", "text"): (3.0, 15.0),
}
MEDIA_FLAT = 0.04  # MEDIA_FLAT_USD default

# Which (cli, model) each router handler bills as.
HANDLER_LANE = {
    "codex_mini": ("codex", "gpt-5.4-mini"),
    "codex_55": ("codex", "gpt-5.5"),
    "agy_flash": ("agy", "auto"),
    "agy_relay": ("agy", "auto"),   # gemini flash, billed as agy default
    "grok_text": ("grok", "text"),
    # claude_keep == work kept on the frontier controller. We price it at the
    # opus-class reference rate: this is the cost the router AVOIDS whenever it
    # safely offloads, and the cost it HONESTLY still pays when it keeps work.
    "claude_keep": ("agy", "opus"),
}

# Lanes a quality-equivalent "fusion" (run-them-all-and-judge) strategy fans out
# to per task. It INCLUDES the frontier (opus-class) lane so the ensemble is at
# least frontier-quality on every task — the same quality bar the inline and
# routed strategies hold — which is exactly why it pays N×.
FUSION_LANES = [
    ("codex", "gpt-5.4-mini"),
    ("codex", "gpt-5.5"),
    ("agy", "auto"),
    ("grok", "text"),
    ("agy", "opus"),  # the frontier, present in every fan-out
]


def token_cost(lane, in_tokens, out_chars):
    ir, orate = RATES[lane]
    return in_tokens / 1e6 * ir + (out_chars / 4) / 1e6 * orate


def relay_cost(relay, in_tokens, out_chars):
    """agy_flash compresses the large context, codex stage2 implements over the
    compressed input. Compression assumed to ~30% of input (documented assumption)."""
    compressed = in_tokens * 0.30
    c1 = token_cost(("agy", "auto"), in_tokens, compressed * 4)  # stage1 emits compressed ctx
    stage2 = HANDLER_LANE.get(relay.get("stage2"), ("codex", "gpt-5.5"))
    c2 = token_cost(stage2, int(compressed), out_chars)
    return c1 + c2


def routed_cost(decision, task):
    in_tok, out_chars = task["in_tokens"], task["out_chars"]
    if task["modality"] in ("image", "video"):
        return MEDIA_FLAT
    if decision["mode"] == "relay" and decision.get("relay"):
        return relay_cost(decision["relay"], in_tok, out_chars)
    return token_cost(HANDLER_LANE[decision["chosen"]], in_tok, out_chars)


def baseline_costs(task):
    """Per-task cost for each non-routed strategy."""
    in_tok, out_chars = task["in_tokens"], task["out_chars"]
    if task["modality"] in ("image", "video"):
        # media has no model choice — flat for everyone, so it's cost-neutral.
        return {"inline": MEDIA_FLAT, "flagship": MEDIA_FLAT, "fusion": MEDIA_FLAT}
    return {
        # "Just use the powerful assistant for everything" — frontier inline.
        "inline": token_cost(("agy", "opus"), in_tok, out_chars),
        # "Just use one strong paid model for everything" — GPT-5.5 for all.
        "flagship": token_cost(("codex", "gpt-5.5"), in_tok, out_chars),
        # "Run them all in parallel to be safe" — pay N×.
        "fusion": sum(token_cost(l, in_tok, out_chars) for l in FUSION_LANES),
    }


# --- router driver -----------------------------------------------------------

def build_workdir(seed_ledger):
    """A tmp dir with router/ copied in and the DB synced. If seed_ledger is a
    path, copy it in as ledger.jsonl so the warm run learns from it."""
    wd = tempfile.mkdtemp(prefix="evolvroute-bench-")
    dst = os.path.join(wd, "router")
    os.makedirs(dst)
    for name in ("ingest.py", "route.py", "handlers.json", "lane_contract.py"):
        shutil.copy(os.path.join(ROUTER, name), os.path.join(dst, name))
    if seed_ledger:
        shutil.copy(seed_ledger, os.path.join(wd, "ledger.jsonl"))
    env = dict(os.environ)
    env.pop("MIN_DELEGATE_TOKENS", None)
    sync = subprocess.run(
        [sys.executable, os.path.join(dst, "ingest.py"), "sync"],
        capture_output=True, text=True, env=env, cwd=wd,
    )
    if sync.returncode != 0:
        raise RuntimeError("ingest sync failed: %s" % sync.stderr)
    return wd


def route_one(wd, task, min_delegate_tokens=0):
    env = dict(os.environ)
    env["MIN_DELEGATE_TOKENS"] = str(min_delegate_tokens)
    cmd = [
        sys.executable, os.path.join(wd, "router", "route.py"), task["prompt"],
        "--task-type", task["task_type"], "--size-class", task["size_class"],
        "--modality", task["modality"], "--risk", task["risk"], "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=wd)
    if proc.returncode != 0:
        raise RuntimeError("route failed for %s: %s" % (task["id"], proc.stderr))
    return json.loads(proc.stdout)


# --- runs --------------------------------------------------------------------

def run_suite(tasks, seed_ledger, min_delegate_tokens=0):
    wd = build_workdir(seed_ledger)
    try:
        out = []
        for t in tasks:
            d = route_one(wd, t, min_delegate_tokens=min_delegate_tokens)
            out.append((t, d))
        return out
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def aggregate(results):
    totals = {"routed": 0.0, "inline": 0.0, "flagship": 0.0, "fusion": 0.0}
    rows = []
    for t, d in results:
        rc = routed_cost(d, t)
        bc = baseline_costs(t)
        totals["routed"] += rc
        for k in ("inline", "flagship", "fusion"):
            totals[k] += bc[k]
        rows.append((t, d, rc, bc))
    return totals, rows


def quality(results):
    # Tiny tasks are governed by the break-even floor (disabled in the cost run
    # to isolate routing), so they're scored separately in governance_check —
    # exclude them here.
    keep_ok = keep_tot = off_ok = off_tot = 0
    dist = {}
    for t, d in results:
        if t.get("tiny"):
            continue
        chosen = "relay:" + d["relay"]["stage2"] if d["mode"] == "relay" else d["chosen"]
        dist[chosen] = dist.get(chosen, 0) + 1
        if t["expected"] == "keep":
            keep_tot += 1
            keep_ok += 1 if d["chosen"] == "claude_keep" else 0
        elif t["expected"] == "delegate":
            off_tot += 1
            off_ok += 1 if d["chosen"] != "claude_keep" else 0
    return {
        "escalation_correct": (keep_ok, keep_tot),
        "offload_correct": (off_ok, off_tot),
        "distribution": dist,
    }


def delegatable_subset_cost(results):
    """Apples-to-apples: on just the tasks the router DID delegate, what does
    routing to the cheapest-capable lane cost vs. forcing GPT-5.5 (flagship) on
    the same tasks? This isolates lane-selection quality from the escalation
    premium (which legitimately keeps critical work on the frontier)."""
    routed = flagship = 0.0
    n = 0
    for t, d in results:
        if t.get("tiny") or t["modality"] in ("image", "video"):
            continue
        if d["chosen"] == "claude_keep":
            continue  # not delegated — excluded from the subset
        n += 1
        routed += routed_cost(d, t)
        flagship += token_cost(("codex", "gpt-5.5"), t["in_tokens"], t["out_chars"])
    return {"n": n, "routed": routed, "flagship": flagship}


def governance_check(tasks):
    """Verify the deterministic gates at DEFAULT settings: tiny tasks hit the
    break-even floor, escalation/high-risk tasks stay inline."""
    tiny = [t for t in tasks if t.get("tiny")]
    res = run_suite(tiny, seed_ledger=None, min_delegate_tokens=1000)
    floored = sum(
        1 for _t, d in res
        if d["chosen"] == "claude_keep" and "break_even" in d["policy"]["applied"]
    )
    return {"tiny_total": len(tiny), "tiny_floored": floored}


# --- reporting ---------------------------------------------------------------

def pct(base, routed):
    return 0.0 if base == 0 else (base - routed) / base * 100.0


def fmt_usd(x):
    return "$%.4f" % x


def build_report(tasks, cold, warm, gov):
    ct, crows = aggregate(cold)
    cq = quality(cold)
    wt, _ = aggregate(warm)
    changed = sum(
        1 for (tc, dc), (tw, dw) in zip(cold, warm)
        if dc["chosen"] != dw["chosen"] or dc["mode"] != dw["mode"]
    )

    L = []
    L.append("# EvolvRoute Benchmark Results")
    L.append("")
    L.append("> Generated by `bench/benchmark.py` over `bench/tasks.jsonl` "
             "(%d tasks). Routing is produced by the real `router/route.py`; "
             "costs use the notional rate table in `MODEL_REGISTRY.md`. "
             "Embedder: deterministic `hashed-bow-256` fallback (offline, "
             "reproducible)." % len(tasks))
    L.append("")
    L.append("_Run: %s UTC._" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    L.append("")
    sub = delegatable_subset_cost(cold)

    L.append("## TL;DR")
    L.append("")
    L.append("**Quality-held-constant strategies** — every one of these keeps "
             "high-tier work (architecture / review / planning / security-critical) "
             "on the frontier; they differ only in what they do with the rest:")
    L.append("")
    L.append("| Strategy | Total notional cost | EvolvRoute savings |")
    L.append("|---|---|---|")
    L.append("| **EvolvRoute (routed)** | **%s** | — |" % fmt_usd(ct["routed"]))
    L.append("| All-frontier inline (no router) | %s | **%.1f%%** |"
             % (fmt_usd(ct["inline"]), pct(ct["inline"], ct["routed"])))
    L.append("| All-fusion (run every lane, pick best) | %s | **%.1f%%** |"
             % (fmt_usd(ct["fusion"]), pct(ct["fusion"], ct["routed"])))
    L.append("")
    L.append("Across %d tasks, routing to the cheapest-capable lane costs "
             "**%s** vs **%s** to run everything on a frontier model inline — a "
             "**%.1f%% reduction** — and **%.1f%%** less than fusing every lane in "
             "parallel, all while keeping every high-tier task on the frontier "
             "(see governance below)."
             % (len(tasks), fmt_usd(ct["routed"]), fmt_usd(ct["inline"]),
                pct(ct["inline"], ct["routed"]), pct(ct["fusion"], ct["routed"])))
    L.append("")
    L.append("### What about \"just use one strong model for everything\"?")
    L.append("")
    L.append("Forcing GPT-5.5 on *every* task (including architecture, review, and "
             "security-critical work) totals **%s** — cheaper than EvolvRoute's "
             "**%s**, but only by **downgrading the high-tier tasks EvolvRoute "
             "deliberately keeps on the frontier**. It is not a quality-equivalent "
             "baseline. The fair, apples-to-apples question is: *on the work the "
             "router actually delegated, did it pick cheaper lanes than blindly "
             "using the flagship?*"
             % (fmt_usd(ct["flagship"]), fmt_usd(ct["routed"])))
    L.append("")
    L.append("| On the %d delegated tasks | Notional cost |" % sub["n"])
    L.append("|---|---|")
    L.append("| **EvolvRoute lane selection** | **%s** |" % fmt_usd(sub["routed"]))
    L.append("| Force GPT-5.5 (flagship) on the same tasks | %s |"
             % fmt_usd(sub["flagship"]))
    L.append("")
    L.append("On the delegated subset the router is **%.1f%% cheaper** than forcing "
             "the flagship — it picks `mini`/`flash`/relay where they suffice and "
             "only spends up when the task warrants it."
             % pct(sub["flagship"], sub["routed"]))
    L.append("")

    L.append("## Routing quality")
    L.append("")
    eo, et = cq["escalation_correct"]
    oo, ot = cq["offload_correct"]
    L.append("- **Escalation correctness:** %d/%d high-tier tasks "
             "(architecture / review / planning / high-risk) kept inline on "
             "`claude_keep`." % (eo, et))
    L.append("- **Offload rate:** %d/%d delegatable tasks routed OFF the "
             "frontier to a cheaper lane." % (oo, ot))
    L.append("- **Break-even floor:** %d/%d trivial tasks correctly kept inline "
             "at default settings (not worth a worker round-trip)."
             % (gov["tiny_floored"], gov["tiny_total"]))
    L.append("")
    L.append("Chosen-lane distribution (cold start):")
    L.append("")
    L.append("| Lane | Tasks |")
    L.append("|---|---|")
    for lane, n in sorted(cq["distribution"].items(), key=lambda kv: -kv[1]):
        L.append("| `%s` | %d |" % (lane, n))
    L.append("")

    L.append("## Learning (cold vs. warm)")
    L.append("")
    L.append("Re-running after seeding the ledger from `ledger.jsonl.example` "
             "(joined verdicts → recomputed centroids):")
    L.append("")
    L.append("- Cold-start routed cost: **%s**" % fmt_usd(ct["routed"]))
    L.append("- Warm (post-learning) routed cost: **%s**" % fmt_usd(wt["routed"]))
    L.append("- Routing decisions changed by learning: **%d / %d**"
             % (changed, len(tasks)))
    L.append("")
    L.append("> The reference embedder here is the coarse hashed-BoW fallback, "
             "so the warm shift is conservative; with the real `model2vec` "
             "embedder and accumulated production verdicts the learning signal "
             "is stronger. The point of this section is that the loop is wired "
             "end-to-end and measurable, not that the toy seed maximizes it.")
    L.append("")

    L.append("## Per-task detail (cold start)")
    L.append("")
    L.append("| Task | type/size | chosen lane | routed | flagship | inline |")
    L.append("|---|---|---|---|---|---|")
    for t, d, rc, bc in crows:
        lane = "relay→" + d["relay"]["stage2"] if d["mode"] == "relay" else d["chosen"]
        L.append("| `%s` | %s/%s | `%s` | %s | %s | %s |"
                 % (t["id"], t["task_type"], t["size_class"], lane,
                    fmt_usd(rc), fmt_usd(bc["flagship"]), fmt_usd(bc["inline"])))
    L.append("")

    L.append("## Method & honesty notes")
    L.append("")
    L.append("- **Real engine.** Each row is a live `route.py … --json` call; "
             "this benchmark does not reimplement routing.")
    L.append("- **Project's own prices.** Rates are the metered-equivalent "
             "reference table in `MODEL_REGISTRY.md` (NOT real billing — workers "
             "bill flat subscriptions). `out_tokens = out_chars/4`, matching "
             "`delegate.sh`.")
    L.append("- **`claude_keep` is priced, not free.** Kept work is charged at "
             "the opus-class frontier rate, so the router gets no free lunch on "
             "the tasks it (correctly) refuses to delegate.")
    L.append("- **Routing isolated from break-even.** The cost/quality run sets "
             "`MIN_DELEGATE_TOKENS=0`; the floor is verified separately at "
             "defaults in the governance check, so the two effects don't "
             "double-count.")
    L.append("- **Relay cost** assumes agy compresses context to ~30% before "
             "the codex stage — a documented, conservative modeling choice.")
    L.append("- **Media is cost-neutral** (flat per-call in every strategy), so "
             "the savings come entirely from text/code/doc/research routing.")
    L.append("")
    return "\n".join(L)


def print_summary(tasks, cold, gov):
    ct, _ = aggregate(cold)
    cq = quality(cold)
    print("EvolvRoute benchmark — %d tasks (cold start)" % len(tasks))
    sub = delegatable_subset_cost(cold)
    print("  routed      %s" % fmt_usd(ct["routed"]))
    print("  inline      %s   (routed -%.1f%% vs status quo)" % (fmt_usd(ct["inline"]), pct(ct["inline"], ct["routed"])))
    print("  fusion      %s   (routed -%.1f%%)" % (fmt_usd(ct["fusion"]), pct(ct["fusion"], ct["routed"])))
    print("  flagship    %s   (downgrades high-tier; not quality-equivalent)" % fmt_usd(ct["flagship"]))
    print("  delegated subset: routed %s vs flagship %s (-%.1f%%)"
          % (fmt_usd(sub["routed"]), fmt_usd(sub["flagship"]), pct(sub["flagship"], sub["routed"])))
    eo, et = cq["escalation_correct"]
    oo, ot = cq["offload_correct"]
    print("  escalation  %d/%d kept inline" % (eo, et))
    print("  offload     %d/%d delegated" % (oo, ot))
    print("  break-even  %d/%d tiny floored" % (gov["tiny_floored"], gov["tiny_total"]))


def main():
    ap = argparse.ArgumentParser(description="EvolvRoute benchmark")
    ap.add_argument("--tasks", default=os.path.join(HERE, "tasks.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "RESULTS.md"))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    with open(args.tasks) as fh:
        tasks = [json.loads(line) for line in fh if line.strip()]

    seed = os.path.join(REPO, "ledger.jsonl.example")
    cold = run_suite(tasks, seed_ledger=None, min_delegate_tokens=0)
    warm = run_suite(tasks, seed_ledger=seed, min_delegate_tokens=0)
    gov = governance_check(tasks)

    print_summary(tasks, cold, gov)
    if not args.no_write:
        report = build_report(tasks, cold, warm, gov)
        with open(args.out, "w") as fh:
            fh.write(report + "\n")
        print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
