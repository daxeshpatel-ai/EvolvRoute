"""The extracted decide() entrypoint — the in-process routing decision used by
both main() and the benchmark. Exercises the full pipeline (filters + policy +
scoring + relay) against the real handler cards, with no DB side effects."""

import route


def test_decide_returns_contract(built_con):
    d = route.decide(built_con, "implement a feature " * 80,
                     task_type="code", size_class="medium")
    assert d["chosen"]
    assert d["mode"] in ("single", "relay")
    assert d["reasons"]
    assert "applied" in d["policy"]


def test_decide_architecture_escalates(built_con):
    d = route.decide(built_con, "design the system " * 80,
                     task_type="architecture", size_class="medium")
    assert d["chosen"] == "claude_keep"


def test_decide_high_risk_escalates(built_con):
    d = route.decide(built_con, "touch the security path " * 80,
                     task_type="code", size_class="medium", risk="high")
    assert d["chosen"] == "claude_keep"


def test_decide_large_code_relays(built_con):
    d = route.decide(built_con, "large multi-file change " * 80,
                     task_type="code", size_class="large")
    assert d["mode"] == "relay"
    assert d["relay"]["stage1"] == "agy_relay"
    assert d["relay"]["stage2"].startswith("codex")


def test_decide_has_no_db_side_effects(built_con):
    before = built_con.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    route.decide(built_con, "a delegatable task " * 80, task_type="doc",
                 size_class="medium")
    after = built_con.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    assert before == after  # decide() must not log; that's main()'s job


def test_decide_image_keeps_media_lane(built_con):
    d = route.decide(built_con, "draw a routing diagram " * 80,
                     task_type="media", size_class="medium", modality="image")
    assert d["chosen"] in ("grok_media", "claude_keep")
    assert any(r.startswith("grok_media: score=") for r in d["reasons"])
