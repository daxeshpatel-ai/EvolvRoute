# EvolvRoute

**by Daxesh Patel**

> Routing that gets cheaper the more you use it.

*A self-learning router that sends each AI task to the cheapest model that can actually do it — and gets smarter every time it runs.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![embeddings: local (model2vec)](https://img.shields.io/badge/embeddings-local%20(model2vec)-green.svg)](https://github.com/MinishLab/model2vec)
[![status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![good first issues](https://img.shields.io/badge/good%20first%20issues-open-7057ff.svg)](#get-involved)
[![GitHub stars](https://img.shields.io/github/stars/daxeshpatel-ai/EvolvRoute?style=social)](https://github.com/daxeshpatel-ai/EvolvRoute)

EvolvRoute is a self-learning AI workload router. It learns from every outcome and routes each task to the cheapest model that can actually do the job — so it gets smarter and cheaper the more you use it. Instead of paying frontier prices for every task — or running them all in parallel and paying N× — it embeds each task, scores your available models on cost, capability, and proven track record, and routes to the single cheapest one that can do it. Every result is graded and fed back, so the router's judgment compounds.

## The problem

Not every task needs a frontier model — but choosing the right one by hand doesn't scale, and running them all *to be safe* multiplies your bill. EvolvRoute makes the call for you: it routes each task to the cheapest model that can actually do the job, then learns from what worked so the next call is sharper. Your cost curve bends down as the system gets smarter — the opposite of how AI usually scales.

## Process overview

![EvolvRoute process overview: a single task enters the controller, the router embeds and scores every lane, governance gates enforce spend and quota caps, exactly one cheapest-capable lane executes, Claude grades the result, and the verdict feeds back into the self-learning loop](docs/process-overview.png)

*One task in, one cheapest-capable lane out — every result graded and fed back so the next route is better.*

| Diagram label | Component | Role |
|---|---|---|
| EvolvController | Claude controller / `delegate.sh` entrypoint | Receives the task, owns final judgment, dispatches to a lane |
| EvolvRouter | Geometric router (`router/route.py`, model2vec embed + score) | Embeds the task, applies hard filters, scores models, selects one lane |
| EvolvGovernance | Policy / quota / spend / sandbox gates | Enforces spend caps, quota windows, break-even floor, escalation, sandbox isolation |
| Text/Code lane | `codex_mini` / `codex_55` (fast workers) | Boilerplate, scoped features, tests |
| High-Context lane | `agy` (deep / large-context worker) | Huge-context reads, web-grounded research |
| Media lane | `grok` (image / video) | Media generation |
| Inline lane | `claude_keep` (local) | Tasks kept by Claude directly — no delegation |
| Orchestra DB | `router/orchestra.db` | Centroids & policies the router scores against |
| Usage Log & Verdicts + Ingest & Digest | EvolvIntelligence (`router/ingest.py` + `digest.py`) | The self-learning loop: re-embed, recompute centroids, write stop-cells |

## Table of contents

- [The problem](#the-problem)
- [Why it's different](#why-its-different)
- [How it works](#how-it-works)
- [Why it works the way it does](#why-it-works-the-way-it-does)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Security & data hygiene](#security--data-hygiene)
- [Adding your own model / CLI](#adding-your-own-model--cli)
- [The self-learning loop](#the-self-learning-loop)
- [Design notes](#design-notes)
- [Roadmap / where this is going](#roadmap--where-this-is-going)
- [Project status](#project-status)
- [Get involved](#get-involved)
- [Author](#author)
- [License](#license)

## Why it's different

- **Learns from outcomes, not prompts.** Routing is reshaped by accept/edit/reject verdicts and auto-graded failures (timeout, error, empty output) — the router improves from what actually happened, not from keyword or prompt heuristics.
- **Local semantic routing, no embedding API.** Tasks are embedded on-device with [model2vec](https://github.com/MinishLab/model2vec) and matched against learned centroids entirely offline — no embedding calls leave your machine.
- **Cost-governed by design.** Spend caps, quota windows, a break-even floor, and escalation gates bound every dispatch, so the router can never spend its way into trouble.

## How it works

A task enters, the router embeds it and scores every available lane on cost, capability, and proven track record, then selects the single cheapest lane that can do the job; the lane executes with a one-hop fallback if it fails, Claude grades the result, and the verdict feeds back into the router so the next route is sharper. See [ARCHITECTURE.md](ARCHITECTURE.md) for the deep dive.

- **Task** — a unit of work arrives at the controller and is classified (tier, task type, size, modality).
- **Router embed + score** — `route.py` embeds the task (model2vec) and scores lanes on centroid similarity, proven outcomes, cost, and latency.
- **Select ONE cheapest-capable lane** — exactly one lane is chosen, not an N× parallel ensemble (`claude_keep` is the floor and is never excluded).
- **Execute with one-hop fallback** — the lane runs read-only; on timeout or empty output it falls back a single hop.
- **Claude verdict** — the artifact is integrated and graded (accept / edit / reject).
- **Self-learning loop** — the outcome is ingested and digested, so the next routing decision is better.

## Why it works the way it does

Four deliberate bets, each with a tradeoff:

| Principle | The bet | The tradeoff |
|---|---|---|
| **Selection over ensemble** | One well-chosen model beats N-in-parallel for most work | You pay ~1×, not N× — fusion stays available for high-stakes tasks |
| **Memory over static rules** | Every outcome is training data, so routing compounds | Needs verdicts to learn — auto-graded on failures, one keystroke on success |
| **Local over API** | Embeddings run offline via model2vec | A slightly smaller model than a hosted embedder, but zero extra cost and zero data egress |
| **Cost as a first-class constraint** | Spend caps, quotas, and a break-even floor live in the router | A few guardrails to configure, but your budget is enforced, not hoped for |

## Quickstart

```bash
# 1. Install dependencies (model2vec local embedder)
pip install -r requirements.txt

# 2. Seed the ledger and usage log from the examples
cp ledger.jsonl.example ledger.jsonl && cp usage.log.example usage.log

# 3. Build the routing DB (embeddings, centroids, policies) from seed outcomes
python3 router/ingest.py sync

# 4. Roll up per-lane learning stats and write stop-cells
python3 digest.py

# 5. Route a task automatically (router picks the cheapest-capable lane)
./delegate.sh auto "summarize this RFC in 5 bullets"

# 6. Or dispatch to a specific lane (- = that CLI's default model)
./delegate.sh codex - "write a python function that parses ISO-8601 durations"

# 7. Grade the result so the router learns from it
./delegate.sh verdict <id> accept
```

> **Bring your own worker CLIs.** `codex`, `agy`, and `grok` are reference lanes — point EvolvRoute at your own tools by setting `CODEX_BIN` / `AGY_BIN` / `GROK_BIN`, or add a new lane (see below). `jq` is required; `graphviz` is optional (only needed to re-render the diagrams).

## Configuration

The most-used environment variables (defaults pulled from `delegate.sh` and `ARCHITECTURE.md`):

| Name | Default | Effect |
|---|---|---|
| `CODEX_BIN` | `codex` | Binary used for the Text/Code lane |
| `AGY_BIN` | `agy` | Binary used for the High-Context lane |
| `GROK_BIN` | `grok` | Binary used for the Media lane |
| `SPEND_CAP` | unset | Notional USD ceiling on today's spend (gate is off unless set) |
| `STRICT_SPEND` | `0` | When `1`, refuse over-spend dispatch (exit 3) instead of warning |
| `CODEX_CAP` | `40` | Per-owner soft quota cap for codex within its window |
| `AGY_CAP` | `800` | Per-owner soft quota cap for agy within its window |
| `GROK_CAP` | `150` | Per-owner soft quota cap for grok within its window |
| `MIN_DELEGATE_TOKENS` | `1000` (from policies) | Break-even floor — tasks below this stay inline |
| `NO_AUTOSYNC` | `0` | When `1`, skip the post-verdict `ingest` + `digest` |
| `AGY_MIN_TO` | `180` | Minimum timeout (secs) for direct agy calls, floors cold starts |

## Security & data hygiene

- EvolvRoute stores no API keys. Each worker CLI (`codex` / `agy` / `grok`, or your own) authenticates with its OWN session/credentials; EvolvRoute only shells out to them.
- Keep real credentials and session files (e.g. `~/.grok/`, `~/.local/`, provider config dirs) OUTSIDE the project directory — EvolvRoute never reads or copies them.
- `ledger.jsonl` and `usage.log` are gitignored: real queries you run are recorded locally for the learning loop but are never staged or committed. Only the synthetic `*.example` seeds are tracked.
- The ledger stores a `spec_hash` of each task, never the prompt text.
- Do not put secrets in task prompts — they are passed to the worker CLI and may be retained by that tool's own session store.
- Workers run sandboxed (`codex` / `grok` read-only; `agy` from a throwaway scratch cwd) so delegated tasks cannot mutate your repo.
- Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## Adding your own model / CLI

Nothing in the router is hardwired to the three reference lanes — a lane is just a capability card plus a few small touch-points, and once added, the learning loop picks it up automatically with no engine changes. See [docs/ADDING_A_LANE.md](docs/ADDING_A_LANE.md) for the step-by-step.

## The self-learning loop

Every dispatched call writes a row to the ledger, and each result gets a verdict — automatically when a call fails (timeout, error, or empty output is recorded as a negative verdict with zero human input) or manually via `./delegate.sh verdict <id> accept|edit|reject`. `python3 router/ingest.py sync` then joins calls to verdicts, re-embeds the outcomes, and recomputes each lane's centroid in the Orchestra DB. `python3 digest.py` rolls up per-lane quality and writes stop-cells (consistently-bad `lane × task_type` pairs) to `router/stopcells.json`. The next `auto` call reads the updated centroids, outcomes, and stop-cells — so routing gets better, and cheaper, the more you use it.

## Design notes

- [ARCHITECTURE.md](ARCHITECTURE.md) — the full system spec: components, request flow, the geometric router, governance gates, subcommands, and env vars.
- [FUSION_COMPARISON.md](FUSION_COMPARISON.md) — how EvolvRoute differs from OpenRouter Fusion: selection + memory (route to one cheapest-capable model and learn) vs a parallel ensemble (run many, pay N×, judge).
- [bench/RESULTS.md](bench/RESULTS.md) — the benchmark ("midterm exam"): routing the suite costs **~61% less** than running everything on a frontier model inline and **~71% less** than fusing every lane, while keeping 100% of high-tier work on the frontier. Reproduce with `python3 bench/benchmark.py` (see [bench/README.md](bench/README.md)).

## Roadmap / where this is going

Directional, not committed — these are the threads worth pulling next, shared so you can weigh in or help shape them:

- A `fuse` mode that runs K lanes in parallel and judges them, for the rare high-stakes task where being right matters more than being cheap.
- The router learning *when* fusion is worth the cost versus routing to a single lane — fusion as a learned decision, not a manual flag.
- More first-class lanes (any CLI or model via the lane spec), so the registry isn't limited to the three reference workers.
- Richer, stateful routing policies that carry more context across a session.
- Optional cost dashboards built straight from the ledger, so spend and learning are visible at a glance.

## Project status

**Alpha.** Single-user, single-host by design — no collaboration layer and no web UI. Interfaces and defaults may change.

## Get involved

EvolvRoute is early and deliberately small — which means your fingerprints can shape its direction. Good places to start:

- **Add a lane for your favorite CLI/model** — point the router at a new worker (see [docs/ADDING_A_LANE.md](docs/ADDING_A_LANE.md)).
- **Improve the scoring policy or break-even heuristic** — sharpen how the router decides what's cheapest-capable.
- **Stress-test the self-learning loop** — throw new task types at it and see where the routing breaks down.
- **Sharpen the docs** — clearer onboarding helps the next person route their first task faster.

Star the repo if the idea resonates, open an issue with how you'd use it, or send a PR — see [CONTRIBUTING.md](CONTRIBUTING.md) and our [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). The [`good first issue`](https://github.com/daxeshpatel-ai/EvolvRoute/labels/good%20first%20issue)-labeled tasks are the easiest entry point, and [`help wanted`](https://github.com/daxeshpatel-ai/EvolvRoute/labels/help%20wanted) marks where an extra hand goes furthest.

## Author

Built by **Daxesh Patel** — exploring how AI systems can be made cheaper, self-improving, and genuinely useful in production. EvolvRoute is one experiment in that direction: treating model choice as a learning problem instead of a fixed rule.

- Website: [daxeshpatel.com](https://daxeshpatel.com)
- GitHub: [github.com/daxeshpatel-ai](https://github.com/daxeshpatel-ai)
- LinkedIn: [linkedin.com/in/dhpatel](https://www.linkedin.com/in/dhpatel/)

## License

Licensed under the [Apache License 2.0](LICENSE) — see also [NOTICE](NOTICE). Created and maintained by **Daxesh Patel**.
