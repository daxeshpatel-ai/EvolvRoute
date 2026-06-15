# router/ — geometric routing layer (WP1: foundation)

Embedding-based handler routing for the EvolvRoute harness. WP1 ships the
data layer only (handler cards, SQLite store, ingest/sync, embedder + centroids).
Routing itself (`route.py`) is WP2.

## Files

- `handlers.json` — handler capability cards (source of truth for declared capabilities).
  **JSON, not YAML**, chosen deliberately: stdlib Python has no YAML parser and
  no pip installs are allowed on this machine.
- `ingest.py` — `python3 ingest.py sync` upserts handler cards, ingests verdicted
  calls from `../ledger.jsonl` into `outcomes`, and recomputes handler centroids.
  Idempotent. Exposes importable `embed(text)` and `cosine(a, b)`.
- `orchestra.db` — SQLite database (created on first sync).

## handlers.json schema (per card)

| field | type | values / meaning |
|---|---|---|
| `id` | str | unique handler id |
| `owner` | str | `codex` \| `agy` \| `grok` \| `claude` |
| `handler_type` | str | `cli_lane` (delegate.sh worker) \| `primary` (Claude keeps) |
| `model` | str | resolved model for the lane |
| `strong_at` | list[str] | declared strengths |
| `weak_at` | list[str] | declared weaknesses |
| `input_types` | list[str] | accepted input modalities |
| `output_types` | list[str] | produced artifact types (e.g. `code`, `summary_file`, `image`) |
| `tools` | list[str] | invocation surface / notable flags |
| `cost_band` | str | `free_quota` \| `subscription` \| `claude_tokens` |
| `latency_band` | str | `low` \| `medium` \| `high` |
| `risk_level` | str | `low` \| `medium` \| `high` — risk-high TASKS route to `claude_keep` |
| `max_artifact_hint` | str | rough artifact-size / quota ceiling |
| `enabled` | bool | card is live |
| `notes` | str | 1-2 lines incl. gotchas (codex SIGTERM-empty, agy scratch-cwd absolute paths, grok media `--always-approve`) |

## DB schema

- `handlers(id PK, owner, handler_type, capability_text, metadata_json, enabled, created_at, updated_at)`
- `outcomes(id PK, task_summary, handler_id, task_type, artifact_type, quality_score, tests_passed, human_approved, cost_tokens, wall_time_ms, error_class, created_at)`
- `invocations(id PK, task_text, chosen_handler_id, router_score, status, created_at)` — empty in WP1; populated by WP2's route.py
- `embeddings(object_type, object_id, embedding_model, vector, text_hash, PK(object_type, object_id))`
  - `object_type` ∈ `handler` (declared capability vec) \| `outcome` (task-summary vec) \| `handler_centroid` (learned vec)

## Embedder

Deterministic hashed bag-of-words TF, dim=256: lowercase, tokenize `[a-z0-9_]+`,
bucket = `int(md5(token)[:8], 16) % 256`, accumulate counts, L2-normalize.
`embedding_model = "hashed-bow-256"`. If `sentence-transformers` is importable
(it is NOT on this machine — never pip-installed), `all-MiniLM-L6-v2` is used
instead and recorded accordingly.

## Centroids

`centroid = normalize(w_d * declared + w_s * avg(success vecs) - w_f * avg(fail vecs))`
where success = outcome quality_score ≥ 0.7, fail = quality_score < 0.3, and
confidence scaling `w_d = max(0.5, 1 - n_outcomes/20)`, `w_s = (1-w_d)*0.8`,
`w_f = (1-w_d)*0.2`. With 0 outcomes the centroid equals the declared vector.

## Outcome ingestion & handler mapping

Verdicted `call` rows in `../ledger.jsonl` are joined to `verdict` rows by id:
quality_score = 1.0 (accept), `1 - edit_pct/100` (edit; null edit_pct → 0.7),
0.0 (reject). Handler id is inferred from the call's cli+model:
codex/gpt-5.4-mini → `codex_mini`, codex/* → `codex_55`, agy/* → `agy_flash`,
grok/* → `grok_text`.

## Known limitation — task_summary

The ledger does NOT store prompts (only `spec_hash`), so outcome
`task_summary` is built from available metadata
(`task_type=<t> cli=<cli> model=<m> spec_hash=<h> out_chars=<n>`), which makes
outcome embeddings coarse until richer task profiles accumulate. delegate.sh
now records optional `task_type` / `size_class` / `modality` fields on call
rows (env vars `TASK_TYPE`, `SIZE_CLASS`, `MODALITY`); canon values:
task_type ∈ code|test|doc|research|media|refactor|other,
size_class ∈ small|medium|large, modality ∈ text|image|video.
