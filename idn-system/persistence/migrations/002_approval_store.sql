-- 002_approval_store: human-approval state persistence
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id  TEXT PRIMARY KEY,
    intent_id    TEXT NOT NULL,
    state_json   TEXT NOT NULL,   -- serialised GlobalAgentState
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_status
    ON pending_approvals(status, created_at DESC);
