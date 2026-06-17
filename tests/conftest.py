"""Shared fixtures for the EvolvRoute test suite.

The router and data layer live in ``router/``; tests import them as modules
(``ingest``, ``route``). Importing ``ingest`` selects an embedder at import
time — in CI/test we deliberately run WITHOUT model2vec installed, so it
falls back to the deterministic ``hashed-bow-256`` embedder. That keeps every
test hermetic and offline: no HuggingFace download, no network, stable vectors.

Fixtures here build a throwaway SQLite DB from the real ``handlers.json`` and
point the path constants at tmp files, so no test touches the developer's real
``orchestra.db`` / ``ledger.jsonl``.
"""

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTER_DIR = os.path.join(REPO_ROOT, "router")
sys.path.insert(0, ROUTER_DIR)

import ingest  # noqa: E402
import route  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point ingest at a fresh tmp DB; return the path. No outcomes/ledger."""
    db_path = str(tmp_path / "orchestra.db")
    monkeypatch.setattr(ingest, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def built_con(tmp_db):
    """A live connection to a DB seeded with the real handler cards + centroids.

    This exercises the genuine ``handlers.json`` so filter/scoring tests run
    against the same lanes the router ships with.
    """
    con = ingest.connect()
    ingest.sync_handlers(con)
    ingest.sync_centroids(con)
    con.commit()
    yield con
    con.close()


@pytest.fixture
def handlers(built_con):
    """The handlers dict as ``route.load_handlers`` produces it."""
    return route.load_handlers(built_con)


@pytest.fixture
def write_ledger(tmp_path, monkeypatch):
    """Return a writer that materializes ledger rows to a tmp file and points
    both ``route.LEDGER`` and ``ingest.LEDGER`` at it."""

    ledger_path = tmp_path / "ledger.jsonl"

    def _write(rows):
        with open(ledger_path, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        monkeypatch.setattr(route, "LEDGER", str(ledger_path))
        monkeypatch.setattr(ingest, "LEDGER", str(ledger_path))
        return str(ledger_path)

    return _write
