## Summary

What this PR does and why, in a sentence or two.

## Changes

- 

## How tested

Run the pre-PR checks and confirm they pass:

```bash
bash -n delegate.sh                 # the entrypoint still parses
python3 -m py_compile router/*.py   # router modules compile
```

Note anything else you ran (a real `./delegate.sh auto ...` route, a verdict cycle, etc.).

## Checklist

- [ ] Docs updated where behavior changed (README / ARCHITECTURE / relevant `docs/`).
- [ ] No live operational data committed (`ledger.jsonl`, `usage.log`, `DIGEST.md`,
      `router/orchestra.db`, `router/stopcells.json`) — only `*.example` seeds.
- [ ] No secrets in code, prompts, or fixtures.
- [ ] If this adds or changes a lane, it follows [docs/ADDING_A_LANE.md](../docs/ADDING_A_LANE.md).
