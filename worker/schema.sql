-- Contributor endpoint storage.
--
-- Two tables and nothing else, because this database holds no survey data. The
-- survey lives in data/observations.json with exactly one writer; everything here
-- is either an identity or a message in transit to that writer. Losing this
-- database entirely would cost the tokens (re-mint them) and any submission not
-- yet collected — never a record, a species or a verification.

-- Who may write, and as what. Only the hash of a token is stored: the plaintext
-- is shown once at minting and never again, so this table is not a key ring even
-- to someone holding it.
--
-- `name` is load-bearing rather than decorative. It is what ends up in
-- `verified.by`, read from this row instead of from anything the browser sent,
-- which is what makes an unattributed verification impossible to construct
-- rather than merely refused after the fact.
CREATE TABLE IF NOT EXISTS contributors (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  role         TEXT NOT NULL CHECK (role IN ('contributor','verifier','admin','pipeline')),
  token_sha256 TEXT NOT NULL UNIQUE,
  created      TEXT NOT NULL,
  last_seen    TEXT,
  active       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS contributors_token ON contributors (token_sha256);

-- The queue, and afterwards the receipt. A row stays here once settled so a
-- contributor can be shown what became of what they submitted — the inbox's
-- version of the steward sheet's `recorded?` column, and there for the same
-- reason: a refusal that exists only in a CI log leaves the person who walked out
-- there believing it was recorded.
--
-- `by_name` and `role` are copied in at submission time rather than joined from
-- contributors. A verification has to keep saying who made it after that person's
-- token is revoked or renamed — the field check still happened, and a revoked
-- token does not un-walk the walk.
CREATE TABLE IF NOT EXISTS submissions (
  id        TEXT PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind IN ('verify','redundant','upload')),
  by_id     TEXT NOT NULL,
  by_name   TEXT NOT NULL,
  role      TEXT NOT NULL,
  payload   TEXT NOT NULL,
  has_photo INTEGER NOT NULL DEFAULT 0,
  submitted TEXT NOT NULL,
  status    TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','recorded','refused')),
  detail    TEXT NOT NULL DEFAULT '',
  settled   TEXT
);

-- The drain reads pending-oldest-first on every pipeline tick; two marks on one
-- photograph have to be applied in the order the person made them.
CREATE INDEX IF NOT EXISTS submissions_pending ON submissions (status, submitted);
CREATE INDEX IF NOT EXISTS submissions_by ON submissions (by_id, submitted);
