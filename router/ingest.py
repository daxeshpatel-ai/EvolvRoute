#!/usr/bin/env python3
"""ingest.py — WP1 data layer for the geometric routing layer.

`python3 ingest.py sync`:
  1. upserts handlers.json cards into the handlers table (+ declared embeddings)
  2. ingests verdicted call rows from ../ledger.jsonl into outcomes (+ embeddings)
  3. recomputes handler centroids (declared + success - fail, confidence-scaled)

Stdlib only. Importable: embed(text) -> list[float], cosine(a, b) -> float.
Known limitation: the ledger stores no prompts, so outcome task_summary is
built from call metadata only (see router/README.md).
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "orchestra.db")
HANDLERS_JSON = os.path.join(SCRIPT_DIR, "handlers.json")
LEDGER = os.path.join(SCRIPT_DIR, "..", "ledger.jsonl")

# Hashed-BoW fallback dimension. The active DIM is set by whichever real
# embedder loads below (model2vec potion-base-8M is also 256-d).
HASHED_BOW_DIM = 256
DIM = HASHED_BOW_DIM

# --- embedder -----------------------------------------------------------
# Embedder priority (first that loads wins). All inference is 100% local —
# no remote API at runtime. Weights are a one-time HuggingFace download,
# cached on disk, after which encoding runs fully offline.
#   1. model2vec (pure-numpy static embeddings, real semantics, no torch)
#   2. sentence-transformers all-MiniLM-L6-v2 (if torch is available)
#   3. hashed bag-of-words TF (deterministic, coarse, last resort)
_M2V_MODEL = None
_ST_MODEL = None
EMBEDDING_MODEL = None
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _hashed_bow_embed(text):
    vec = [0.0] * HASHED_BOW_DIM
    for tok in _TOKEN_RE.findall((text or "").lower()):
        idx = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16) % HASHED_BOW_DIM
        vec[idx] += 1.0
    return _l2_normalize(vec)


try:  # 1. model2vec — real local semantic embeddings, no torch dependency.
    from model2vec import StaticModel  # noqa: F401

    _M2V_MODEL = StaticModel.from_pretrained("minishlab/potion-base-8M")
    EMBEDDING_MODEL = "model2vec:potion-base-8M"
    DIM = int(_M2V_MODEL.encode(["x"]).shape[1])

    def embed(text):
        vec = _M2V_MODEL.encode([text or ""])[0]
        return _l2_normalize([float(x) for x in vec])

except Exception:  # noqa: BLE001 - any load failure falls through
    _M2V_MODEL = None
    try:  # 2. sentence-transformers (only if torch wheels are present).
        from sentence_transformers import SentenceTransformer  # noqa: F401

        _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        EMBEDDING_MODEL = "st:all-MiniLM-L6-v2"
        DIM = int(len(_ST_MODEL.encode("x")))

        def embed(text):
            return _l2_normalize([float(x) for x in _ST_MODEL.encode(text or "")])

    except ImportError:  # 3. hashed bag-of-words — coarse last resort.
        _ST_MODEL = None
        EMBEDDING_MODEL = "hashed-bow-256"
        DIM = HASHED_BOW_DIM
        embed = _hashed_bow_embed


def _l2_normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return list(vec)
    return [x / norm for x in vec]


def cosine(a, b):
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return num / (na * nb)


# --- db -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS handlers(
  id TEXT PRIMARY KEY, owner TEXT, handler_type TEXT,
  capability_text TEXT, metadata_json TEXT, enabled INTEGER,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes(
  id TEXT PRIMARY KEY, task_summary TEXT, handler_id TEXT,
  task_type TEXT, artifact_type TEXT, quality_score REAL,
  tests_passed INTEGER, human_approved INTEGER, cost_tokens INTEGER,
  wall_time_ms INTEGER, error_class TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS invocations(
  id TEXT PRIMARY KEY, task_text TEXT, chosen_handler_id TEXT,
  router_score REAL, status TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS embeddings(
  object_type TEXT, object_id TEXT, embedding_model TEXT,
  vector TEXT, text_hash TEXT,
  PRIMARY KEY(object_type, object_id)
);
"""


def connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


