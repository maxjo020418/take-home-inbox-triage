-- One row per ingested email; `status` is the pipeline state machine.
--   queued -> classifying -> needs_approval -> done
--                         \-> dropped (spam)
--   classifying -> queued (retry) | failed (attempts exhausted; human can retry)
CREATE TABLE IF NOT EXISTS emails (
    id           TEXT PRIMARY KEY,               -- provider id: dedupe key for polling
    sender       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued','classifying','needs_approval','done','dropped','failed')),
    label        TEXT CHECK (label IN ('billing','bug_report','sales_lead','spam')),
    reason       TEXT,
    attempts     INT NOT NULL DEFAULT 0,
    last_error   TEXT,
    locked_at    TIMESTAMPTZ,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS emails_status_idx ON emails (status, received_at);

-- One row per proposed external write. Flow: pending -> approved -> executed | failed
-- (or pending -> rejected). Nothing is sent before 'approved'. After approval the write
-- may reach the external API even if the row ends 'failed' or stays 'approved' (crash):
-- failed+retryable means it provably never left; any other failure is uncertain and must
-- be verified in the target system before re-sending.
CREATE TABLE IF NOT EXISTS actions (
    id           BIGSERIAL PRIMARY KEY,
    email_id     TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('send_reply','send_alert','create_lead')),
    payload      JSONB NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','executed','failed')),
    result       JSONB,
    error        TEXT,
    proposed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ,
    executed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS actions_status_idx ON actions (status);

-- Reviewers. Passwords are scrypt hashes (see src/backend/auth.py).
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bearer sessions. Only a SHA-256 of the token is stored, so a DB leak
-- doesn't hand out live sessions.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- Who approved/rejected each action.
ALTER TABLE actions ADD COLUMN IF NOT EXISTS decided_by TEXT;

-- Append-only audit trail, written in the same transaction as the change.
CREATE TABLE IF NOT EXISTS events (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,            -- 'poller', 'worker', or a username
    email_id   TEXT,
    action_id  BIGINT,
    message    TEXT NOT NULL
);

-- Sender-domain check (src/backend/domain_check.py), filled by the worker before the LLM call.
ALTER TABLE emails ADD COLUMN IF NOT EXISTS domain_verdict TEXT
    CHECK (domain_verdict IN ('ok','check_needed','impersonation_suspected'));
ALTER TABLE emails ADD COLUMN IF NOT EXISTS domain_detail JSONB;

-- LLM-reported: the email contained instructions aimed at an AI/automation (ignored).
ALTER TABLE emails ADD COLUMN IF NOT EXISTS injection_detected BOOLEAN;
-- Where the injection flag came from: {"llm": bool, "patterns": [names]} (src/backend/injection.py).
ALTER TABLE emails ADD COLUMN IF NOT EXISTS injection_detail JSONB;
-- Action suggested by the model beyond the label's standard routing.
ALTER TABLE actions ADD COLUMN IF NOT EXISTS extra BOOLEAN NOT NULL DEFAULT false;
-- Set when execution failed: true = the write provably never reached the external API
-- (no token, no write scope, connect error), so a human may reopen it for re-approval.
-- false = outcome uncertain (sent, then timed out / errored, or interrupted): verify manually.
ALTER TABLE actions ADD COLUMN IF NOT EXISTS retryable BOOLEAN;
-- Identity of the current classification claim; result writes are fenced to it so a
-- job re-queued mid-run (stale sweep) can't be overwritten by the superseded run.
ALTER TABLE emails ADD COLUMN IF NOT EXISTS claim_id UUID;
