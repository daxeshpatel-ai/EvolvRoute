# EvolvRoute — Model Registry

> **Single source of truth** for the three delegate CLIs Claude can subprocess to.
> **Last verified: 2026-06-11 · Last tuned: 2026-06-11 (pilot) — re-check monthly.**
> Confidence tags: ✅ verified locally · 🟡 vendor docs · 🔴 volatile (changes often)

---

## codex (OpenAI Codex CLI)

- **Binary:** `codex` (on PATH) ✅
- **Version:** `v0.139.0` ✅
- **Auth:** ChatGPT subscription plan (OAuth), **not** API key ✅
- **Quota/reset:** ChatGPT-plan **5-hour rolling window** 🟡 (shared with ChatGPT usage)

### Headless syntax ✅ (provided, confirmed)
```
codex exec --model <model> --sandbox read-only --skip-git-repo-check --color never "<prompt>"
```

### Models
| Model | Context | Best for | Confidence |
|---|---|---|---|
| `gpt-5.5` | ~256K 🟡 | Flagship coding, mid/real feature impl, tests | ✅ default |
| `gpt-5.4` | ~256K 🟡 | General coding, mid tier | 🟡 |
| `gpt-5.4-mini` | ~128K 🟡 | **Cheap bulk** — boilerplate, low-tier coding | ✅ (`mini` shorthand) |

- **Image:** via `gpt-image-1` **API only** — NOT exposed by the core `codex` binary. 🟡
- **Video:** Sora — **RETIRING, do-not-use.** 🔴

---

## agy (Antigravity CLI)

- **Binary:** `~/.local/bin/agy` ✅
- **Version:** `v1.0.7` ✅
- **Auth:** Google account / subscription (OAuth) ✅
- **Quota/reset:** Google free tier **~1000–1500 req/day, 60 RPM** 🟡 — most generous lane; prefer first.

### Headless syntax ✅ (verified locally)
```
agy --print "<prompt>" --print-timeout <dur>     # e.g. 150s
```
- `--print` / `-p` / `--prompt` = run a single prompt non-interactively and print the response (no TUI).
- `--print-timeout` default `5m0s`; we pass an explicit `<secs>s`.
- **Model is auto-selected** by agy — do NOT pin a model in the wrapper. `--model` exists but the harness leaves it to agy.

### Models (`agy models`) ✅
| Model | Context | Best for | Confidence |
|---|---|---|---|
| **Gemini 3.5 Flash** (Low/Medium/High) | **~1M** 🟡 | **Default.** Huge-context whole-repo reads, web-grounded research, bulk text/docs | ✅ default |
| Gemini 3.1 Pro (Low/High) | ~1M 🟡 | Heavier reasoning when auto-selected | ✅ listed |
| Claude Sonnet 4.6 (Thinking) | — | Available via agy routing | ✅ listed |
| Claude Opus 4.6 (Thinking) | — | Available via agy routing | ✅ listed |
| GPT-OSS 120B (Medium) | — | Open-weight option | ✅ listed |

- **Image:** Imagen / "Nano Banana" 🟡 (API/skill path, not core headless flag).
- **Video:** Veo — **API, gated.** 🟡

---

## grok (xAI Grok CLI — "Grok Build")

- **Binary:** `~/.grok/bin/grok` ✅
- **Version:** `0.2.45 (e4661b89c04)` ✅
- **Auth:** SuperGrok subscription (`grok login`, OAuth) ✅ — **beta** 🔴
- **Quota/reset:** SuperGrok beta limits 🔴 (volatile). Imagine ~200 gen/24h combined; ~10–15 real 720p clips/day.

### Headless syntax ✅ (verified locally)
```
grok -p "<prompt>" -m <model> --sandbox read-only --output-format plain
```
- `-p` / `--single <PROMPT>` = single-turn prompt, prints to stdout and exits (true headless).
- `--output-format` ∈ `plain | json | streaming-json` (we use `plain`).
- `-m` / `--model` selects the model.
- `grok agent headless` exists but runs over the **WebSocket relay** — NOT used by this harness.
- Extras (headless-only, available if needed): `--best-of-n <N>`, `--check` (self-verify loop), `--effort low|medium|high|xhigh|max`.

### Models
| Model | Context | Best for | Confidence |
|---|---|---|---|
| `grok-build` | **512K local cache / 256K per docs** 🟡🔴 (flagged both) | **Default.** Real-time / X-data-grounded coding & research | ✅ default |
| `grok-composer-2.5-fast` | ~200K 🟡 | Fast composition lane | 🟡 |

