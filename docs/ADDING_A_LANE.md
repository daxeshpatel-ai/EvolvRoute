# Adding a Worker Lane

EvolvRoute ships three **reference lanes** — `codex`, `agy`, and `grok`. They are
worked examples, **not requirements**. Nothing in the router is hardwired to those
three CLIs: a lane is just a capability card plus four small touch-points. This guide
walks you through registering your own CLI (e.g. a `claude`, `ollama`, or `llm` lane)
using the existing `codex` lane as the template.

There are **four** touch-points. Do all four and the learning loop (routing → ledger →
verdicts → digest → stop-cells) picks up your lane automatically.

---

## 1. Add a capability card to `router/handlers.json`

The router scores lanes from their declared cards until real outcomes accumulate, so
this card is your initial routing prior. Copy the `codex_mini` card and edit it:

```json
{
  "id": "ollama_local",
  "owner": "ollama",
  "handler_type": "cli_lane",
  "model": "llama3.1",
  "strong_at": ["offline drafts", "cheap bulk text", "low latency"],
  "weak_at": ["architecture", "huge context", "media generation"],
  "input_types": ["text", "code"],
  "output_types": ["code", "text"],
  "tools": ["ollama run"],
  "cost_band": "free",
  "latency_band": "low",
  "risk_level": "low",
  "max_artifact_hint": "small module / single file",
  "enabled": true,
  "notes": "Local, no quota. Set OLLAMA_BIN to override the binary."
}
```

- `id` is the **handler id** referenced by the other three touch-points — keep it stable.
- `owner` is the **lane/quota bucket** (multiple handlers can share one owner, e.g.
  `codex_mini` and `codex_55` both have `owner: codex`).
- `strong_at` / `weak_at` drive the embedded centroid that the router matches tasks against.

## 2. Add a `build_cmd()` arm in `delegate.sh`

`build_cmd()` (search for `case "$CLI" in`) assembles the argv for the chosen CLI into
the global `CMD` array. Add an arm for your owner, mirroring the read-only/sandbox flag
your CLI supports:

```bash
    ollama)
      local model="$MODEL_ARG"
      case "$model" in -|"") model="llama3.1" ;; esac
      CMD=("$OLLAMA_BIN" run "$model" "$PROMPT")
      ;;
```

If your CLI needs a binary override, add it to the **CONFIG block** at the top of
`delegate.sh` next to `CODEX_BIN`/`AGY_BIN`/`GROK_BIN`:

```bash
OLLAMA_BIN="${OLLAMA_BIN:-ollama}"
```

and resolve it with the existing `resolve_bin` helper.

## 3. Add a rate to `model_rate()`

`model_rate()` returns `"IN_RATE OUT_RATE"` (notional USD per 1M tokens) so the spend
gate and `est_cost_usd` ledger field work for your lane. Add an arm:

```bash
    ollama)
      echo "0 0" ;;   # local model, no metered cost
```

(Use real reference rates if your CLI bills per token; `0 0` is fine for local models.)

## 4. Map handler → lane in two places

So a ledger row can be attributed back to your handler card:

- **`delegate.sh` auto-dispatch** — in the `case "$ROUTE_CHOSEN" in` block, map your
  handler id to its CLI + model:
  ```bash
    ollama_local)  AUTO_CLI="ollama"; AUTO_MODEL="llama3.1" ;;
  ```
- **`router/ingest.py` `map_handler_id()`** — teach the ingester to turn a `(cli, model)`
  ledger row back into your handler id:
  ```python
    if cli == "ollama":
        return "ollama_local"
  ```

---

## Verify

```bash
bash -n delegate.sh                                  # script still parses
python3 router/route.py "draft a changelog" --task-type doc --json   # your lane can be chosen
./delegate.sh ollama llama3.1 "say hi" 60            # it dispatches
```

Record verdicts (`./delegate.sh verdict <id> accept|edit|reject [edit_pct]`) and run
`python3 router/ingest.py sync && python3 digest.py` — after ~10–20 verdicted calls the
router will route to your lane based on measured outcomes rather than the declared card.

> The three reference lanes are not special-cased anywhere the four touch-points above
> don't cover. Adding a lane is additive: you never have to refactor the routing engine.
