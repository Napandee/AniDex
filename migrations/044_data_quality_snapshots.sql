-- Issue #485 — periodic snapshots of the admin Data Quality tab's signals
-- (_data_quality_signals() in app/main.py, issue #202), so the tab can show a
-- trend instead of only ever a single point-in-time read. Purely additive: a
-- new table, nothing existing changes shape. Instance-wide, not per-user —
-- same scope boundary as the admin Data Quality tab itself (see #202/#337's
-- split for the separate per-user companion view, which this does not touch).
--
-- A compact summarized shape rather than a full dump of _data_quality_signals()'s
-- raw output, per the issue's own "pick whichever keeps write volume and query
-- cost reasonable at this app's real scale" guidance — one row per scheduled
-- run (see health_signal_check-adjacent data_quality_snapshot job in app/main.py),
-- pruned to a rolling window rather than kept forever.
CREATE TABLE data_quality_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    failure_rate_overall DOUBLE PRECISION,
    orphaned_personal_notes_count INTEGER NOT NULL,
    stale_recommendations_count INTEGER NOT NULL,
    drift_candidates_count INTEGER NOT NULL,
    pending_migrations INTEGER,
    rate_limit_active BOOLEAN NOT NULL
);

CREATE INDEX idx_data_quality_snapshots_snapshot_at ON data_quality_snapshots (snapshot_at);
