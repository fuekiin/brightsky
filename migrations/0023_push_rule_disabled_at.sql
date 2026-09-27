-- A rule missing from a registration is disabled, not deleted: it keeps its
-- rule_state („already reported") for a while, so a registration that
-- briefly lacks it cannot re-arm it (docs/nano/push.md, „Missing rules").
ALTER TABLE push.rules ADD COLUMN disabled_at timestamptz;

-- Foreign keys without an index make every delete of a rule or a cell scan
-- the referencing table (review 2026-09-27, B1): the hourly cleanup.
CREATE INDEX live_activities_rule_idx ON push.live_activities (rule_id);
CREATE INDEX rules_cell_idx ON push.rules (cell_key);
