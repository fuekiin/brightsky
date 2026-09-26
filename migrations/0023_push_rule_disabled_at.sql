-- A rule missing from a registration is disabled, not deleted: it keeps its
-- rule_state („already reported") for a while, so a registration that
-- briefly lacks it cannot re-arm it (docs/nano/push.md, „Missing rules").
ALTER TABLE push.rules ADD COLUMN disabled_at timestamptz;
