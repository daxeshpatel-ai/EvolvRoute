"""Data layer (ingest.py): vector math, ledger->outcome mapping, and the
verdict->quality_score conversion that feeds the learning loop."""

import pytest

import ingest


# --- vector math ---------------------------------------------------------

def test_cosine_identical_is_one():
    assert ingest.cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert ingest.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert ingest.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_l2_normalize_unit_length():
    out = ingest._l2_normalize([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])
    assert sum(x * x for x in out) == pytest.approx(1.0)


def test_l2_normalize_zero_vector_safe():
    assert ingest._l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_embed_is_deterministic_and_normalized():
    a = ingest.embed("write a parser for ISO-8601 durations")
    b = ingest.embed("write a parser for ISO-8601 durations")
    assert a == b
    assert len(a) == ingest.DIM
    assert sum(x * x for x in a) == pytest.approx(1.0)


# --- handler-id mapping --------------------------------------------------

@pytest.mark.parametrize("cli,model,modality,expected", [
    ("codex", "gpt-5.4-mini", None, "codex_mini"),
    ("codex", "gpt-5.5", None, "codex_55"),
    ("agy", "gemini", None, "agy_flash"),
    ("grok", "grok-4", "text", "grok_text"),
    ("grok", "grok-imagine", "image", "grok_media"),
    ("grok", "grok-imagine", "video", "grok_media"),
])
def test_map_handler_id(cli, model, modality, expected):
    call = {"cli": cli, "model": model, "modality": modality}
    assert ingest.map_handler_id(call) == expected


# --- error classification -----------------------------------------------

@pytest.mark.parametrize("call,expected", [
    ({"rc": 124, "out_chars": 0}, "timeout"),
    ({"rc": 1, "out_chars": 0}, "error"),
    ({"rc": 0, "out_chars": 0}, "empty_output"),
    ({"rc": 0, "out_chars": 500}, None),
])
def test_error_class_for(call, expected):
    assert ingest.error_class_for(call) == expected


# --- verdict -> quality_score (the learning signal) ----------------------

def _ingest_quality(monkeypatch, tmp_path, verdict_row):
    """Run sync over a one-call ledger and return the stored quality_score."""
    import json
    ledger = tmp_path / "ledger.jsonl"
    call = {"type": "call", "id": "t1", "cli": "codex", "model": "gpt-5.5",
            "task_type": "code", "ts": "2026-06-17T11:00:00Z", "out_chars": 200}
    with open(ledger, "w") as fh:
        fh.write(json.dumps(call) + "\n")
        fh.write(json.dumps(verdict_row) + "\n")
    monkeypatch.setattr(ingest, "LEDGER", str(ledger))
    monkeypatch.setattr(ingest, "DB_PATH", str(tmp_path / "orchestra.db"))
    con = ingest.connect()
    ingest.sync_handlers(con)
    ingest.sync_outcomes(con)
    con.commit()
    row = con.execute(
        "SELECT quality_score, handler_id FROM outcomes WHERE id='t1'"
    ).fetchone()
    con.close()
    return row


def test_accept_maps_to_quality_1(monkeypatch, tmp_path):
    q, hid = _ingest_quality(monkeypatch, tmp_path,
                             {"type": "verdict", "id": "t1", "verdict": "accept"})
    assert q == pytest.approx(1.0)
    assert hid == "codex_55"


def test_reject_maps_to_quality_0(monkeypatch, tmp_path):
    q, _ = _ingest_quality(monkeypatch, tmp_path,
                           {"type": "verdict", "id": "t1", "verdict": "reject"})
    assert q == pytest.approx(0.0)


def test_edit_with_pct(monkeypatch, tmp_path):
    q, _ = _ingest_quality(monkeypatch, tmp_path,
                           {"type": "verdict", "id": "t1", "verdict": "edit",
                            "edit_pct": 30})
    assert q == pytest.approx(0.7)  # 1.0 - 30/100


def test_edit_without_pct_defaults(monkeypatch, tmp_path):
    q, _ = _ingest_quality(monkeypatch, tmp_path,
                           {"type": "verdict", "id": "t1", "verdict": "edit"})
    assert q == pytest.approx(0.7)
