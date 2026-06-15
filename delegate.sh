#!/usr/bin/env bash
# delegate.sh — thin multi-CLI delegation wrapper (read-only by default)
# Usage: delegate.sh [--code] <cli> <model|-> "<prompt>" [timeout_secs]
#        delegate.sh quota                 # summarize today's usage.log burn
#        delegate.sh verdict <id> <accept|edit|reject> [edit_pct]  # record outcome
#   cli ∈ codex | agy | grok
#   model "-" means default for that CLI; codex accepts "mini" shorthand
#   --code  strip a single outer markdown code fence from the output (clean code)
# Env: TO=<secs> overrides default timeout (150). Per-call timeout arg wins over TO.
#
# Behaviour:
#   - Each CLI call is run under a portable timeout (macOS has no `timeout` binary).
#   - On rc==124 (timeout): hop ONCE to a fallback lane (codex->agy, agy->codex,
#     grok->agy, model auto). On other non-zero rc: ONE same-lane retry.
#   - Known-benign MCP startup noise (rmcp / AuthRequired / mcp.facebook /
#     "transport error") is filtered from the returned text; real errors surface.
#   - Appends one JSON line per (final) call to usage.log AND a richer row to
#     ledger.jsonl (id, spec_hash, est_in_tokens, latency_s, fallback_used).
#     Optional task-profile fields on the ledger row: task_type/size_class/modality
#     from env TASK_TYPE/SIZE_CLASS/MODALITY (defaults unknown/unknown/text).
#     Canon: task_type code|test|doc|research|media|refactor|other;
#     size_class small|medium|large; modality text|image|video.
#   - Preflight quota gate: warns (or, with STRICT_QUOTA=1, refuses rc=3) when the
#     chosen CLI is at/over its soft cap within its window.
#   - Preflight spend gate (NOTIONAL, off unless SPEND_CAP set): sums today's
#     metered-equivalent cost from the ledger; warns (or, with STRICT_SPEND=1,
#     refuses rc=3) at/over SPEND_CAP. Each call also logs est_cost_usd to the
#     ledger. Rates are notional (MODEL_REGISTRY.md), not actual billing.
#   - Prints the CLI's stdout to our stdout; exits with the CLI's rc.

set -u

# ============================ CONFIG: worker binaries ========================
# The codex/agy/grok lanes are REFERENCE implementations. Point them at your own
# installs by exporting these env vars (e.g. CODEX_BIN=/opt/bin/codex). Each one
# falls back to `command -v` on PATH, then to a known per-user install path.
# To register a NEW lane (e.g. claude/ollama/llm), see docs/ADDING_A_LANE.md.
CODEX_BIN="${CODEX_BIN:-codex}"
AGY_BIN="${AGY_BIN:-agy}"
GROK_BIN="${GROK_BIN:-grok}"
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USAGE_LOG="${USAGE_LOG:-${SCRIPT_DIR}/usage.log}"
LEDGER="${LEDGER:-${SCRIPT_DIR}/ledger.jsonl}"

# --- auto-sync: after ANY verdict append (manual or auto), close the learning
# loop by re-syncing the router DB and refreshing the digest. BEST-EFFORT, QUIET
# (stdout -> /dev/null so the delegated artifact is never polluted; notices to
# stderr only) and NON-FATAL (never changes the caller's exit code). NO_AUTOSYNC=1
# skips it entirely (e.g. for tests against a temp ledger). ---
autosync() {
  [ "${NO_AUTOSYNC:-0}" = "1" ] && return 0
  python3 "${SCRIPT_DIR}/router/ingest.py" sync >/dev/null 2>&1 \
    || echo "delegate.sh: autosync ingest.py sync failed (non-fatal)" >&2
  python3 "${SCRIPT_DIR}/digest.py" >/dev/null 2>&1 \
    || echo "delegate.sh: autosync digest.py failed (non-fatal)" >&2
  return 0
}

# --- verdict subcommand: record an outcome for a prior call id. Makes NO CLI call. ---
# Usage: delegate.sh verdict <id> <accept|edit|reject> [edit_pct]
if [ "${1:-}" = "verdict" ]; then
  VID="${2:-}"
  VV="${3:-}"
  VPCT="${4:-}"
  if [ -z "$VID" ] || [ -z "$VV" ]; then
    echo "Usage: delegate.sh verdict <id> <accept|edit|reject> [edit_pct]" >&2
    exit 2
  fi
  case "$VV" in
    accept|edit|reject) ;;
    *) echo "delegate.sh: invalid verdict '$VV' (expected accept|edit|reject)" >&2; exit 2 ;;
  esac
  if [ -n "$VPCT" ]; then
    case "$VPCT" in
      ''|*[!0-9]*) echo "delegate.sh: edit_pct must be an integer 0-100" >&2; exit 2 ;;
    esac
    if [ "$VPCT" -lt 0 ] || [ "$VPCT" -gt 100 ]; then
      echo "delegate.sh: edit_pct must be 0-100" >&2; exit 2
    fi
    PCT_JSON="$VPCT"
  else
    PCT_JSON="null"
  fi
  VTS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"id":"%s","ts":"%s","type":"verdict","verdict":"%s","verdict_source":"manual","edit_pct":%s}\n' \
    "$VID" "$VTS" "$VV" "$PCT_JSON" >> "$LEDGER"
  echo "delegate.sh: recorded verdict=${VV} for id=${VID}${VPCT:+ edit_pct=${VPCT}}" >&2
  autosync
  exit 0
fi

