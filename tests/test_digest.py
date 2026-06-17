"""digest.py — the ledger rollup + stop-cell writer. The stop-cell rule is the
one part of the learning loop that feeds back into route.py's hard filter, so
its threshold logic is the priority here. Pure functions only — no file writes."""

import digest


# --- verdict -> quality (must match ingest.py) -------------------------------

def test_quality_of_matches_ingest():
    assert digest.quality_of({"verdict": "accept"}) == 1.0
    assert digest.quality_of({"verdict": "reject"}) == 0.0
    assert digest.quality_of({"verdict": "edit", "edit_pct": 40}) == 0.6
    assert digest.quality_of({"verdict": "edit"}) == 0.7   # no pct -> default
    assert digest.quality_of({"verdict": "bogus"}) is None


def test_pct():
    assert digest.pct(1, 4) == 25.0
    assert digest.pct(5, 0) == 0.0  # divide-by-zero guarded


def test_cutoff_30d():
    assert digest.cutoff_30d("2026-02-15T00:00:00Z") == "2026-01-16T00:00:00Z"
    assert digest.cutoff_30d(None) is None
    assert digest.cutoff_30d("garbage") is None


# --- stop-cell rule: n>=3 AND (reject%>30 OR avg edit_pct>35) ----------------

def make_life(accept=0, edit=0, reject=0, edit_pcts=None):
    c = digest.new_cell()
    c["accept"], c["edit"], c["reject"] = accept, edit, reject
    c["verdicted"] = accept + edit + reject
    c["edit_pcts"] = edit_pcts or []
    return c


def test_stop_flag_fires_on_high_reject():
    # 4 verdicted, 2 rejects -> 50% > 30%, n>=3 -> STOP.
    life = make_life(accept=2, reject=2)
    reason = digest.stop_flag(life)
    assert reason and "reject" in reason


def test_stop_flag_fires_on_high_avg_edit():
    # 3 edits averaging 50% > 35%, n>=3 -> STOP.
    life = make_life(edit=3, edit_pcts=[50, 50, 50])
    reason = digest.stop_flag(life)
    assert reason and "edit" in reason


def test_stop_flag_silent_below_min_sample():
    # only 2 verdicted -> never a stop-cell regardless of reject rate.
    life = make_life(reject=2)
    assert digest.stop_flag(life) is None


def test_stop_flag_silent_on_healthy_cell():
    life = make_life(accept=4, edit=1, edit_pcts=[10])
    assert digest.stop_flag(life) is None


# --- build_cells: join calls<->verdicts into (cli, task_type) accumulators ----

def test_build_cells_groups_and_counts():
    calls = {
        "1": {"cli": "codex", "task_type": "code", "ts": "2026-02-01T00:00:00Z"},
        "2": {"cli": "codex", "task_type": "code", "ts": "2026-02-02T00:00:00Z"},
        "3": {"cli": "agy", "task_type": "doc", "ts": "2026-02-03T00:00:00Z"},
    }
    verdicts = {
        "1": {"verdict": "accept"},
        "2": {"verdict": "reject"},
        # id 3 unverdicted
    }
    cells = digest.build_cells(calls, verdicts, cutoff=None)
    codex_code = cells[("codex", "code")]["life"]
    assert codex_code["calls"] == 2
    assert codex_code["verdicted"] == 2
    assert codex_code["accept"] == 1 and codex_code["reject"] == 1
    agy_doc = cells[("agy", "doc")]["life"]
    assert agy_doc["calls"] == 1 and agy_doc["verdicted"] == 0


def test_build_cells_recent_window_respects_cutoff():
    calls = {
        "old": {"cli": "codex", "task_type": "code", "ts": "2026-01-01T00:00:00Z"},
        "new": {"cli": "codex", "task_type": "code", "ts": "2026-03-01T00:00:00Z"},
    }
    verdicts = {"old": {"verdict": "accept"}, "new": {"verdict": "accept"}}
    cells = digest.build_cells(calls, verdicts, cutoff="2026-02-01T00:00:00Z")
    cell = cells[("codex", "code")]
    assert cell["life"]["calls"] == 2     # both count lifetime
    assert cell["recent"]["calls"] == 1   # only the post-cutoff one is recent
