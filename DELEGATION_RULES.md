# Delegation Rules

How Claude decides whether to delegate a task to a subprocess CLI worker
(`codex` / `agy` / `grok`) or do it itself. See `MODEL_REGISTRY.md` for syntax/limits.

> **Last tuned: 2026-06-11** (pilot findings folded in — see §(f)).

---

## (a) The delegate-vs-do test

**Delegate only if ALL of these hold:**
1. The task is **low or mid tier** (see definitions below).
2. The task has an **unambiguous spec** — a worker with no project context could execute it correctly.
3. The output is **cheaply verifiable** by Claude (compiles, tests pass, diff is small, factual claims checkable).
4. A worker has a real **edge**: lower cost, higher speed, larger context window, or a modality Claude lacks (image/video gen).

**If any condition fails → Claude does it.** When in doubt, Claude does it.

---

## (b) Tier definitions

- **Low** — boilerplate, mechanical transforms, single-function snippets, bulk text/doc drafting, simple lookups. Spec is obvious; verification is trivial.
- **Mid** — a self-contained feature, a module, a focused test suite, a bounded research question. Spec is clear but execution has some depth; verification is feasible.
- **High** — architecture, cross-cutting design, ambiguous requirements, security/correctness-critical work, anything needing judgment or whole-system consistency. **Never delegated.**

---

## (c) Delegation matrix

| Task | Tier | Primary | Fallback |
|---|---|---|---|
| Low coding / boilerplate | Low | codex `gpt-5.4-mini` | agy Flash |
| Mid feature implementation | Mid | codex `gpt-5.5` | grok `grok-build` |
| Tests | Mid | codex `gpt-5.5` | — |
| Docs / bulk text | Low | agy Flash | codex `mini` |
| Research / web-grounded | Mid | agy Flash | grok (real-time X data) |
| Huge-context / whole-repo read | Mid | agy (1M+ ctx) | — |
| Image generation | — | grok imagine | `gpt-image-1` API |
| Video generation | — | grok imagine | **(NOT Sora)** |
| Huge-context code change | Mid | RELAY: agy (compress→summary file) → codex gpt-5.5 (implement) | — |
| Architecture / review / QC / planning / complex debugging / consistency | High | **CLAUDE (keep)** | — |

**Context-relay pattern (the one sanctioned multi-CLI chain):** stage 1 — agy reads the
repo via ABSOLUTE paths (scratch-cwd lane) and writes a compact summary/spec to a file;
Claude skims the summary (cheap QC); stage 2 — codex gpt-5.5 implements with the summary
inlined in its prompt.

---

## (d) Fallback chain

Prefer the **most generous / cheapest tier first**, escalate only on failure:

```
agy (Google free, ~1000–1500/day)  →  codex / grok (subscription coding)  →  Claude (last resort for delegable work)
```

- Use agy first for anything text/research/huge-context where it qualifies.
- Use codex/grok subscription quota for coding/tests that need stronger models.
- Claude is the **last resort for *delegable* work** — but remains **first and only** for High-tier tasks (those are never delegated).

---

## (e) Guardrails

- **Never delegate unverifiable output.** If Claude can't cheaply confirm correctness, Claude does it.
- **Never use Sora** for video. Video gen → grok imagine only.
- **agy auto-selects its model** — do not pin a model for agy.
- Workers run **read-only** by default (`delegate.sh`) — they cannot mutate the repo. Claude applies any resulting changes itself after review.
- Claude reads only the worker's **final artifact**, never its chain-of-thought.

---

## (f) Pilot learnings (tuned 2026-06-11)

- **Min-artifact threshold — don't delegate trivial output.** If the expected artifact
  is **< ~1KB / trivially small**, Claude does it inline. Codex carries a **fixed ~15K-token
  per-call overhead** (one "Reply OK" call burned ~15,221 tokens), and the spec-writing +
  result-review cost on top makes small delegations a net loss. **Delegate substantial
  artifacts only** (a real module, a test suite, a sizeable doc/research answer).

- **Timeouts & latency.** Use the per-CLI first-attempt timeouts in
  `MODEL_REGISTRY.md` → *Timeouts & latency*: codex mini ~120s, codex gpt-5.5 ~300s+,
  agy/grok ~240s **plus ~70s cold start**. **codex is the lowest-latency lane** — prefer
  it for latency-sensitive qualified work; agy/grok have higher cold start and suit
  throughput/big-context work. (The `delegate.sh` watchdog bug that made every call burn
  its full first-timeout was fixed 2026-06-11 — fast calls now finish in real latency.)