# --- quota subcommand: report today's (UTC) usage.log burn. Makes NO CLI call. ---
if [ "${1:-}" = "quota" ]; then
  if [ ! -s "$USAGE_LOG" ]; then
    echo "no usage logged yet"
    exit 0
  fi
  TODAY="$(date -u +%Y-%m-%d)"
  echo "usage for ${TODAY} (UTC):"
  grep "\"ts\":\"${TODAY}" "$USAGE_LOG" 2>/dev/null | awk '
    {
      cli=""; ts=""; oc=0;
      if (match($0, /"cli":"[^"]*"/))      { cli=substr($0,RSTART+7,RLENGTH-8) }
      if (match($0, /"ts":"[^"]*"/))       { ts=substr($0,RSTART+6,RLENGTH-7) }
      if (match($0, /"out_chars":[0-9]+/)) { oc=substr($0,RSTART+12,RLENGTH-12)+0 }
      calls[cli]++; chars[cli]+=oc; last[cli]=ts;
    }
    END {
      if (length(calls)==0) { print "  no calls today"; next }
      for (c in calls)
        printf "  %-6s calls=%-3d last=%s out_chars=%d\n", c, calls[c], last[c], chars[c];
    }'
  exit 0
fi

# --- verdicts-pending subcommand: list ids of "call" rows in $LEDGER that have
# NO matching "verdict" row (one id/line on stdout), plus a count to stderr.
# Surfaces verdict-coverage gaps so the loop's negative/positive signal stays
# complete. Makes NO CLI call. ---
if [ "${1:-}" = "verdicts-pending" ]; then
  if [ ! -s "$LEDGER" ]; then
    echo "delegate.sh: pending unverdicted call ids: 0" >&2
    exit 0
  fi
  awk -F'"' '
    {
      line=$0; id=""; type="";
      if (match(line, /"id":"[^"]*"/))   id=substr(line,RSTART+6,RLENGTH-7);
      if (match(line, /"type":"[^"]*"/)) type=substr(line,RSTART+8,RLENGTH-9);
      if (id=="") next;
      if (type=="call")    { if (!(id in seen_v)) calls[id]=1 }
      if (type=="verdict") { seen_v[id]=1; delete calls[id] }
    }
    END {
      n=0;
      for (id in calls) { print id; n++ }
      printf "delegate.sh: pending unverdicted call ids: %d\n", n > "/dev/stderr";
    }' "$LEDGER"
  exit 0
fi

# --- auto subcommand: geometric router (router/route.py) picks the handler,
# we map it to a lane and FALL THROUGH to the normal dispatch path below —
# the exact same code path as a manual `delegate.sh <cli> <model> ...` call.
# Exit codes: 4=router/parse failure, 5=claude_keep (handle directly, no CLI
# call, no ledger row), 6=grok_media (use documented imagine commands). ---
if [ "${1:-}" = "auto" ]; then
  AUTO_PROMPT="${2:-}"
  AUTO_TIMEOUT="${3:-}"
  if [ -z "$AUTO_PROMPT" ]; then
    echo "Usage: delegate.sh auto \"<prompt>\" [timeout_secs]" >&2
    exit 2
  fi
  ROUTE_ARGS=( "$AUTO_PROMPT" --json )
  [ -n "${TASK_TYPE:-}" ]  && ROUTE_ARGS+=( --task-type "$TASK_TYPE" )
  [ -n "${SIZE_CLASS:-}" ] && ROUTE_ARGS+=( --size-class "$SIZE_CLASS" )
  [ -n "${MODALITY:-}" ]   && ROUTE_ARGS+=( --modality "$MODALITY" )
  ROUTE_JSON="$(python3 "${SCRIPT_DIR}/router/route.py" "${ROUTE_ARGS[@]}")" || {
    echo "delegate.sh: router failed (route.py non-zero rc)" >&2
    exit 4
  }
  ROUTE_INFO="$(printf '%s' "$ROUTE_JSON" | python3 -c '
import sys, json
d = json.load(sys.stdin)
chosen = d["chosen"]
reason = next((r for r in d.get("reasons", []) if r.startswith(chosen + ":")), "")
print(chosen)
print(d.get("score"))
print(d.get("mode"))
print(reason)
')" || { echo "delegate.sh: could not parse router JSON" >&2; exit 4; }
  ROUTE_CHOSEN="$(printf '%s\n' "$ROUTE_INFO" | sed -n 1p)"
  ROUTE_SCORE="$(printf '%s\n' "$ROUTE_INFO" | sed -n 2p)"
  ROUTE_MODE="$(printf '%s\n' "$ROUTE_INFO" | sed -n 3p)"
  ROUTE_REASON="$(printf '%s\n' "$ROUTE_INFO" | sed -n 4p)"
  echo "delegate.sh: ROUTE mode=${ROUTE_MODE} chosen=${ROUTE_CHOSEN} score=${ROUTE_SCORE} — ${ROUTE_REASON}" >&2
  case "$ROUTE_CHOSEN" in
    codex_mini)          AUTO_CLI="codex"; AUTO_MODEL="gpt-5.4-mini" ;;
    codex_55)            AUTO_CLI="codex"; AUTO_MODEL="gpt-5.5" ;;
    agy_flash|agy_relay) AUTO_CLI="agy";   AUTO_MODEL="-" ;;
    grok_text)           AUTO_CLI="grok";  AUTO_MODEL="grok-build" ;;
    grok_media)
      # Media is a real lane now. If the caller set a MEDIA_OUT target, dispatch
      # into the grok-media executor (below); otherwise keep the advisory + exit 6
      # so `auto` never silently spends media quota without a destination file.
      if [ -n "${MEDIA_OUT:-}" ]; then
        echo "ROUTE: grok_media — dispatching to grok-media lane (MEDIA_OUT=${MEDIA_OUT})." >&2
        if [ -n "$AUTO_TIMEOUT" ]; then
          set -- grok-media "$AUTO_PROMPT" "$AUTO_TIMEOUT"
        else
          set -- grok-media "$AUTO_PROMPT"
        fi
      else
        echo "ROUTE: grok_media — set MEDIA_OUT=/abs/target to execute, or use the documented imagine commands (MODEL_REGISTRY.md)." >&2
        exit 6
      fi
      ;;
    claude_keep)
      echo "ROUTE: claude_keep — handle directly, do not delegate." >&2
      exit 5
      ;;
    *)
      echo "delegate.sh: unknown routed handler '${ROUTE_CHOSEN}'" >&2
      exit 4
      ;;
  esac
  # Rewrite the positional args and fall through to the normal dispatch path.
  if [ -n "$AUTO_TIMEOUT" ]; then
    set -- "$AUTO_CLI" "$AUTO_MODEL" "$AUTO_PROMPT" "$AUTO_TIMEOUT"
  else
    set -- "$AUTO_CLI" "$AUTO_MODEL" "$AUTO_PROMPT"
  fi