- **Image+Video:** **Grok Imagine = primary gen lane** (Aurora engine).
  - ~200 generations / 24h combined (image+video).
  - ~10–15 real **720p** clips/day; video clips **≤30s**; **throttles at peak hours.**
  - **Mechanism:** Imagine is NOT a CLI flag/subcommand. It's a registered agent skill (`~/.grok/skills/imagine/`) exposing tool calls `image_gen`, `image_edit`, `image_to_video`, `reference_to_video`. Drive it headlessly by prompting the `grok -p` agent to call the tool. Tool params: `prompt` (required), `aspect_ratio` (`1:1`/`16:9`/`9:16`/`4:3`/`3:4`/`auto`); no `n`/`count`. Edits take `image` (path/data-URL). **No text-to-video** — video always starts from an image.
  - **IMAGE — ✅ verified locally (2026-06-11).** Exact working command:
    ```
    cd <output-dir> && ~/.grok/bin/grok -p "Use the image_gen tool to generate <subject>, <aspect ratio>. Save the output to <ABS_PATH>.png and print the path." -m grok-build --always-approve --output-format plain
    ```
    Use macOS-safe timeouts (no `timeout` binary by default — run backgrounded + poll, ~60–120s/gen). `read-only` sandbox would block the save; use `--always-approve` (default permission mode is fine) so the write executes.
  - **Output convention:** the tool writes the raw generation into the **session store** at `~/.grok/sessions/<url-encoded-cwd>/<session-id>/images/N.jpg` (always **JPEG**, even if you name it `.png`). When prompted to "save to <ABS_PATH>", the agent also copies it there. Verified test: `cli-orchestra/pilot/imagine_test.png` = 1024×1024 JPEG, 62781 bytes.
  - **VIDEO — ✅ verified locally (live-tested 2026-06-11, Jyotish clip).** Working command: `~/.grok/bin/grok -p "<prompt telling the agent to call image_to_video on /abs/path/img.png, duration 10 seconds, aspect_ratio 16:9, then print the absolute mp4 path>" -m grok-build --always-approve --output-format plain`. **Output-location convention:** the tool writes into the grok session store at `~/.grok/sessions/<url-encoded-cwd>/<session-id>/videos/N.mp4` (read-only sandbox blocks direct file saves — `--always-approve` is required, and you must copy the newest mp4 out by mtime afterward). **Verified specs:** duration **10.04s** (requested 10s), resolution **1280×720** (16:9), codec **h264 @ 24fps**, **audio: YES** (silent aac track present). One image generation produced a 1280×720 PNG frame-1; quota consumed this run = 1 image + 1 video. Same headless pattern, prompting the agent to call:
    - `image_to_video` (default) — animates an existing image as frame 1. Stage frame 1 with `image_gen`/`image_edit` first; set aspect ratio on the source image.
    - `reference_to_video` — only when a shot needs multiple references / user asks; otherwise compose refs via multi-image `image_edit` then `image_to_video`.
    - Constraints: duration **6s or 10s only** (prefer 6s, round to nearest); **≤30s** total / **720p** per day-quota; ~10–15 clips/day; one clear subject + one simple camera move per shot; assemble multi-shot with `ffmpeg -f concat ... -c copy` (no re-encode). Outputs land in the session store like images. Verify the video tools exist before calling (skill notes they may be unavailable).

---

## Quick reference — default headless calls
| CLI | Default one-shot command |
|---|---|
| codex | `codex exec --model gpt-5.5 --sandbox read-only --skip-git-repo-check --color never "<p>"` |
| agy   | `agy --print "<p>" --print-timeout 150s` (auto-model) |
| grok  | `grok -p "<p>" -m grok-build --sandbox read-only --output-format plain` |

> macOS has **no `timeout` binary** — `delegate.sh` uses a portable watchdog (returns 124 on timeout). ✅

---

## Timeouts & latency — measured (pilot 2026-06-11) ✅

**Watchdog bug fixed 2026-06-11.** The old watchdog used one long `sleep "$secs"`
which inherited the command-substitution pipe fd; even after a fast call finished,
the orphaned sleep held the pipe open so command substitution blocked for the FULL
timeout, then a needless retry ran. Symptom: *every* coding call appeared to burn its
whole first-timeout and "succeed on retry". The watchdog now **polls (1s ticks) and
returns the instant the child exits** — fast calls finish in their real latency, and
a genuine deadline still yields rc=124 + one retry. Verified: a `codex mini` "Reply OK"
call now returns in **~10s** (was ~120s+), rc=0, single clean usage-log line.

