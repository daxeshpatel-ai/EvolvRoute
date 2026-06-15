# Security Policy

EvolvRoute is alpha software maintained on a best-effort basis. Security reports are taken
seriously and handled as promptly as a single maintainer can manage.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue, and do
not disclose details publicly until a fix is available.

- Email: **dhpatel@gmail.com**
- Include: a description of the issue, the affected file or component, steps to reproduce,
  and the impact you observed.

You can expect an acknowledgement within a few days. There is no bug-bounty program.

## Scope

In scope:

- The harness scripts — `delegate.sh`, the router modules under `router/`, and `digest.py`.
- The handling of operational data (`ledger.jsonl`, `usage.log`) and how delegation prompts
  are passed to worker CLIs.

Out of scope:

- Vulnerabilities in the worker CLIs themselves (`codex`, `agy`, `grok`, or any tool you
  point a lane at). Report those to their respective maintainers.
- Issues that require an attacker to already control your shell, environment, or the worker
  CLI's session store.

## Data hygiene

EvolvRoute stores **no secrets or API keys**. Each worker CLI authenticates with its own
session and credentials; EvolvRoute only shells out to those tools. The ledger stores a
`spec_hash` of each task, never the prompt text, and real `ledger.jsonl` / `usage.log` files
are gitignored — only synthetic `*.example` seeds are tracked.

Do not put secrets in task prompts: they are passed to the worker CLI and may be retained by
that tool's own session store. See the [Security & data hygiene](README.md#security--data-hygiene)
section of the README for the full guidance.