fi

# --- parse optional --code flag from anywhere before the positional args ---
CODE_MODE=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--code" ]; then CODE_MODE=1; else ARGS+=("$a"); fi
done
set -- "${ARGS[@]:-}"

CLI="${1:-}"
# grok-media uses a 2-positional form: `grok-media "<prompt>" [timeout]` (no model
# arg). Normalize it into the same CLI/PROMPT/TIMEOUT_SECS slots the rest of the
# script reads, so the shared usage gate, ledger and timeout logic all apply.
if [ "$CLI" = "grok-media" ]; then
  MODEL_ARG="grok-build"
  PROMPT="${2:-}"
  TIMEOUT_SECS="${3:-${TO:-}}"
else
  MODEL_ARG="${2:-}"
  PROMPT="${3:-}"
  TIMEOUT_SECS="${4:-${TO:-150}}"
fi

if [ -z "$CLI" ] || [ -z "$PROMPT" ]; then
  echo "Usage: delegate.sh [--code] <codex|agy|grok> <model|-> \"<prompt>\" [timeout_secs]" >&2
  echo "       delegate.sh quota" >&2
  exit 2
fi

# --- agy timeout floor: agy carries a ~70s cold start, so a tiny timeout meant
# for a fast lane makes agy "time out" before it can answer. Bump any agy call
# below AGY_MIN_TO (default 180) up to the floor. Other lanes are unaffected. ---
if [ "$CLI" = "agy" ] && [ -n "$TIMEOUT_SECS" ]; then
  AGY_MIN_TO="${AGY_MIN_TO:-180}"
  if [ "$TIMEOUT_SECS" -lt "$AGY_MIN_TO" ]; then
    echo "delegate.sh: agy timeout ${TIMEOUT_SECS}s < floor ${AGY_MIN_TO}s — bumping to ${AGY_MIN_TO}s" >&2
    TIMEOUT_SECS="$AGY_MIN_TO"
  fi
fi

# Resolve binaries. Precedence: explicit CONFIG override (CODEX_BIN/AGY_BIN/
# GROK_BIN set to an absolute path that exists) > `command -v` on PATH > the
# known per-user install path. The CONFIG block above seeds these to the bare
# command name, so an unmodified clone resolves via PATH, then the home path.
resolve_bin() {  # resolve_bin <current_value> <bare_name> <home_fallback>
  local cur="$1" name="$2" home="$3"
  # If the user pointed us at an existing executable, honor it verbatim.
  if [ "$cur" != "$name" ] && [ -x "$cur" ]; then echo "$cur"; return; fi
  # Otherwise prefer PATH, then the known per-user install path, then bare name.
  command -v "$name" 2>/dev/null && return
  [ -x "$home" ] && { echo "$home"; return; }
  echo "$name"
}
CODEX_BIN="$(resolve_bin "$CODEX_BIN" codex "")"
AGY_BIN="$(resolve_bin  "$AGY_BIN"  agy  "$HOME/.local/bin/agy")"
GROK_BIN="$(resolve_bin "$GROK_BIN" grok "$HOME/.grok/bin/grok")"

# Portable timeout: run "$@" with a watchdog. Returns 124 on timeout.
# The watchdog POLLS (1s ticks) and exits the instant the child finishes, instead
# of one long `sleep "$secs"`. That long sleep was the bug: it inherits the write
# end of the `OUT="$(...)"` command-substitution pipe, so even after the child
# completed and we killed the watchdog *shell*, its orphaned `sleep` kept the pipe
# open and command substitution blocked for the FULL timeout — making every fast
# call appear to "time out" then succeed on retry. Polling means no lingering sleep,
# and on a genuine deadline we kill the child plus any helper descendants it spawned
# so the pipe closes promptly and we return 124.
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local cmd_pid=$!
  (
    waited=0
    while [ "$waited" -lt "$secs" ]; do
      kill -0 "$cmd_pid" 2>/dev/null || exit 0   # child done => stop watching
      sleep 1; waited=$((waited+1))
    done
    kill -TERM "$cmd_pid" 2>/dev/null; pkill -TERM -P "$cmd_pid" 2>/dev/null
    sleep 2
    kill -KILL "$cmd_pid" 2>/dev/null; pkill -KILL -P "$cmd_pid" 2>/dev/null
  ) &
  local wd_pid=$!
  wait "$cmd_pid" 2>/dev/null
  local rc=$?
  kill -TERM "$wd_pid" 2>/dev/null
  wait "$wd_pid" 2>/dev/null
  if [ $rc -ge 128 ]; then return 124; fi   # killed by signal => treat as timeout
  return $rc
}

