"""report.py — the ledger spend/savings dashboard. Cost recomputation must
match delegate.sh, and the savings-vs-frontier math must be correct."""

import os

import pytest

import report

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(cli, model, in_tok=0, out_chars=0, modality="text", **extra):
    row = {"type": "call", "cli": cli, "model": model, "est_in_tokens": in_tok,
           "out_chars": out_chars, "modality": modality}
    row.update(extra)
    return row


# --- cost model parity with delegate.sh -------------------------------------

@pytest.mark.parametrize("cli,model,expect", [
    ("codex", "gpt-5.5", (1.25, 10.0)),
    ("codex", "gpt-5.4-mini", (0.25, 2.0)),
    ("codex", "weird", (1.0, 5.0)),
    ("agy", "auto", (0.30, 2.50)),
    ("agy", "opus", (15.0, 75.0)),
    ("grok", "grok-build", (3.0, 15.0)),
    ("unknown", "x", (1.0, 5.0)),
])
def test_model_rate(cli, model, expect):
    assert report.model_rate(cli, model) == expect


def test_call_cost_token_math():
    # in=1,000,000 tok @1.25 = $1.25; out=4,000,000 chars -> 1M tok @10 = $10.
    c = call("codex", "gpt-5.5", in_tok=1_000_000, out_chars=4_000_000)
    assert report.call_cost(c) == pytest.approx(11.25)


def test_call_cost_media_is_flat():
    c = call("grok", "grok-build", in_tok=10, out_chars=999999, modality="image")
    assert report.call_cost(c) == pytest.approx(report.MEDIA_FLAT)


def test_frontier_cost_uses_opus_rate():
    c = call("codex", "gpt-5.4-mini", in_tok=1_000_000, out_chars=4_000_000)
    # frontier prices the SAME tokens at 15/75 regardless of the lane used.
    assert report.frontier_cost(c) == pytest.approx(15.0 + 75.0)


# --- summarize --------------------------------------------------------------

def test_summarize_savings_and_coverage():
    calls = {
        "1": call("codex", "gpt-5.4-mini", in_tok=1000, out_chars=4000, latency_s=5),
        "2": call("agy", "auto", in_tok=2000, out_chars=8000, latency_s=100,
                  fallback_used=True),
    }
    verdicts = {"1": {"verdict": "accept"}}  # id 2 unverdicted
    s = report.summarize(calls, verdicts)
    assert s["n_calls"] == 2
    assert s["spent"] == pytest.approx(report.call_cost(calls["1"])
                                       + report.call_cost(calls["2"]))
    assert s["frontier_equiv"] > s["spent"]
    assert s["savings"] == pytest.approx(s["frontier_equiv"] - s["spent"])
    assert s["verdict_cov"] == pytest.approx(50.0)
    assert s["accept"] == 1 and s["pending"] == 1
    assert s["by_lane"]["agy"]["fallbacks"] == 1


def test_summarize_empty():
    s = report.summarize({}, {})
    assert s["n_calls"] == 0
    assert s["spent"] == 0.0
    assert s["savings_pct"] == 0.0


def test_load_and_summarize_example_ledger():
    calls, verdicts = report.load_ledger(
        os.path.join(REPO_ROOT, "ledger.jsonl.example"))
    s = report.summarize(calls, verdicts)
    assert s["n_calls"] == 10
    assert s["savings"] > 0           # delegation saved money vs frontier
    assert s["verdict_cov"] == pytest.approx(100.0)
