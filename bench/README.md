# Benchmark — EvolvRoute's "midterm exam"

A reproducible benchmark that answers the question every router has to answer:
**does routing-to-cheapest-capable actually save money, without dropping
quality on the work that matters?**

## Run it

```bash
python3 bench/benchmark.py            # run, print summary, regenerate RESULTS.md
python3 bench/benchmark.py --no-write  # summary only
```

No dependencies beyond the standard library. With `model2vec` absent the router
uses its deterministic `hashed-bow-256` embedder, so results are identical
run-to-run. Latest results are committed in [`RESULTS.md`](RESULTS.md).

## What it measures

1. **Cost** — total notional spend for routing vs. three alternatives:
   - *all-frontier inline* — do everything on the frontier model (the status quo
     for a single powerful assistant);
   - *all-fusion* — run every lane in parallel and judge (the "to be safe" N×
     anti-pattern), quality-matched by including the frontier in every fan-out;
   - *all-flagship* — force one strong-but-cheaper model on everything. This one
     is **not** quality-equivalent (it downgrades high-tier work), so it's
     reported with a caveat plus an apples-to-apples comparison on just the
     tasks the router delegated.
2. **Routing quality** — escalation correctness (high-tier work kept on the
   frontier), offload rate (delegatable work actually leaves the frontier), and
   the break-even floor (trivial tasks stay inline).
3. **Learning** — re-runs after seeding the ledger from `ledger.jsonl.example`
   and reports how many routing decisions the recomputed centroids changed.

## How it stays honest

- **Real engine.** Every decision is a live `router/route.py … --json` call —
  the benchmark never reimplements routing.
- **Project's own prices.** Costs use the notional rate table from
  `MODEL_REGISTRY.md` (mirrored by `delegate.sh`). These are
  metered-*equivalent* reference prices, **not** real billing — the worker CLIs
  spend their own flat subscriptions.
- **`claude_keep` is priced, not free.** Work kept inline is charged at the
  opus-class frontier rate, so the router earns no free lunch on what it
  declines to delegate.
- **Break-even isolated.** The cost/quality run disables the break-even floor
  (`MIN_DELEGATE_TOKENS=0`) so lane-selection quality is measured on its own;
  the floor is then verified separately at default settings.

## The task suite

[`tasks.jsonl`](tasks.jsonl) — 28 tasks spanning code / test / refactor / doc /
research / media / architecture / review / planning, across small→large sizes,
text + image + video, plus a few high-risk and trivial tasks. Each carries a
realistic input/output size for costing and an `expected` disposition
(`keep` / `delegate` / `media`) for scoring routing quality. Edit it to stress
the router with your own workload mix.
