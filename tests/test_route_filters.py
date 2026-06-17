"""Hard filters — the safety-critical gate. The invariant under test is that
``claude_keep`` is the floor and is NEVER excluded, and that escalation,
modality, quota, and stop-cell gates each remove exactly the right lanes."""

from datetime import datetime, timezone

import route

NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def survivors_for(handlers, **kw):
    kw.setdefault("task_type", "code")
    kw.setdefault("modality", "text")
    kw.setdefault("risk", "low")
    kw.setdefault("now", NOW)
    survivors, excl = route.hard_filters(handlers, **kw)
    return survivors, excl


def test_claude_keep_always_present_baseline(handlers):
    survivors, _ = survivors_for(handlers)
    assert "claude_keep" in survivors


def test_text_task_excludes_grok_media(handlers):
    survivors, excl = survivors_for(handlers, modality="text")
    assert "grok_media" not in survivors
    assert "grok_media" in excl


def test_image_task_keeps_only_media_and_floor(handlers):
    survivors, _ = survivors_for(handlers, modality="image")
    assert "grok_media" in survivors      # has image in output_types
    assert "claude_keep" in survivors     # floor survives every filter
    assert "codex_55" not in survivors    # text-only lane filtered out
    assert "agy_flash" not in survivors


def test_escalation_high_risk_collapses_to_floor(handlers):
    survivors, excl = survivors_for(handlers, risk="high")
    assert survivors == ["claude_keep"]
    # Text lanes are dropped specifically for escalation (grok_media is dropped
    # earlier by the modality gate, which runs first — so it carries that reason).
    assert "escalation" in excl["codex_55"]
    assert "escalation" in excl["agy_flash"]


def test_escalation_task_type_collapses_to_floor(handlers):
    for tt in ("architecture", "review", "planning"):
        survivors, _ = survivors_for(handlers, task_type=tt)
        assert survivors == ["claude_keep"], tt


def test_quota_excludes_owner_at_cap(handlers, write_ledger, monkeypatch):
    # codex cap is 40 in a 5h window; write 40 recent codex calls.
    monkeypatch.setenv("CODEX_CAP", "40")
    rows = [{"type": "call", "cli": "codex", "ts": "2026-06-17T11:00:00Z"}
            for _ in range(40)]
    write_ledger(rows)
    survivors, excl = survivors_for(handlers)
    assert "codex_mini" not in survivors
    assert "codex_55" not in survivors
    assert any("quota" in why for why in excl.values())
    # other owners unaffected
    assert "agy_flash" in survivors
    assert "claude_keep" in survivors


def test_quota_under_cap_keeps_owner(handlers, write_ledger):
    rows = [{"type": "call", "cli": "codex", "ts": "2026-06-17T11:00:00Z"}
            for _ in range(5)]
    write_ledger(rows)
    survivors, _ = survivors_for(handlers)
    assert "codex_55" in survivors


def test_stop_cell_excludes_pair(handlers, monkeypatch, tmp_path):
    import json
    p = tmp_path / "stopcells.json"
    p.write_text(json.dumps([{"handler_id": "codex_mini", "task_type": "code"}]))
    monkeypatch.setattr(route, "STOPCELLS", str(p))
    survivors, excl = survivors_for(handlers, task_type="code")
    assert "codex_mini" not in survivors
    assert "stop-cell" in excl["codex_mini"]
    # same handler is fine for a different task_type
    survivors2, _ = survivors_for(handlers, task_type="doc")
    assert "codex_mini" in survivors2


def test_claude_keep_never_excluded_even_stopcelled(handlers, monkeypatch, tmp_path):
    # Even a malformed stop-cell naming the floor must not remove it.
    import json
    p = tmp_path / "stopcells.json"
    p.write_text(json.dumps([{"handler_id": "claude_keep", "task_type": "code"}]))
    monkeypatch.setattr(route, "STOPCELLS", str(p))
    survivors, _ = survivors_for(handlers, task_type="code")
    assert "claude_keep" in survivors
