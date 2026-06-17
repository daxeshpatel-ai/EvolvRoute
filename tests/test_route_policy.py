"""Stateful policy filter: break-even floor, escalate-after-failure, lane
cooldown. claude_keep is never excluded by any rule."""

from datetime import datetime, timezone

import route

NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
SURVIVORS = ["codex_mini", "codex_55", "agy_flash", "claude_keep"]

# Policies that disable the break-even floor so the ledger-driven rules can be
# tested in isolation (break-even has its own dedicated tests below).
NO_FLOOR = {
    "recent_window_secs": 900,
    "escalate_after_failure": True,
    "escalate_task_types": ["code", "refactor", "test"],
    "lane_cooldown_errors": 2,
    "min_delegate_tokens": 0,
}


def call_row(cli, ts="2026-06-17T11:58:00Z", rc=0, out_chars=100, **extra):
    row = {"type": "call", "cli": cli, "ts": ts, "rc": rc, "out_chars": out_chars}
    row.update(extra)
    return row


def run(survivors, task_type, policies, now=NOW, task_text="x" * 8000,
        modality="text"):
    return route.stateful_policy_filter(
        {}, list(survivors), task_type, policies, now,
        task_text=task_text, modality=modality)


# --- break-even floor (deterministic, ledger-independent) ----------------

def test_break_even_floors_tiny_text_task(monkeypatch):
    monkeypatch.delenv("MIN_DELEGATE_TOKENS", raising=False)
    pol = {"min_delegate_tokens": 1000, "recent_window_secs": 900}
    kept, applied = run(SURVIVORS, "code", pol, task_text="tiny task")
    assert kept == ["claude_keep"]
    assert applied and applied[0]["rule"] == "break_even"


def test_break_even_exempts_media(monkeypatch, write_ledger):
    monkeypatch.delenv("MIN_DELEGATE_TOKENS", raising=False)
    write_ledger([])  # no recent rows -> survivors pass through unchanged
    pol = {"min_delegate_tokens": 1000, "recent_window_secs": 900}
    kept, applied = run(SURVIVORS, "media", pol, task_text="draw a cat",
                        modality="image")
    assert kept == SURVIVORS
    assert all(a["rule"] != "break_even" for a in applied)


def test_break_even_env_override(monkeypatch):
    monkeypatch.setenv("MIN_DELEGATE_TOKENS", "0")  # disable floor
    write = {"min_delegate_tokens": 1000, "recent_window_secs": 900}
    # ledger absent -> no recent rows; with floor disabled survivors pass through
    monkeypatch.setattr(route, "LEDGER", "/nonexistent/ledger.jsonl")
    kept, applied = run(SURVIVORS, "code", write, task_text="tiny")
    assert kept == SURVIVORS


def test_break_even_exempts_declared_medium_large(monkeypatch, write_ledger):
    # An explicit size_class of medium/large means the caller already declared
    # the task non-trivial, so a terse prompt must NOT be floored.
    monkeypatch.delenv("MIN_DELEGATE_TOKENS", raising=False)
    write_ledger([])  # no recent rows -> survivors pass through
    pol = {"min_delegate_tokens": 1000, "recent_window_secs": 900}
    for sc in ("medium", "large"):
        kept, applied = route.stateful_policy_filter(
            {}, list(SURVIVORS), "code", pol, NOW,
            task_text="terse spec", modality="text", size_class=sc)
        assert kept == SURVIVORS, sc
        assert all(a["rule"] != "break_even" for a in applied), sc


def test_break_even_still_floors_small_and_unknown(monkeypatch):
    monkeypatch.delenv("MIN_DELEGATE_TOKENS", raising=False)
    pol = {"min_delegate_tokens": 1000, "recent_window_secs": 900}
    for sc in ("small", "unknown"):
        kept, applied = route.stateful_policy_filter(
            {}, list(SURVIVORS), "code", pol, NOW,
            task_text="tiny", modality="text", size_class=sc)
        assert kept == ["claude_keep"], sc
        assert any(a["rule"] == "break_even" for a in applied), sc


# --- escalate-after-failure ---------------------------------------------

def test_escalate_after_failure_on_risky_task(write_ledger):
    write_ledger([call_row("codex", rc=1)])  # a recent error
    kept, applied = run(SURVIVORS, "code", NO_FLOOR)
    assert kept == ["claude_keep"]
    assert any(a["rule"] == "escalate_after_failure" for a in applied)


def test_escalate_after_failure_ignores_nonrisky_task(write_ledger):
    write_ledger([call_row("codex", rc=1)])
    kept, applied = run(SURVIVORS, "doc", NO_FLOOR)  # doc not in escalate list
    assert any(a["rule"] == "escalate_after_failure" for a in applied) is False
    assert "claude_keep" in kept and "codex_55" in kept  # not collapsed by rule A


def test_escalate_detects_empty_output_as_failure(write_ledger):
    # rc==0 but out_chars==0 is the codex SIGTERM-trap signature -> failure.
    write_ledger([call_row("codex", rc=0, out_chars=0)])
    kept, _ = run(SURVIVORS, "code", NO_FLOOR)
    assert kept == ["claude_keep"]


def test_escalate_detects_fallback_used(write_ledger):
    write_ledger([call_row("codex", rc=0, out_chars=100, fallback_used=True)])
    kept, _ = run(SURVIVORS, "code", NO_FLOOR)
    assert kept == ["claude_keep"]


# --- lane cooldown -------------------------------------------------------

def test_lane_cooldown_excludes_noisy_owner(write_ledger):
    # two codex errors on a NON-risky task_type (so rule A doesn't fire first).
    write_ledger([call_row("codex", rc=1), call_row("codex", rc=1)])
    kept, applied = run(SURVIVORS, "doc", NO_FLOOR)
    assert "codex_mini" not in kept
    assert "codex_55" not in kept
    assert "agy_flash" in kept
    assert "claude_keep" in kept            # floor preserved
    assert any(a["rule"] == "lane_cooldown" for a in applied)


def test_lane_cooldown_threshold_not_met(write_ledger):
    write_ledger([call_row("codex", rc=1)])  # only one error, threshold is 2
    kept, applied = run(SURVIVORS, "doc", NO_FLOOR)
    assert kept == SURVIVORS
    assert all(a["rule"] != "lane_cooldown" for a in applied)


def test_no_recent_rows_passes_through(write_ledger):
    write_ledger([])
    kept, applied = run(SURVIVORS, "code", NO_FLOOR)
    assert kept == SURVIVORS
    assert applied == []
