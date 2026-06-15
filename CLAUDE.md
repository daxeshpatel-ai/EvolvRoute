# EvolvRoute — Operator Manual (for Claude)

## 1. What this is

Thin subprocess delegation harness. Claude is the controller and QC; `codex` (OpenAI),
`agy` (Antigravity/Gemini), `grok` (xAI) are subordinate lanes that spend their OWN
subscription quotas (ChatGPT plan / Google free tier / SuperGrok) — not Anthropic credits.
One shell entrypoint (`delegate.sh`), a geometric router on top (`delegate.sh auto`),
and a ledger-driven learning loop (verdicts → digest → stop-cells → routing).

| Doc | Holds |
|---|---|
| `MODEL_REGISTRY.md` | per-CLI capabilities, models, quotas, exact headless syntax, measured timeouts |
| `DELEGATION_RULES.md` | delegate-vs-do policy, tier definitions, delegation matrix, learning loop §(g), router §(h) |
| `router/README.md` | router data layer: handlers.json schema, DB schema, embedder, centroids |
| this file | how to operate day-to-day |

## 2. Decision procedure (delegate-vs-do)

Delegate ONLY if ALL hold; any failure → Claude does it. When in doubt, Claude does it.

1. Task is **low or mid tier**. High tier — architecture, cross-cutting design, review,
   QC, planning, complex debugging, security/correctness-critical, whole-system
   consistency — is NEVER delegated.
2. **Unambiguous spec** — a worker with zero project context could execute it correctly.
3. Output is **cheaply verifiable** (compiles, tests pass, small diff, checkable facts).
4. A worker has a real **edge** (cost, speed, context window, or image/video modality).
5. **Min-artifact threshold**: expected artifact ≥ ~1KB. Codex burns ~15K fixed tokens
   per call; tiny delegations are a net loss — do them inline.

Lane defaults (full matrix in `DELEGATION_RULES.md` §(c)): boilerplate → codex mini;
mid feature/tests → codex gpt-5.5; docs/bulk text/research/huge-context → agy;
image/video → grok imagine; huge-context code change → relay (agy compress → codex 5.5).

## 3. Command cheatsheet

All from `cli-orchestra/`. Timeouts in seconds; arg beats `TO=` env (default 150).

```
TASK_TYPE=code SIZE_CLASS=small ./delegate.sh auto "<prompt>" [timeout]   # routed
./delegate.sh [--code] <codex|agy|grok> <model|-> "<prompt>" [timeout]    # manual
./delegate.sh verdict <id> accept|edit|reject [edit_pct]                  # record outcome
./delegate.sh quota                                                       # today's burn
python3 router/ingest.py sync                                             # sync ledger→DB
python3 digest.py                                                         # DIGEST.md + stop-cells
python3 router/route.py "<task>" --task-type T --json                     # dry routing, no dispatch
```

`<model|->`: `-` = CLI default; codex accepts `mini` shorthand. `--code` strips one
outer markdown fence. Each call prints `id=<id>` to stderr — keep it for the verdict.

| Env var | Values / meaning |
|---|---|
| `TASK_TYPE` | code\|test\|doc\|research\|media\|refactor\|other |
| `SIZE_CLASS` | small\|medium\|large |
| `MODALITY` | text\|image\|video |
| `TO` | default timeout secs (per-call arg wins) |
| `FALLBACK_TO` | fallback-hop timeout floor (default 150) |
| `STRICT_QUOTA=1` | refuse over-cap dispatch (exit 3) instead of warn |
| `CODEX_CAP`/`AGY_CAP`/`GROK_CAP` | soft caps: 40/5h, 800/24h, 150/24h |
| `SPEND_CAP` | NOTIONAL USD ceiling on today's spend; unset = off. Rates in MODEL_REGISTRY.md |
| `STRICT_SPEND=1` | refuse over-`SPEND_CAP` dispatch (exit 3) instead of warn |
| `MEDIA_FLAT_USD` | flat notional cost per grok-media call (default 0.04) |

