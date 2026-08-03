#!/usr/bin/env python3
"""A durable record of every identification we have paid for, keyed by image content.

`observations.json` already prevents re-identifying a photo, but only while that
record exists. Reset the file, re-ingest the same photo from another folder, or
`remove --batch` and re-add it, and the identification is gone — the next run pays
for it again. The content hash doesn't change under any of that, so keying on it
means a photo is bought once and only once, for the life of the project.

SQLite rather than JSON because this is a keyed lookup, it wants to be queryable
("what have we spent?", "which photos were identified by the old model?"), and it
should not be rewritten wholesale on every save.

It is committed to git deliberately. A gitignored cache is empty on a fresh clone
or a second machine, which is exactly when re-paying hurts.
"""
import json, pathlib, sqlite3, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "identifications.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS identifications (
    hash          TEXT PRIMARY KEY,   -- content hash of the photo; the real identity
    file          TEXT,               -- filename at the time, for humans
    result_json   TEXT NOT NULL,      -- the model's parsed response, verbatim
    model         TEXT,
    region        TEXT,
    species_id    TEXT,               -- what the result resolved to, so replay is idempotent
    identified_at TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL
);
CREATE INDEX IF NOT EXISTS idx_identified_at ON identifications(identified_at);

-- Submitted batches, so an interrupted poll can be resumed instead of resubmitted.
-- A batch can take up to 24 hours; losing the id would mean paying for it twice
-- and never collecting the first run's results.
-- Drive file ids already fetched. Separate from the photo content hash: the hash
-- stops the same picture being added twice, this stops us re-downloading bytes.
CREATE TABLE IF NOT EXISTS drive_files (
    file_id       TEXT PRIMARY KEY,
    name          TEXT,
    downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    created_at TEXT,
    n_requests INTEGER,
    model      TEXT,
    region     TEXT,
    collected  INTEGER DEFAULT 0
);
"""

# $ per token, for the ledger. Prices as of 2026-08; see scripts/prices.json to override.
PRICES = {
    "claude-opus-5":    (5.00 / 1e6, 25.00 / 1e6),
    "claude-opus-4-8":  (5.00 / 1e6, 25.00 / 1e6),
    "claude-sonnet-5":  (3.00 / 1e6, 15.00 / 1e6),
    "claude-haiku-4-5": (1.00 / 1e6,  5.00 / 1e6),
}


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def get(con, hash_):
    """Return {"result": ..., "species_id": ...} for a paid-for photo, or None."""
    if not hash_:
        return None
    row = con.execute("SELECT result_json, species_id FROM identifications WHERE hash = ?",
                      (hash_,)).fetchone()
    return {"result": json.loads(row[0]), "species_id": row[1]} if row else None


def put(con, hash_, file, result, model, region, usage=None, species_id=None):
    if not hash_:
        return
    cin, cout = PRICES.get(model, (0.0, 0.0))
    it = getattr(usage, "input_tokens", 0) or 0
    ot = getattr(usage, "output_tokens", 0) or 0
    con.execute(
        "INSERT OR REPLACE INTO identifications "
        "(hash, file, result_json, model, region, species_id, identified_at, "
        " input_tokens, output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (hash_, file, json.dumps(result), model, region, species_id,
         datetime.date.today().isoformat(), it, ot, it * cin + ot * cout))
    con.commit()


def stats(con):
    row = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "COALESCE(SUM(cost_usd),0), MIN(identified_at), MAX(identified_at) FROM identifications"
    ).fetchone()
    models = con.execute(
        "SELECT model, COUNT(*), COALESCE(SUM(cost_usd),0) FROM identifications GROUP BY model"
    ).fetchall()
    return {"count": row[0], "input_tokens": row[1], "output_tokens": row[2],
            "cost_usd": row[3], "first": row[4], "last": row[5], "by_model": models}


def record_batch(con, batch_id, n, model, region):
    con.execute("INSERT OR REPLACE INTO batches (batch_id, created_at, n_requests, model, region, collected)"
                " VALUES (?,?,?,?,?,0)",
                (batch_id, datetime.datetime.now().isoformat(timespec="seconds"), n, model, region))
    con.commit()


def open_batches(con):
    return con.execute("SELECT batch_id, created_at, n_requests, model, region FROM batches "
                       "WHERE collected = 0 ORDER BY created_at").fetchall()


def mark_collected(con, batch_id):
    con.execute("UPDATE batches SET collected = 1 WHERE batch_id = ?", (batch_id,))
    con.commit()


def drive_seen(con):
    return {r[0] for r in con.execute("SELECT file_id FROM drive_files")}


def drive_record(con, file_id, name):
    con.execute("INSERT OR REPLACE INTO drive_files (file_id, name, downloaded_at) VALUES (?,?,?)",
                (file_id, name, datetime.datetime.now().isoformat(timespec="seconds")))
    con.commit()
