# Tests

Unit + integration coverage for the EvolvRoute routing engine and data layer.

## Running

```bash
pip install -r requirements-dev.txt   # just pytest
pytest -q                              # from the repo root
```

The suite is **hermetic and offline**. It deliberately runs *without*
`model2vec` installed, so `ingest.py` selects its deterministic
`hashed-bow-256` embedder fallback — no HuggingFace download, no network, and
stable vectors run-to-run. Every test points the DB / ledger / stop-cell /
policy paths at `tmp_path`, so your real `router/orchestra.db` and
`ledger.jsonl` are never touched.

## Layout

| File | Covers |
|---|---|
| `test_route_helpers.py` | `parse_ts`, ledger windows (`count_recent_calls`, `recent_call_rows`, `ledger_now`), stop-cell + policy loading |
| `test_route_filters.py` | hard filters — the `claude_keep`-is-the-floor invariant, escalation, modality, quota, stop-cells |
| `test_route_scoring.py` | `smoothed_success` (Bayesian prior + recency) and the `score_handler` weighting formula |
| `test_route_policy.py` | stateful policy filter — break-even floor, escalate-after-failure, lane cooldown |
| `test_ingest.py` | vector math, ledger→handler mapping, error classification, verdict→quality_score |
| `test_integration.py` | end-to-end `route.py` via subprocess: decision contract, escalation, relay, break-even, media routing |

CI runs the suite on Python 3.10–3.12 via `.github/workflows/tests.yml`.