| Exit | Meaning |
|---|---|
| 0 | OK — stdout is the artifact |
| 2 | usage error |
| 3 | STRICT_QUOTA or STRICT_SPEND refusal, no call made |
| 4 | router/parse failure (`auto` only) |
| 5 | claude_keep — handle it directly, no CLI call, no ledger row |
| 6 | grok_media — use the imagine commands in MODEL_REGISTRY.md |
| 124 | timeout (after one fallback-lane hop: codex→agy, agy→codex, grok→agy) |

## 4. The operating loop

1. Classify the task (tier + TASK_TYPE/SIZE_CLASS) via §2.
2. Dispatch: `auto` for routine routing, manual lane when the matrix answer is obvious.
3. Review the printed stdout (final artifact only — never worker reasoning).
4. Integrate the artifact yourself (workers are read-only; Claude is sole writer).
5. Record the verdict: `./delegate.sh verdict <id> accept|edit|reject [edit_pct]`.
6. `python3 router/ingest.py sync` so centroids/outcomes reflect the verdict.
7. Periodically `python3 digest.py` — refreshes DIGEST.md and stop-cells.

**Health metric: verdict coverage > 70%** (digest reports it). Unverdicted calls teach
the router nothing.

## 5. Routing internals (6 lines)

Hard filters first: modality (media↔grok_media), escalation (risk=high or task_type
architecture/review/planning → claude_keep only), per-owner quota windows, stop-cells
from `router/stopcells.json`; claude_keep is the floor and never excluded.
Score = 0.45·centroid sim + 0.25·best proven-outcome (cosine×quality) + 0.20·smoothed
success (Bayes prior 0.7) + 0.05·cost pref + 0.05·latency pref. Relay mode fires for
code/refactor/test at size_class=large: agy_relay compresses context → codex_55 implements.
Centroids are confidence-weighted: declared vector dominates (weight floors at 0.5 by
~10–20 outcomes per handler), so early routing follows handlers.json declarations.

## 6. Gotchas

| Gotcha | Detail |
|---|---|
| codex SIGTERM trap | exits rc=0 with EMPTY stdout on watchdog kill; the empty-output guard is what triggers the fallback hop |
| agy scratch cwd | agy runs from a throwaway $TMPDIR — repo reads MUST use ABSOLUTE paths |
| agy/grok cold start | ~70s; size timeouts accordingly (agy ~240s, codex mini ~120s, codex 5.5 ~300s+) |
| codex per-call overhead | ~15K fixed tokens — enforces the §2 min-artifact rule |
| grok media | needs `--always-approve` (read-only sandbox blocks saves); outputs land in `~/.grok/sessions/<url-encoded-cwd>/<id>/images\|videos/` — copy newest out by mtime |
| grok image format | always JPEG regardless of the extension you request |
| noisy embedder | hashed-BoW-256 is coarse — `routing_tags`/declared capabilities in handlers.json are the effective prior until outcomes accumulate |
| prompts not stored | ledger keeps only `spec_hash`, never prompt text — still, never put secrets in delegation prompts (they hit the worker CLI and its session store) |

## 7. File map

- `delegate.sh` — entrypoint: dispatch, auto-route, quota, verdict
- `usage.log` — one JSON line per final call (feeds `quota`)
- `ledger.jsonl` — rich call + verdict rows (the learning signal)
- `digest.py` / `DIGEST.md` — ledger rollup: per-lane accept/edit/reject, coverage, stop-cell writer
- `router/handlers.json` — handler capability cards (declared routing prior)
- `router/ingest.py` — `sync`: cards + verdicted outcomes → DB, recompute centroids
- `router/route.py` — scorer/filter; logs every decision to invocations table
- `router/orchestra.db` — SQLite store (gitignored, rebuilt by sync)
- `router/stopcells.json` — bad lane×task_type cells, hard-filtered by route.py
- `MODEL_REGISTRY.md` / `DELEGATION_RULES.md` — see pointer table in §1

## 8. Maintenance

- This dir is its own git repo — commit before risky edits to delegate.sh/router.
- `ledger.dev-archive.jsonl` = pre-baseline dev data; excluded from the live loop.
- Re-verify `MODEL_REGISTRY.md` quotas monthly — 🔴-tagged items (grok beta limits,
  Sora retirement, context sizes) are volatile.