- **Parallelize independent steps.** Delegated steps with no data dependency should be
  launched **in parallel** (the pilot ran 3 concurrently to stay in budget), not serially.

- **Measured ROI.** The pilot measured **~25% Claude-token saving on an all-code slice**.
  Media-inclusive sprints (image/video gen, a modality Claude lacks) are expected to save
  **~35–50%**, since those tasks offload entirely to a worker lane.

---

## (g) Learning loop & quota gate

Every `delegate.sh` call now also appends a richer row to **`ledger.jsonl`** (id,
spec_hash, est_in_tokens, latency_s, fallback_used) and prints its id to stderr
(`delegate.sh: id=<id>`).

- **Verdict workflow.** After Claude integrates a delegated artifact, record the
  outcome: `delegate.sh verdict <id> <accept|edit|reject> [edit_pct]`
  (`accept` = used as-is, `edit` = used after edits — optional `edit_pct` 0-100,
  `reject` = discarded). This is what turns the ledger into a feedback signal.
- **Tune the matrix.** Run `python3 digest.py` periodically. It joins calls↔verdicts
  and writes `DIGEST.md` + a stdout summary: per-lane accept/edit/reject %, verdict
  coverage, avg latency, fallbacks, and flags any lane with **reject% > 30** (≥3
  verdicts) as `⚠ HIGH REJECT`. Use it to re-rank the §(c) matrix.
- **Soft quota caps (warn-only).** Before dispatching, the wrapper counts recent
  `call` rows for the chosen CLI within its window (codex 5h, agy/grok 24h) and
  warns at the cap: `CODEX_CAP` (40), `AGY_CAP` (800), `GROK_CAP` (150). Set
  **`STRICT_QUOTA=1`** to refuse instead (exit 3, no call made).
- **Timeout → fallback hop.** Fires on **both** `rc==124` (watchdog timeout) **and**
  `rc==0-with-empty-output` (codex traps SIGTERM and exits 0 with empty stdout on a
  watchdog kill, so empty-output is the real timeout signal for the codex lane). The
  wrapper does **not** retry the same lane — it hops once to a fallback (codex→agy,
  agy→codex, grok→agy) with model auto and a fallback timeout floor (`FALLBACK_TO`,
  default 150s). Non-timeout errors keep the prior single same-lane retry. The
  ledger/usage rows reflect the **final** lane actually used (`fallback_used:true`).
- **agy lane is write-isolated.** agy runs from a throwaway `$TMPDIR` scratch cwd so it
  cannot mutate the repo. **Consequence:** a delegated agy task that must **read** repo
  files MUST reference them by **absolute path** (relative paths resolve against the temp
  dir, not the repo). grok text lane uses `--sandbox read-only`; codex uses
  `--sandbox read-only`.
- **Verdict workflow reminder.** After integrating a delegated result, call
  `./delegate.sh verdict <id> accept|edit|reject [edit_pct]`; run `python3 digest.py`
  periodically to tune the §(c) matrix from real accept/reject rates.

---

## (h) Geometric router

- **route.py** — `python3 router/route.py "<task>" [--task-type T] [--size-class S] [--modality M] [--risk R] [--json]` embeds the task (hashed-BoW-256) and ranks the 7 handler cards in `router/orchestra.db`; logs every decision to the invocations table.
- **auto lane** — `./delegate.sh auto "<prompt>" [timeout]` routes via route.py then dispatches through the normal path. Exit 5 = `claude_keep` (handle directly, no CLI call); exit 6 = `grok_media` (use the documented imagine commands).
- **Scoring** — 0.45·centroid similarity + 0.25·best proven-outcome match (cosine × quality) + 0.20·smoothed success (Bayesian prior 0.7, >30-day outcomes half weight) + 0.05·cost pref + 0.05·latency pref.
- **Hard filters** — modality (media ↔ grok_media), risk-high / architecture / review / planning escalation, per-owner quota (same CODEX_CAP/AGY_CAP/GROK_CAP windows as delegate.sh), and stop-cells from `router/stopcells.json` (written by `digest.py` when a lane×task_type cell has n≥3 and reject%>30 or avg edit_pct>35).
- **claude_keep floor** — claude_keep is never excluded; if everything else is filtered out, the router returns it.
- **Sync cadence** — run `python3 router/ingest.py sync` after recording verdicts so outcomes/centroids reflect the latest ledger.