# --- Adding a worker lane (codex/agy/grok are REFERENCE lanes) ---------------
# To register your own CLI (e.g. claude / ollama / llm):
#   (1) add a capability card to router/handlers.json (so the router can pick it),
#   (2) add a `case` arm here in build_cmd() for its argv + sandbox/read-only flag,
#   (3) add a rate to model_rate() (USD/1M tokens) so spend metering works,
#   (4) map handler->lane in the `auto` dispatch (RESOLVE block) AND in
#       router/ingest.py `map_handler_id`.
# Full worked walkthrough: docs/ADDING_A_LANE.md.
# -----------------------------------------------------------------------------
# Build the argv for the selected CLI into the global array CMD.
build_cmd() {
  case "$CLI" in
    codex)
      local model="$MODEL_ARG"
      case "$model" in
        -|"")  model="gpt-5.5" ;;
        mini)  model="gpt-5.4-mini" ;;
      esac
      CMD=( "$CODEX_BIN" exec --model "$model" --sandbox read-only \
            --skip-git-repo-check --color never "$PROMPT" )
      RESOLVED_MODEL="$model"
      ;;
    agy)
      # agy auto-selects its model; --print runs one prompt non-interactively.
      # agy has NO working-tree read-only flag (its --sandbox only restricts the
      # terminal tool, not file writes), so we make the lane non-mutating by
      # running agy from a throwaway scratch cwd: any files it writes land in temp,
      # not the repo. stdout/stderr still flow through unchanged (output capture is
      # unaffected — only the child's cwd differs). bash -c '... "$@"' keeps $PROMPT
      # in argv so no shell re-quoting of the prompt is needed.
      local scratch; scratch="$(mktemp -d "${TMPDIR:-/tmp}/agy-scratch.XXXXXX")"
      CMD=( bash -c 'cd "$1" || exit 1; shift; exec "$@"' _ "$scratch" \
            "$AGY_BIN" --print "$PROMPT" --print-timeout "${TIMEOUT_SECS}s" )
      RESOLVED_MODEL="auto"
      ;;
    grok)
      local model="$MODEL_ARG"
      case "$model" in -|"") model="grok-build" ;; esac
      # grok -p = single-turn headless; read-only sandbox; no interactive plan.
      CMD=( "$GROK_BIN" -p "$PROMPT" -m "$model" \
            --sandbox read-only --output-format plain )
      RESOLVED_MODEL="$model"
      ;;
    *)
      echo "Unknown cli: $CLI (expected codex|agy|grok)" >&2
      exit 2
      ;;
  esac
}

# grok-media is a self-contained lane handled below (after the quota helpers are
# defined); it does NOT use build_cmd's text-lane argv. Skip the text build here.
if [ "$CLI" != "grok-media" ]; then
  build_cmd
fi

# Strip known-benign MCP startup noise that some CLIs emit into the stream.
# Only these specific patterns are dropped; genuine errors pass through untouched.
strip_noise() {
  grep -v -E 'rmcp|AuthRequired|mcp\.facebook|transport error'
}

