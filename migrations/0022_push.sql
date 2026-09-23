-- nano push service (docs/nano/push.md). Own schema so it never collides with
-- upstream tables; nothing here references Bright Sky's tables.
CREATE SCHEMA push;

CREATE TABLE push.devices (
  id                       uuid PRIMARY KEY,
  secret_hash              text NOT NULL,
  apns_token               text,
  push_to_start_token      text,
  live_activities_enabled  boolean NOT NULL DEFAULT true,
  environment              text NOT NULL CHECK (environment IN ('sandbox', 'production')),
  tier                     text NOT NULL DEFAULT 'free',
  app_version              text,
  created_at               timestamptz NOT NULL DEFAULT current_timestamp,
  last_seen                timestamptz NOT NULL
);

CREATE TABLE push.cells (
  cell_key      text PRIMARY KEY,
  lat           double precision NOT NULL,
  lon           double precision NOT NULL,
  warn_cell_id  int,
  resolved_at   timestamptz
);

CREATE TABLE push.rules (
  id         uuid PRIMARY KEY,
  device_id  uuid NOT NULL REFERENCES push.devices ON DELETE CASCADE,
  kind       text NOT NULL,
  cell_key   text NOT NULL REFERENCES push.cells,
  params     jsonb NOT NULL,
  schedule   jsonb,
  live       jsonb,
  -- Order the app sent the rules in: breaks ties for the lead rule
  position   int NOT NULL DEFAULT 0,
  enabled    boolean NOT NULL DEFAULT true
);
CREATE INDEX rules_kind_cell_idx ON push.rules (kind, cell_key) WHERE enabled;
CREATE INDEX rules_device_idx ON push.rules (device_id);

CREATE TABLE push.rule_state (
  rule_id         uuid NOT NULL REFERENCES push.rules ON DELETE CASCADE,
  occurrence_key  text NOT NULL,
  state           jsonb NOT NULL,
  fired_at        timestamptz,
  expires_at      timestamptz,
  PRIMARY KEY (rule_id, occurrence_key)
);

CREATE TABLE push.live_activities (
  device_id       uuid PRIMARY KEY REFERENCES push.devices ON DELETE CASCADE,
  rule_id         uuid REFERENCES push.rules ON DELETE SET NULL,
  activity_id     text,
  phase           text NOT NULL,
  activity_token  text,
  started_at      timestamptz NOT NULL,
  last_update_at  timestamptz,
  last_content    jsonb NOT NULL,
  -- What the activity is about: event key, level, predicted change, class
  state           jsonb NOT NULL DEFAULT '{}',
  ended_at        timestamptz,
  cooldown_until  timestamptz
);

CREATE TABLE push.notifications_sent (
  id              bigserial PRIMARY KEY,
  device_id       uuid,
  rule_id         uuid,
  occurrence_key  text,
  push_type       text,
  sent_at         timestamptz NOT NULL DEFAULT current_timestamp,
  apns_status     int,
  apns_reason     text,
  apns_id         text
);
CREATE INDEX notifications_sent_sent_at_idx ON push.notifications_sent (sent_at);

-- Liveness of the push-work loops and the sender, read by push-api's /health.
CREATE TABLE push.source_status (
  source        text PRIMARY KEY,
  last_success  timestamptz,
  last_attempt  timestamptz,
  last_error    text
);
