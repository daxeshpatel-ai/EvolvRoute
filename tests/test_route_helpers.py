"""Helpers and file readers in route.py: timestamp parsing, ledger windows,
stop-cell loading, policy loading."""

import json
from datetime import datetime, timezone

import route


def ts(s):
    return s  # readability alias for ISO strings


def test_parse_ts_valid():
    got = route.parse_ts("2026-06-17T12:00:00Z")
    assert got == datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_ts_invalid_returns_none():
    assert route.parse_ts("not-a-date") is None
    assert route.parse_ts(None) is None
    assert route.parse_ts("2026-06-17 12:00:00") is None  # wrong format


def test_count_recent_calls_respects_window(write_ledger):
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    write_ledger([
        {"type": "call", "cli": "codex", "ts": "2026-06-17T11:59:00Z"},  # in 5h window
        {"type": "call", "cli": "codex", "ts": "2026-06-17T08:00:00Z"},  # in 5h window
        {"type": "call", "cli": "codex", "ts": "2026-06-17T06:00:00Z"},  # 6h ago -> out
        {"type": "call", "cli": "agy", "ts": "2026-06-17T11:59:00Z"},   # other owner
        {"type": "verdict", "cli": "codex", "ts": "2026-06-17T11:59:00Z"},  # not a call
    ])
    assert route.count_recent_calls("codex", now) == 2
    assert route.count_recent_calls("agy", now) == 1


def test_count_recent_calls_missing_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(route, "LEDGER", str(tmp_path / "nope.jsonl"))
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert route.count_recent_calls("codex", now) == 0


def test_ledger_now_uses_max_ts(write_ledger):
    write_ledger([
        {"type": "call", "cli": "codex", "ts": "2026-06-17T09:00:00Z"},
        {"type": "call", "cli": "codex", "ts": "2026-06-17T11:30:00Z"},
        {"type": "call", "cli": "codex", "ts": "2026-06-17T10:00:00Z"},
    ])
    assert route.ledger_now() == datetime(2026, 6, 17, 11, 30, 0, tzinfo=timezone.utc)


def test_ledger_now_falls_back_to_wallclock(monkeypatch, tmp_path):
    monkeypatch.setattr(route, "LEDGER", str(tmp_path / "absent.jsonl"))
    before = datetime.now(timezone.utc)
    got = route.ledger_now()
    assert got >= before  # wall-clock fallback when no parseable ledger ts


def test_recent_call_rows_filters_type_and_window(write_ledger):
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    write_ledger([
        {"type": "call", "cli": "codex", "ts": "2026-06-17T11:55:00Z"},
        {"type": "verdict", "cli": "codex", "ts": "2026-06-17T11:55:00Z"},
        {"type": "call", "cli": "agy", "ts": "2026-06-17T11:00:00Z"},  # 60m ago, outside 15m
    ])
    rows = route.recent_call_rows(now, window=900)  # 15 min
    assert len(rows) == 1
    assert rows[0]["cli"] == "codex"


def test_load_stopcells_owner_form(monkeypatch, tmp_path):
    p = tmp_path / "stopcells.json"
    p.write_text(json.dumps([{"handler_owner": "codex", "task_type": "doc"}]))
    monkeypatch.setattr(route, "STOPCELLS", str(p))
    pairs = route.load_stopcells()
    assert ("codex_mini", "doc") in pairs
    assert ("codex_55", "doc") in pairs


def test_load_stopcells_explicit_handler_id(monkeypatch, tmp_path):
    p = tmp_path / "stopcells.json"
    p.write_text(json.dumps([{"handler_id": "agy_flash", "task_type": "code"}]))
    monkeypatch.setattr(route, "STOPCELLS", str(p))
    assert route.load_stopcells() == {("agy_flash", "code")}


def test_load_stopcells_missing_or_bad(monkeypatch, tmp_path):
    monkeypatch.setattr(route, "STOPCELLS", str(tmp_path / "missing.json"))
    assert route.load_stopcells() == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(route, "STOPCELLS", str(bad))
    assert route.load_stopcells() == set()


def test_load_policies_defaults_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(route, "POLICIES", str(tmp_path / "missing.json"))
    pol = route.load_policies()
    assert pol == route.POLICY_DEFAULTS
    assert pol["recent_window_secs"] == 900


def test_load_policies_merges_over_defaults(monkeypatch, tmp_path):
    p = tmp_path / "policies.json"
    p.write_text(json.dumps({"recent_window_secs": 60, "extra": 1}))
    monkeypatch.setattr(route, "POLICIES", str(p))
    pol = route.load_policies()
    assert pol["recent_window_secs"] == 60          # overridden
    assert pol["lane_cooldown_errors"] == 2          # default preserved
    assert pol["extra"] == 1                          # new key merged