# Strip a SINGLE outer markdown code fence, if and only if the whole output is
# one fenced block (optionally preceded/followed by blank lines). Leaves prose,
# multi-block, or unfenced output untouched so we never mangle content.
strip_fence() {
  awk '
    { lines[NR]=$0 }
    END {
      # find first/last non-blank line indices
      f=0; l=0;
      for (i=1;i<=NR;i++) if (lines[i] ~ /[^ \t\r]/) { if(!f) f=i; l=i }
      if (f==0) { for (i=1;i<=NR;i++) print lines[i]; exit }   # all blank
      # outer wrapping fence: first non-blank starts with ``` and last is ```
      if (lines[f] ~ /^[ \t]*```/ && lines[l] ~ /^[ \t]*```[ \t\r]*$/ && l>f) {
        # ensure no intermediate fence lines (i.e. exactly one block)
        inner=0;
        for (i=f+1;i<l;i++) if (lines[i] ~ /^[ \t]*```/) inner++;
        if (inner==0) {
          for (i=f+1;i<l;i++) print lines[i];
          exit
        }
      }
      for (i=1;i<=NR;i++) print lines[i]
    }'
}

# Compute the "effective" output: apply the same strip_noise (and, in --code mode,
# strip_fence) transforms used for the final result, then trim ALL whitespace, and
# echo the result. Used to decide whether a rc=0 reply is genuinely EMPTY (e.g. when
# the watchdog TERMs codex and it exits 0 with no real content) so we can still hop.
effective_out() {
  local e
  e="$(printf '%s' "$1" | strip_noise)"
  if [ "$CODE_MODE" -eq 1 ]; then
    e="$(printf '%s' "$e" | strip_fence)"
  fi
  printf '%s' "$e" | tr -d '[:space:]'
}

# Resolve the fallback lane for a given cli (codex->agy, agy->codex, grok->agy).
fallback_lane() {
  case "$1" in
    codex) echo "agy" ;;
    agy)   echo "codex" ;;
    grok)  echo "agy" ;;
    *)     echo "" ;;
  esac
}

# --- preflight quota gate: count recent "call" rows for this CLI within its window.
# Warn-only by default; STRICT_QUOTA=1 refuses (exit 3) without any CLI call. ---
quota_window_secs() {
  case "$1" in
    codex) echo $((5*3600)) ;;   # 5h
    *)     echo $((24*3600)) ;;  # agy / grok: 24h
  esac
}
quota_cap() {
  case "$1" in
    codex) echo "${CODEX_CAP:-40}" ;;
    agy)   echo "${AGY_CAP:-800}" ;;
    grok)  echo "${GROK_CAP:-150}" ;;
    *)     echo 0 ;;
  esac
}
# Count "call" rows for $1 (cli) with ts within window of now. Reads ledger.jsonl,
# falls back to usage.log if the ledger is missing. Unparseable ts rows are skipped.
count_recent_calls() {
  local cli="$1" window now src
  window="$(quota_window_secs "$cli")"
  now="$(date -u +%s)"
  if [ -s "$LEDGER" ]; then src="$LEDGER"; elif [ -s "$USAGE_LOG" ]; then src="$USAGE_LOG"; else echo 0; return; fi
  awk -v cli="$cli" -F'"' '
    /"type":"verdict"/ { next }
    {
      line=$0; c=""; ts="";
      if (match(line, /"cli":"[^"]*"/)) c=substr(line,RSTART+7,RLENGTH-8);
      if (match(line, /"ts":"[^"]*"/))  ts=substr(line,RSTART+6,RLENGTH-7);
      if (c==cli && ts!="") print ts;
    }' "$src" | while IFS= read -r ts; do
      ep="$(date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null)" || continue
      [ -z "$ep" ] && continue
      if [ $((now - ep)) -le "$window" ]; then echo x; fi
    done | wc -l | tr -d ' '
}

# --- NOTIONAL spend gate (off unless SPEND_CAP set). Workers bill flat
# subscription, so these rates are metered-EQUIVALENT references (USD per 1M
# tokens) for efficiency tracking + an optional ceiling — NOT actual billing.
# See MODEL_REGISTRY.md for the rate table. ---
# model_rate <cli> <model> -> "IN_RATE OUT_RATE" (USD/1M tokens).
model_rate() {
  case "$1" in
    codex)
      case "$2" in
        gpt-5.5|gpt-5.4) echo "1.25 10" ;;
        gpt-5.4-mini|mini) echo "0.25 2" ;;
        *) echo "1.0 5.0" ;;
      esac ;;
    agy)
      case "$2" in
        auto|-|""|flash) echo "0.30 2.50" ;;
        sonnet) echo "3 15" ;;
        opus)   echo "15 75" ;;
        gpt-oss-120b) echo "0.10 0.50" ;;
        *) echo "1.0 5.0" ;;
      esac ;;
    grok)
      case "$2" in
        grok-build|-|""|text) echo "3 15" ;;
        *) echo "1.0 5.0" ;;
      esac ;;
    *) echo "1.0 5.0" ;;
  esac
}
# Flat per-call notional cost for the grok-media lane (image/video).
MEDIA_FLAT_USD="${MEDIA_FLAT_USD:-0.04}"

# est_cost_usd <cli> <model> <in_tokens> <out_chars> -> notional USD (4 dp).
est_cost_usd() {
  local rates ir or in_tok out_tok
  rates="$(model_rate "$1" "$2")"; ir="${rates% *}"; or="${rates#* }"
  in_tok="$3"; out_tok=$(( $4 / 4 ))
  awk -v i="$in_tok" -v o="$out_tok" -v ir="$ir" -v or="$or" \
    'BEGIN { printf "%.4f", i/1e6*ir + o/1e6*or }'
}

# Sum NOTIONAL cost across today's (UTC) ledger "call" rows, RECOMPUTING each
# row's cost from its est_in_tokens + out_chars via model_rate (so it works on
# old rows that predate est_cost_usd; media rows use the flat per-call cost).
spend_today() {
  [ -s "$LEDGER" ] || { echo "0.0000"; return; }
  local today; today="$(date -u +%Y-%m-%d)"
  awk -F'"' -v today="$today" -v media_flat="$MEDIA_FLAT_USD" '
    /"type":"call"/ {
      line=$0; ts=""; cli=""; model=""; modality="text"; it=0; oc=0;
      if (match(line, /"ts":"[^"]*"/))           ts=substr(line,RSTART+6,RLENGTH-7);
      if (substr(ts,1,10) != today) next;
      if (match(line, /"cli":"[^"]*"/))           cli=substr(line,RSTART+7,RLENGTH-8);
      if (match(line, /"model":"[^"]*"/))         model=substr(line,RSTART+9,RLENGTH-10);
      if (match(line, /"modality":"[^"]*"/))      modality=substr(line,RSTART+12,RLENGTH-13);
      if (match(line, /"est_in_tokens":[0-9]+/))  it=substr(line,RSTART+16,RLENGTH-16)+0;
      if (match(line, /"out_chars":[0-9]+/))      oc=substr(line,RSTART+12,RLENGTH-12)+0;
      if (modality=="image" || modality=="video") { total += media_flat; next }
      ir=1.0; or=5.0;  # unknown-model fallback
      if (cli=="codex") {
        if (model=="gpt-5.5"||model=="gpt-5.4") { ir=1.25; or=10 }
        else if (model=="gpt-5.4-mini"||model=="mini") { ir=0.25; or=2 }
      } else if (cli=="agy") {
        if (model=="auto"||model=="-"||model==""||model=="flash") { ir=0.30; or=2.50 }
        else if (model=="sonnet") { ir=3; or=15 }
        else if (model=="opus")   { ir=15; or=75 }
        else if (model=="gpt-oss-120b") { ir=0.10; or=0.50 }
      } else if (cli=="grok") {
        if (model=="grok-build"||model=="-"||model==""||model=="text") { ir=3; or=15 }
      }
      total += it/1e6*ir + (oc/4)/1e6*or;
    }
    END { printf "%.4f", total+0 }' "$LEDGER"
}

# =============================================================================
# grok-media lane — REAL media executor (image|video) via the grok imagine tools.
# Invocation:
#   MEDIA_OUT=/abs/out.png MODALITY=image ./delegate.sh grok-media "<prompt>" [to]
#   MEDIA_OUT=/abs/out.mp4 MODALITY=video MEDIA_SRC=/abs/frame.png \
#       ./delegate.sh grok-media "<prompt>" [to]
# Reuses run_with_timeout (watchdog), the quota helpers above, and the SAME
# ledger/usage.log writer path at the bottom of this script (CLI=grok).
# =============================================================================
if [ "$CLI" = "grok-media" ]; then
  MEDIA_PROMPT="$PROMPT"
  MEDIA_MODALITY="${MODALITY:-image}"
  if [ -z "${MEDIA_OUT:-}" ]; then
    echo "delegate.sh: grok-media requires MEDIA_OUT=/abs/target (image:.png/.jpg, video:.mp4)" >&2
    exit 2
  fi
  case "$MEDIA_MODALITY" in
    image|video) ;;
    *) echo "delegate.sh: grok-media MODALITY must be image|video (got '${MEDIA_MODALITY}')" >&2; exit 2 ;;
  esac

  # --- quota gate (identical semantics to the text lanes: grok 24h cap) ---
  GM_CAP="$(quota_cap grok)"
  GM_COUNT="$(count_recent_calls grok)"
  GM_WINH=$(( $(quota_window_secs grok) / 3600 ))
  if [ "$GM_COUNT" -ge "$GM_CAP" ]; then
    if [ "${STRICT_QUOTA:-0}" = "1" ]; then
      echo "delegate.sh: WARN grok near quota: ${GM_COUNT}/${GM_CAP} in ${GM_WINH}h" >&2
      echo "delegate.sh: REFUSE grok over quota (STRICT_QUOTA=1) — no media call made." >&2
      exit 3
    fi
    echo "delegate.sh: WARN grok near quota: ${GM_COUNT}/${GM_CAP} in ${GM_WINH}h" >&2
  fi

  # --- preflight spend gate (notional; identical semantics to the text lane) ---
  if [ -n "${SPEND_CAP:-}" ]; then
    GM_SPENT="$(spend_today)"
    if awk -v s="$GM_SPENT" -v c="$SPEND_CAP" 'BEGIN { exit !(s+0 >= c+0) }'; then
      if [ "${STRICT_SPEND:-0}" = "1" ]; then
        echo "delegate.sh: WARN notional spend at/over cap: \$${GM_SPENT}/\$${SPEND_CAP} today" >&2
        echo "delegate.sh: REFUSE over spend cap (STRICT_SPEND=1) — no media call made." >&2
        exit 3
      fi
      echo "delegate.sh: WARN notional spend at/over cap: \$${GM_SPENT}/\$${SPEND_CAP} today" >&2
    fi
  fi

  # Build the imagine prompt + pick a per-modality default timeout.
  if [ "$MEDIA_MODALITY" = "image" ]; then
    GM_TIMEOUT="${TIMEOUT_SECS:-240}"
    GM_PROMPT="Use the image_gen tool to generate, aspect_ratio ${AR:-16:9}: ${MEDIA_PROMPT}. Save the output to ${MEDIA_OUT} and print the path."
    GM_FIND_GLOB="*.jpg"
  else
    GM_TIMEOUT="${TIMEOUT_SECS:-300}"
    if [ -z "${MEDIA_SRC:-}" ]; then
      echo "delegate.sh: grok-media MODALITY=video requires MEDIA_SRC=/abs/frame.png" >&2
      exit 2
    fi
    GM_PROMPT="Call the image_to_video tool with image ${MEDIA_SRC}, duration ${MEDIA_DUR:-10} seconds, aspect_ratio ${AR:-16:9}. Then print the absolute path of the generated mp4."
    GM_FIND_GLOB="*.mp4"
  fi

  # Ledger bookkeeping (same fields/order as the text path — id printed for verdict).
  ID="$(date +%s)-$$"
  echo "delegate.sh: id=$ID" >&2
  SPEC_HASH="$(printf '%s' "$MEDIA_PROMPT" | shasum | cut -c1-12)"
  EST_IN_TOKENS=$(( ${#MEDIA_PROMPT} / 4 ))
  RESOLVED_MODEL="grok-build"
  START_EPOCH="$(date -u +%s)"

  # Stamp a marker BEFORE the grok call so the find below only sees files this
  # invocation produced. We use a real marker FILE + `find -newer` (portable across
  # BSD/GNU find) rather than `-newermt "@epoch"` whose epoch parsing is flaky on
  # macOS BSD find. NOTE: this assumes grok runs single-threaded (jyotish runs
  # serially); a concurrent grok media call could race and have its newest output
  # picked up here. Acceptable given the serial pipeline.
  GM_MARKER_FILE="$(mktemp "${TMPDIR:-/tmp}/grok-media-mark.XXXXXX")"
  echo "delegate.sh: grok-media ${MEDIA_MODALITY} → ${MEDIA_OUT} (timeout ${GM_TIMEOUT}s)" >&2
  run_with_timeout "$GM_TIMEOUT" "$GROK_BIN" -p "$GM_PROMPT" -m grok-build \
    --always-approve --output-format plain
  GM_RC=$?

  # Output detection: newest matching artifact under ~/.grok/sessions newer than the
  # marker. grok image output is JPEG even when the target is named .png — byte-copy
  # AS-IS (ffmpeg in the jyotish pipeline reads by content; do NOT transcode).
  GM_FOUND=""
  if [ "$GM_RC" -eq 0 ]; then
    GM_FOUND="$(find "$HOME/.grok/sessions" -name "$GM_FIND_GLOB" -newer "$GM_MARKER_FILE" 2>/dev/null \
      | while IFS= read -r f; do printf '%s\t%s\n' "$(stat -f '%m' "$f" 2>/dev/null)" "$f"; done \
      | sort -rn | head -1 | cut -f2-)"
    if [ -n "$GM_FOUND" ] && [ -f "$GM_FOUND" ]; then
      cp "$GM_FOUND" "$MEDIA_OUT" && echo "delegate.sh: grok-media copied ${GM_FOUND} → ${MEDIA_OUT}" >&2
    else
      echo "delegate.sh: grok-media found no ${GM_FIND_GLOB} newer than marker — no output." >&2
      GM_RC=1
    fi
  else
    echo "delegate.sh: grok-media grok call rc=${GM_RC} (timeout=124) — no output." >&2
  fi
  rm -f "$GM_MARKER_FILE" 2>/dev/null

  END_EPOCH="$(date -u +%s)"
  LATENCY_S=$((END_EPOCH - START_EPOCH))
  # out_chars = output file size in bytes (0 if missing/failed).
  if [ -f "$MEDIA_OUT" ]; then
    CHARS="$(wc -c < "$MEDIA_OUT" | tr -d ' ')"
  else
    CHARS=0
  fi
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # usage.log line (same shape as other lanes; quota subcommand depends on it).
  printf '{"ts":"%s","cli":"%s","model":"%s","timeout":%s,"rc":%s,"out_chars":%s}\n' \
    "$TS" "grok" "$RESOLVED_MODEL" "$GM_TIMEOUT" "$GM_RC" "$CHARS" >> "$USAGE_LOG"
  # Notional cost: media is a flat per-call charge (out tokens N/A for image/video).
  GM_COST_USD="$MEDIA_FLAT_USD"
  # Rich ledger "call" row — SAME printf field list as the text path below.
  printf '{"id":"%s","ts":"%s","type":"call","cli":"%s","model":"%s","timeout":%s,"rc":%s,"out_chars":%s,"spec_hash":"%s","est_in_tokens":%s,"latency_s":%s,"fallback_used":%s,"task_type":"%s","size_class":"%s","modality":"%s","est_cost_usd":%s}\n' \
    "$ID" "$TS" "grok" "$RESOLVED_MODEL" "$GM_TIMEOUT" "$GM_RC" "$CHARS" \
    "$SPEC_HASH" "$EST_IN_TOKENS" "$LATENCY_S" "false" \
    "${TASK_TYPE:-media}" "${SIZE_CLASS:-unknown}" "$MEDIA_MODALITY" "$GM_COST_USD" >> "$LEDGER"

  # --- auto-verdict (media lane): SAME contract as the text lane. On an objective
  # FINAL failure append ONE reject row sharing the call id; success is NEVER
  # auto-accepted. 124->timeout, other non-zero->error, rc=0 + no bytes->empty_output. ---
  GM_AUTO_REASON=""
  if [ "$GM_RC" -eq 124 ]; then
    GM_AUTO_REASON="timeout"
  elif [ "$GM_RC" -ne 0 ]; then
    GM_AUTO_REASON="error"
  elif [ -z "$CHARS" ] || [ "$CHARS" -eq 0 ]; then
    GM_AUTO_REASON="empty_output"
  fi
  if [ -n "$GM_AUTO_REASON" ]; then
    printf '{"id":"%s","ts":"%s","type":"verdict","verdict":"reject","verdict_source":"auto","reason":"%s"}\n' \
      "$ID" "$TS" "$GM_AUTO_REASON" >> "$LEDGER"
    echo "delegate.sh: auto-verdict reject (${GM_AUTO_REASON}) for id=${ID}" >&2
    autosync
  fi

  if [ "$GM_RC" -eq 0 ]; then
    echo "$MEDIA_OUT"
  fi
  exit "$GM_RC"
fi

QCAP="$(quota_cap "$CLI")"
QCOUNT="$(count_recent_calls "$CLI")"
QWIN="$(quota_window_secs "$CLI")"; QWINH=$((QWIN/3600))
if [ "$QCOUNT" -ge "$QCAP" ]; then
  if [ "${STRICT_QUOTA:-0}" = "1" ]; then
    echo "delegate.sh: WARN ${CLI} near quota: ${QCOUNT}/${QCAP} in ${QWINH}h" >&2
    echo "delegate.sh: REFUSE ${CLI} over quota (STRICT_QUOTA=1) — no call made." >&2
    exit 3
  fi
  echo "delegate.sh: WARN ${CLI} near quota: ${QCOUNT}/${QCAP} in ${QWINH}h" >&2
fi

# --- preflight spend gate (notional). Off unless SPEND_CAP set. Mirrors the
# quota gate: warn at/over cap; STRICT_SPEND=1 refuses (exit 3) without a call. ---
if [ -n "${SPEND_CAP:-}" ]; then
  SPENT="$(spend_today)"
  if awk -v s="$SPENT" -v c="$SPEND_CAP" 'BEGIN { exit !(s+0 >= c+0) }'; then
    if [ "${STRICT_SPEND:-0}" = "1" ]; then
      echo "delegate.sh: WARN notional spend at/over cap: \$${SPENT}/\$${SPEND_CAP} today" >&2
      echo "delegate.sh: REFUSE over spend cap (STRICT_SPEND=1) — no call made." >&2
      exit 3
    fi
    echo "delegate.sh: WARN notional spend at/over cap: \$${SPENT}/\$${SPEND_CAP} today" >&2
  fi
fi

# --- outcome ledger bookkeeping ---
ID="$(date +%s)-$$"
echo "delegate.sh: id=$ID" >&2
SPEC_HASH="$(printf '%s' "$PROMPT" | shasum | cut -c1-12)"
EST_IN_TOKENS=$(( ${#PROMPT} / 4 ))
FALLBACK_USED=false
START_EPOCH="$(date -u +%s)"

# Execute with one retry. Capture stdout; stderr passes through.
attempt() {
  OUT="$(run_with_timeout "$TIMEOUT_SECS" "${CMD[@]}")"
  RC=$?
}

attempt
# Decide whether to hop to a fallback lane. Two trigger conditions, BOTH gated on
# fallback not yet used and a fallback lane existing:
#   (1) rc == 124               (watchdog deadline; existing behavior), OR
#   (2) rc == 0 BUT effective output is EMPTY. On this machine codex, when TERMed by
#       the watchdog, can exit 0 with empty stdout instead of rc=124 — a hung call
#       that would otherwise be logged as a silent rc=0 success. Close that hole.
# The rc != 0 (and != 124) branch keeps its single same-lane retry, unchanged.
FB_CLI="$(fallback_lane "$CLI")"
if [ "$RC" -eq 124 ] && [ "$FALLBACK_USED" = false ] && [ -n "$FB_CLI" ]; then
  echo "delegate.sh: ${CLI} timed out (rc=124), hopping to fallback lane ${FB_CLI}..." >&2
  CLI="$FB_CLI"
  MODEL_ARG="-"
  FALLBACK_USED=true
  # The fallback lane (agy/grok) carries ~70s cold start; a tiny timeout meant to
  # trip the primary would guarantee the fallback also times out. Give the fallback
  # a sane floor (FALLBACK_TO, default 150s) unless the original timeout is larger.
  if [ "$TIMEOUT_SECS" -lt "${FALLBACK_TO:-150}" ]; then
    TIMEOUT_SECS="${FALLBACK_TO:-150}"
  fi
  build_cmd
  attempt
elif [ "$RC" -eq 0 ] && [ -z "$(effective_out "$OUT")" ] && [ "$FALLBACK_USED" = false ] && [ -n "$FB_CLI" ]; then
  echo "delegate.sh: ${CLI} returned rc=0 but EMPTY output — hopping to fallback ${FB_CLI}" >&2
  CLI="$FB_CLI"
  MODEL_ARG="-"
  FALLBACK_USED=true
  if [ "$TIMEOUT_SECS" -lt "${FALLBACK_TO:-150}" ]; then
    TIMEOUT_SECS="${FALLBACK_TO:-150}"
  fi
  build_cmd
  attempt
elif [ "$RC" -ne 0 ] && [ "$RC" -ne 124 ]; then
  echo "delegate.sh: ${CLI} call rc=${RC}, retrying once..." >&2
  attempt
fi
# Guard against double-hop: after a fallback attempt we do NOT re-hop even if the
# result is still empty — we return whatever we have. out_chars in BOTH usage.log and
# ledger.jsonl already reflect the FINAL OUT (logged below from the final OUT/RC), so
# a still-empty rc=0 surfaces as out_chars=0 in the ledger for the digest to flag.
END_EPOCH="$(date -u +%s)"
LATENCY_S=$((END_EPOCH - START_EPOCH))

# Post-process captured stdout: drop known MCP noise, then (optionally) unwrap fence.
OUT="$(printf '%s' "$OUT" | strip_noise)"
if [ "$CODE_MODE" -eq 1 ]; then
  OUT="$(printf '%s' "$OUT" | strip_fence)"
fi

# Log one JSON line to usage.log (unchanged — the quota subcommand depends on it).
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
CHARS=${#OUT}
printf '{"ts":"%s","cli":"%s","model":"%s","timeout":%s,"rc":%s,"out_chars":%s}\n' \
  "$TS" "$CLI" "$RESOLVED_MODEL" "$TIMEOUT_SECS" "$RC" "$CHARS" >> "$USAGE_LOG"

# Notional cost of this call (recomputed-compatible: see spend_today / MODEL_REGISTRY).
EST_COST_USD="$(est_cost_usd "$CLI" "$RESOLVED_MODEL" "$EST_IN_TOKENS" "$CHARS")"

# Log one richer "call" row to the outcome ledger (cli/model = FINAL lane used).
printf '{"id":"%s","ts":"%s","type":"call","cli":"%s","model":"%s","timeout":%s,"rc":%s,"out_chars":%s,"spec_hash":"%s","est_in_tokens":%s,"latency_s":%s,"fallback_used":%s,"task_type":"%s","size_class":"%s","modality":"%s","est_cost_usd":%s}\n' \
  "$ID" "$TS" "$CLI" "$RESOLVED_MODEL" "$TIMEOUT_SECS" "$RC" "$CHARS" \
  "$SPEC_HASH" "$EST_IN_TOKENS" "$LATENCY_S" "$FALLBACK_USED" \
  "${TASK_TYPE:-unknown}" "${SIZE_CLASS:-unknown}" "${MODALITY:-text}" "$EST_COST_USD" >> "$LEDGER"

# --- auto-verdict: on an OBJECTIVE FINAL failure (post-fallback), append ONE
# reject row sharing the call id so the router learns the negative signal with no
# human input. Success is NEVER auto-accepted — it awaits Claude's subjective
# verdict. Reasons: 124->timeout, other non-zero->error, rc=0 + empty output->
# empty_output. A fallback hop that ultimately SUCCEEDED (final rc=0, non-empty)
# yields NO auto-reject. ---
AUTO_VERDICT_REASON=""
if [ "$RC" -eq 124 ]; then
  AUTO_VERDICT_REASON="timeout"
elif [ "$RC" -ne 0 ]; then
  AUTO_VERDICT_REASON="error"
elif [ -z "$CHARS" ] || [ "$CHARS" -eq 0 ]; then
  AUTO_VERDICT_REASON="empty_output"
fi
if [ -n "$AUTO_VERDICT_REASON" ]; then
  printf '{"id":"%s","ts":"%s","type":"verdict","verdict":"reject","verdict_source":"auto","reason":"%s"}\n' \
    "$ID" "$TS" "$AUTO_VERDICT_REASON" >> "$LEDGER"
  echo "delegate.sh: auto-verdict reject (${AUTO_VERDICT_REASON}) for id=${ID}" >&2
  autosync
fi

printf '%s' "$OUT"
[ -n "$OUT" ] && echo
exit "$RC"