def upsert_embedding(con, object_type, object_id, text=None, vector=None):
    """Embed text (or store a precomputed vector). Skips work when text_hash
    is unchanged. Returns True if the row was written/updated."""
    if vector is None:
        thash = text_hash(text)
        row = con.execute(
            "SELECT text_hash FROM embeddings WHERE object_type=? AND object_id=?",
            (object_type, object_id),
        ).fetchone()
        if row and row[0] == thash:
            return False
        vector = embed(text)
    else:
        thash = text_hash(json.dumps(vector))
        row = con.execute(
            "SELECT text_hash FROM embeddings WHERE object_type=? AND object_id=?",
            (object_type, object_id),
        ).fetchone()
        if row and row[0] == thash:
            return False
    con.execute(
        "INSERT INTO embeddings(object_type, object_id, embedding_model, vector, text_hash)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(object_type, object_id) DO UPDATE SET"
        " embedding_model=excluded.embedding_model, vector=excluded.vector,"
        " text_hash=excluded.text_hash",
        (object_type, object_id, EMBEDDING_MODEL, json.dumps(vector), thash),
    )
    return True


def get_vector(con, object_type, object_id):
    row = con.execute(
        "SELECT vector FROM embeddings WHERE object_type=? AND object_id=?",
        (object_type, object_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


# --- 1. handlers --------------------------------------------------------

def capability_text(card):
    # routing_tags are canonical task-profile tokens (task_type/size_class
    # vocabulary). They are repeated TAG_WEIGHT times so the hashed-BoW
    # similarity gives task-affinity more pull than incidental prose overlap.
    TAG_WEIGHT = 4
    tags = " ".join(card.get("routing_tags", []) * TAG_WEIGHT)
    parts = [
        "id: %s" % card["id"],
        "routing_tags: %s" % tags,
        "strong_at: %s" % "; ".join(card.get("strong_at", [])),
        "weak_at: %s" % "; ".join(card.get("weak_at", [])),
        "input_types: %s" % ", ".join(card.get("input_types", [])),
        "output_types: %s" % ", ".join(card.get("output_types", [])),
        "notes: %s" % card.get("notes", ""),
    ]
    return "\n".join(parts)


def sync_handlers(con):
    with open(HANDLERS_JSON) as fh:
        cards = json.load(fh)["handlers"]
    # Fail fast on a malformed lane card rather than mis-routing later.
    import lane_contract
    errors, _warnings = lane_contract.validate(cards)
    if errors:
        raise ValueError(
            "invalid handlers.json (%d error(s)):\n  %s"
            % (len(errors), "\n  ".join(errors)))
    ts = now_iso()
    n = 0
    for card in cards:
        cap = capability_text(card)
        con.execute(
            "INSERT INTO handlers(id, owner, handler_type, capability_text,"
            " metadata_json, enabled, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,"
            " handler_type=excluded.handler_type,"
            " capability_text=excluded.capability_text,"
            " metadata_json=excluded.metadata_json, enabled=excluded.enabled,"
            " updated_at=excluded.updated_at",
            (
                card["id"], card["owner"], card["handler_type"], cap,
                json.dumps(card), 1 if card.get("enabled", True) else 0, ts, ts,
            ),
        )
        upsert_embedding(con, "handler", card["id"], text=cap)
        n += 1
    return n, [c["id"] for c in cards]


# --- 2. outcomes from ledger --------------------------------------------

def map_handler_id(call):
    """Infer a handlers.json id from a ledger call row's cli+model."""
    cli = call.get("cli", "")
    model = call.get("model", "")
    if cli == "codex":
        return "codex_mini" if "mini" in model else "codex_55"
    if cli == "agy":
        return "agy_flash"
    if cli == "grok":
        # media calls (image/video) belong to the grok_media handler, not grok_text.
        if call.get("modality") in ("image", "video"):
            return "grok_media"
        return "grok_text"
    return cli or "unknown"


def error_class_for(call):
    rc = call.get("rc")
    out_chars = call.get("out_chars")
    if rc == 124:
        return "timeout"
    if rc not in (0, 124) and rc is not None:
        return "error"
    if rc == 0 and out_chars == 0:
        return "empty_output"
    return None


def sync_outcomes(con):
    if not os.path.exists(LEDGER):
        return 0, 0
    calls, verdicts = {}, {}
    with open(LEDGER) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("id")
            if rid is None:
                continue
            if row.get("type") == "call":
                calls[rid] = row  # last call row wins
            elif row.get("type") == "verdict":
                verdicts[rid] = row  # last verdict wins

    new = 0
    total = 0
    for rid, v in verdicts.items():
        call = calls.get(rid)
        if call is None:
            continue
        total += 1
        verdict = v.get("verdict")
        if verdict == "accept":
            quality = 1.0
        elif verdict == "edit":
            pct = v.get("edit_pct")
            quality = 0.7 if pct is None else 1.0 - float(pct) / 100.0
        elif verdict == "reject":
            quality = 0.0
        else:
            continue
        human_approved = 1 if verdict in ("accept", "edit") else 0
        task_type = call.get("task_type") or "unknown"
        artifact_type = call.get("artifact_type") or "unknown"
        summary = (
            "task_type=%s cli=%s model=%s spec_hash=%s out_chars=%s"
            % (
                task_type, call.get("cli", "?"), call.get("model", "?"),
                call.get("spec_hash", "?"), call.get("out_chars", "?"),
            )
        )
        existed = con.execute(
            "SELECT 1 FROM outcomes WHERE id=?", (rid,)
        ).fetchone()
        con.execute(
            "INSERT INTO outcomes(id, task_summary, handler_id, task_type,"
            " artifact_type, quality_score, tests_passed, human_approved,"
            " cost_tokens, wall_time_ms, error_class, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET task_summary=excluded.task_summary,"
            " handler_id=excluded.handler_id, task_type=excluded.task_type,"
            " artifact_type=excluded.artifact_type,"
            " quality_score=excluded.quality_score,"
            " human_approved=excluded.human_approved,"
            " cost_tokens=excluded.cost_tokens,"
            " wall_time_ms=excluded.wall_time_ms,"
            " error_class=excluded.error_class",
            (
                rid, summary, map_handler_id(call), task_type, artifact_type,
                quality, None, human_approved,
                int(call.get("est_in_tokens") or 0),
                int(float(call.get("latency_s") or 0) * 1000),
                error_class_for(call),
                call.get("ts") or now_iso(),
            ),
        )
        upsert_embedding(con, "outcome", rid, text=summary)
        if not existed:
            new += 1
    return new, total


# --- 3. centroids --------------------------------------------------------

def sync_centroids(con):
    handler_ids = [r[0] for r in con.execute("SELECT id FROM handlers")]
    updated = 0
    for hid in handler_ids:
        declared = get_vector(con, "handler", hid)
        if declared is None:
            continue
        dim = len(declared)
        rows = con.execute(
            "SELECT o.id, o.quality_score FROM outcomes o WHERE o.handler_id=?",
            (hid,),
        ).fetchall()
        succ, fail = [], []
        n_outcomes = 0
        for oid, q in rows:
            vec = get_vector(con, "outcome", oid)
            if vec is None or len(vec) != dim:
                continue
            n_outcomes += 1
            if q is not None and q >= 0.7:
                succ.append(vec)
            elif q is not None and q < 0.3:
                fail.append(vec)
        w_d = max(0.5, 1.0 - n_outcomes / 20.0)
        w_s = (1.0 - w_d) * 0.8
        w_f = (1.0 - w_d) * 0.2

        def avg(vectors):
            if not vectors:
                return [0.0] * dim
            return [sum(col) / len(vectors) for col in zip(*vectors)]

        avg_s, avg_f = avg(succ), avg(fail)
        centroid = _l2_normalize(
            [
                w_d * declared[i] + w_s * avg_s[i] - w_f * avg_f[i]
                for i in range(dim)
            ]
        )
        if upsert_embedding(con, "handler_centroid", hid, vector=centroid):
            updated += 1
    return updated, len(handler_ids)


# --- cli ------------------------------------------------------------------

def migrate_embedding_model(con):
    """If the stored embeddings were produced by a different embedder (model
    name and therefore dimension may differ), wipe the embeddings table so the
    sync rebuilds every vector under the current model. This prevents cosine
    ever running over mixed-dimension / mixed-model vectors."""
    rows = con.execute(
        "SELECT DISTINCT embedding_model FROM embeddings"
    ).fetchall()
    stored = {r[0] for r in rows}
    if stored and stored != {EMBEDDING_MODEL}:
        con.execute("DELETE FROM embeddings")
        con.commit()
        return sorted(stored)
    return None


def cmd_sync():
    con = connect()
    try:
        migrated_from = migrate_embedding_model(con)
        n_handlers, _ids = sync_handlers(con)
        new_outcomes, total_verdicted = sync_outcomes(con)
        cent_updated, cent_total = sync_centroids(con)
        con.commit()
    finally:
        con.close()
    print("sync summary (embedding_model=%s, dim=%d):" % (EMBEDDING_MODEL, DIM))
    if migrated_from:
        print(
            "  MIGRATED:            wiped embeddings from %s -> %s (rebuilt all)"
            % (", ".join(migrated_from), EMBEDDING_MODEL)
        )
    print("  handlers upserted:   %d" % n_handlers)
    print(
        "  outcomes ingested:   %d new (%d verdicted calls total in ledger)"
        % (new_outcomes, total_verdicted)
    )
    print("  centroids updated:   %d of %d" % (cent_updated, cent_total))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="upsert handlers, ingest ledger outcomes, recompute centroids")
    args = ap.parse_args()
    if args.cmd == "sync":
        cmd_sync()


if __name__ == "__main__":
    main()
