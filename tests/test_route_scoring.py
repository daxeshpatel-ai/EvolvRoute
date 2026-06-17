"""Scoring math: smoothed_success (Bayesian prior + recency weighting) and
score_handler (the 0.45/0.25/0.20/0.05/0.05 weighting). These are tested with
hand-built vectors so they're independent of whichever embedder is loaded."""

from datetime import datetime, timedelta, timezone

import pytest

import route

NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- smoothed_success ----------------------------------------------------

def test_smoothed_success_neutral_with_no_outcomes():
    # Bayesian prior 0.7 with pseudo-count 2 -> 0.7 when n=0.
    assert route.smoothed_success([], NOW) == pytest.approx(0.7)


def test_smoothed_success_all_pass_pulls_above_prior():
    outcomes = [{"quality": 1.0, "created_at": iso(NOW)} for _ in range(4)]
    val = route.smoothed_success(outcomes, NOW)
    # (4*1 + 2*0.7)/(4+2) = 5.4/6 = 0.9
    assert val == pytest.approx(0.9)
    assert val > 0.7


def test_smoothed_success_all_fail_pulls_below_prior():
    outcomes = [{"quality": 0.0, "created_at": iso(NOW)} for _ in range(4)]
    # (0 + 1.4)/(4+2) = 0.2333...
    assert route.smoothed_success(outcomes, NOW) == pytest.approx(1.4 / 6)


def test_smoothed_success_threshold_is_0_7():
    # quality >= 0.7 counts as success; just-below does not.
    pass_o = [{"quality": 0.7, "created_at": iso(NOW)}]
    fail_o = [{"quality": 0.69, "created_at": iso(NOW)}]
    assert route.smoothed_success(pass_o, NOW) == pytest.approx((1 + 1.4) / 3)
    assert route.smoothed_success(fail_o, NOW) == pytest.approx((0 + 1.4) / 3)


def test_smoothed_success_old_outcomes_down_weighted():
    old = NOW - timedelta(days=40)
    recent_o = [{"quality": 1.0, "created_at": iso(NOW)}]
    old_o = [{"quality": 1.0, "created_at": iso(old)}]
    # recent weight 1.0 -> (1 + 1.4)/(1+2)=0.8; old weight 0.5 -> (0.5+1.4)/(0.5+2)=0.76
    assert route.smoothed_success(recent_o, NOW) == pytest.approx(2.4 / 3)
    assert route.smoothed_success(old_o, NOW) == pytest.approx(1.9 / 2.5)


def test_smoothed_success_ignores_none_quality():
    outcomes = [{"quality": None, "created_at": iso(NOW)}]
    assert route.smoothed_success(outcomes, NOW) == pytest.approx(0.7)


# --- score_handler -------------------------------------------------------

def vec(*xs):
    return list(xs)


def make_handler(centroid, cost_band="subscription", latency_band="low"):
    return {
        "id": "h", "owner": "codex", "centroid": centroid,
        "meta": {"cost_band": cost_band, "latency_band": latency_band},
    }


def test_score_handler_weighting_formula():
    # qvec == centroid -> sim 1.0; no outcomes -> outcome term 0, success 0.7.
    qvec = vec(1.0, 0.0, 0.0)
    h = make_handler(vec(1.0, 0.0, 0.0), cost_band="free_quota", latency_band="low")
    score, parts = route.score_handler(h, [], qvec, NOW)
    assert parts["sim"] == pytest.approx(1.0)
    assert parts["outcome"] == 0.0
    assert parts["success"] == pytest.approx(0.7)
    assert parts["cost"] == pytest.approx(1.0)       # free_quota
    assert parts["latency"] == pytest.approx(1.0)    # low
    expected = 0.45 * 1.0 + 0.25 * 0.0 + 0.20 * 0.7 + 0.05 * 1.0 + 0.05 * 1.0
    assert score == pytest.approx(expected)


def test_score_handler_no_centroid_gives_zero_sim():
    h = make_handler(None)
    score, parts = route.score_handler(h, [], vec(1.0, 0.0), NOW)
    assert parts["sim"] == 0.0


def test_score_handler_unknown_bands_default():
    h = make_handler(vec(1.0, 0.0), cost_band="weird", latency_band="weird")
    _, parts = route.score_handler(h, [], vec(1.0, 0.0), NOW)
    assert parts["cost"] == pytest.approx(0.5)    # default cost
    assert parts["latency"] == pytest.approx(0.7)  # default latency


def test_score_handler_cost_band_mapping():
    qvec = vec(1.0, 0.0)
    for band, exp in (("free_quota", 1.0), ("subscription", 0.6), ("claude_tokens", 0.1)):
        h = make_handler(vec(1.0, 0.0), cost_band=band)
        _, parts = route.score_handler(h, [], qvec, NOW)
        assert parts["cost"] == pytest.approx(exp), band


def test_score_handler_outcome_term_uses_quality_weighted_cosine():
    qvec = vec(1.0, 0.0)
    h = make_handler(vec(0.0, 1.0))  # orthogonal centroid -> sim 0
    # one aligned, high-quality outcome dominates the outcome term.
    outcomes = [
        {"vec": vec(1.0, 0.0), "quality": 0.8, "created_at": iso(NOW)},
        {"vec": vec(0.0, 1.0), "quality": 1.0, "created_at": iso(NOW)},
    ]
    _, parts = route.score_handler(h, outcomes, qvec, NOW)
    # max(cos(q,[1,0])*0.8, cos(q,[0,1])*1.0) = max(0.8, 0.0) = 0.8
    assert parts["outcome"] == pytest.approx(0.8)


def test_score_handler_outcome_term_skips_none_vectors():
    qvec = vec(1.0, 0.0)
    h = make_handler(vec(1.0, 0.0))
    outcomes = [{"vec": None, "quality": 1.0, "created_at": iso(NOW)}]
    _, parts = route.score_handler(h, outcomes, qvec, NOW)
    assert parts["outcome"] == 0.0