### Recommended first-attempt timeouts
| CLI / model | First-attempt timeout | Notes |
|---|---|---|
| codex `gpt-5.4-mini` | **~120s** | Lowest latency; trivial replies ~10s, real work well under 120s. |
| codex `gpt-5.5` | **~300s+** | Flagship coding; real features need the headroom. |
| agy (auto / Gemini Flash) | **~240s** + budget ~**70s cold start** | First call warms a session; size timeout to include cold start. |
| grok `grok-build` | **~240s** + ~**70s cold start** | Higher cold start like agy; not latency-optimal. |

- **codex is the lowest-latency lane** — prefer it for latency-sensitive qualified work.
- **agy/grok carry ~70s cold start** — fine for big/throughput work, poor for quick turnarounds.
- One retry on genuine timeout/non-zero rc is preserved; with the fix it almost never fires spuriously.

### Outcome ledger & quota gate (added 2026-06-12)
- Every call appends a rich row to **`ledger.jsonl`** (id, spec_hash, est_in_tokens, latency_s, fallback_used) alongside the existing `usage.log` line.
- `delegate.sh verdict <id> accept|edit|reject [edit_pct]` records integration outcomes; `python3 digest.py` rolls them into **`DIGEST.md`**.
- Preflight soft caps (`CODEX_CAP`/`AGY_CAP`/`GROK_CAP`, `STRICT_QUOTA=1` to refuse) and rc=124 timeout→fallback hop (codex→agy, agy→codex, grok→agy). See `DELEGATION_RULES.md` §(g). The built-in cap values are **illustrative defaults** tied to one author's subscription tiers — override them to match your own plans via the `*_CAP` env vars.
- **GOTCHA — codex (v0.139.0)** traps SIGTERM → exits **0 with EMPTY stdout** on a watchdog kill (does NOT return rc≥128/124); `delegate.sh`'s empty-output guard is what actually catches a hung codex.
- **GOTCHA — agy** has **NO filesystem read-only flag** (its `--sandbox` only restricts the terminal tool); `delegate.sh` isolates agy in a scratch `$TMPDIR` cwd instead — so agy read-tasks need **absolute paths**.

### Notional spend gate & cost rates (added 2026-06-15)

> **NOTIONAL only.** Workers bill a flat subscription (ChatGPT plan / Google free tier /
> SuperGrok) — they do **not** consume per-token Anthropic credits. These rates are
> **metered-equivalent reference prices** so the harness can track relative efficiency and
> enforce an optional ceiling. They are **NOT** actual billing.

Each call logs `est_cost_usd` to `ledger.jsonl`, computed as
`in_tokens/1e6*IN_RATE + out_tokens/1e6*OUT_RATE` where `in_tokens = est_in_tokens`
(prompt chars/4) and `out_tokens = out_chars/4` (rounded to 4 dp). The grok-media lane
(image/video) uses a flat per-call charge instead of token math.

| Lane / model | IN (USD/1M tok) | OUT (USD/1M tok) |
|---|---|---|
| codex `gpt-5.5` / `gpt-5.4` | 1.25 | 10 |
| codex `gpt-5.4-mini` (`mini`) | 0.25 | 2 |
| agy default / Gemini Flash (`auto`/`-`/`flash`) | 0.30 | 2.50 |
| agy `sonnet` | 3 | 15 |
| agy `opus` | 15 | 75 |
| agy `gpt-oss-120b` | 0.10 | 0.50 |
| grok `grok-build` / text | 3 | 15 |
| grok media (image/video) | flat `MEDIA_FLAT_USD` per call (default **$0.04**), out tokens N/A |
| **unknown model fallback** | 1.0 | 5.0 |

**Gate (preflight, off unless `SPEND_CAP` set).** Mirrors the quota gate. Before a call,
`spend_today()` sums today's (UTC) notional cost across `ledger.jsonl` `call` rows —
**recomputing** each row's cost from its `est_in_tokens` + `out_chars` (so it works on old
rows that predate `est_cost_usd`; media rows use the flat cost). If `SPEND_CAP` (USD) is set
and `spend_today >= SPEND_CAP`, it **warns** to stderr; if `STRICT_SPEND=1` is also set it
**refuses** with **exit 3** (no CLI call), reusing the quota gate's refusal path. Unset
`SPEND_CAP` = disabled (no behavior change).

| Env var | Meaning |
|---|---|
| `SPEND_CAP` | notional USD ceiling for today's cumulative spend; unset = gate disabled |
| `STRICT_SPEND=1` | refuse (exit 3) at/over `SPEND_CAP` instead of warn-only |
| `MEDIA_FLAT_USD` | flat notional cost per grok-media call (default `0.04`) |
