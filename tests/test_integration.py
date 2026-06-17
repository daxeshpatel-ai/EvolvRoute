"""End-to-end: build a real routing DB from handlers.json and drive route.py
as a subprocess, asserting the decision contract (chosen lane, mode, relay).

Runs in a hermetic tmp copy of router/ so the developer's real orchestra.db
and ledger are never touched. With model2vec absent the embedder is the
deterministic hashed-BoW fallback, so these assertions are stable offline.
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def routed(tmp_path):
    """A tmp repo with router/ copied in and the DB synced. Returns a callable
    that runs route.py --json and parses the result."""
    router_dst = tmp_path / "router"
    router_dst.mkdir()
    for name in ("ingest.py", "route.py", "handlers.json", "lane_contract.py"):
        shutil.copy(os.path.join(REPO_ROOT, "router", name), router_dst / name)

    env = dict(os.environ)
    env.pop("MIN_DELEGATE_TOKENS", None)

    # Build the DB (handlers + centroids; no ledger present -> 0 outcomes).
    sync = subprocess.run(
        [sys.executable, str(router_dst / "ingest.py"), "sync"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert sync.returncode == 0, sync.stderr

    def _route(task, **flags):
        cmd = [sys.executable, str(router_dst / "route.py"), task, "--json"]
        for k, v in flags.items():
            cmd += ["--" + k.replace("_", "-"), str(v)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=str(tmp_path))
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return _route


LONG = "implement the feature described here in detail. " * 200  # > break-even


def test_routes_and_emits_contract(routed):
    out = routed(LONG, task_type="code", size_class="medium")
    assert out["chosen"]  # some lane chosen
    assert out["mode"] in ("single", "relay")
    assert "reasons" in out and out["reasons"]


def test_architecture_escalates_to_claude_keep(routed):
    out = routed(LONG, task_type="architecture", size_class="medium")
    assert out["chosen"] == "claude_keep"


def test_high_risk_escalates_to_claude_keep(routed):
    out = routed(LONG, task_type="code", size_class="medium", risk="high")
    assert out["chosen"] == "claude_keep"


def test_large_code_task_triggers_relay(routed):
    out = routed(LONG, task_type="code", size_class="large")
    assert out["mode"] == "relay"
    assert out["relay"]["stage1"] == "agy_relay"
    assert out["relay"]["stage2"].startswith("codex")
    assert out["relay"]["stage2"] != "codex_mini"  # flagship lane only


def test_tiny_task_floored_to_claude_keep(routed):
    out = routed("fix typo", task_type="code", size_class="small")
    assert out["chosen"] == "claude_keep"
    assert "break_even" in out["policy"]["applied"]


def test_image_task_routes_to_media_lane(routed):
    out = routed(LONG, task_type="media", size_class="medium", modality="image")
    # On an image task only grok_media and the claude_keep floor survive the
    # modality gate; which of the two wins is embedder-dependent, but the media
    # lane must survive (be scored, not excluded) and text lanes must be gone.
    assert out["chosen"] in ("grok_media", "claude_keep")
    assert any(r.startswith("grok_media: score=") for r in out["reasons"])
    assert any("codex_55: excluded" in r and "modality" in r for r in out["reasons"])
