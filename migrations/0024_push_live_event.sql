-- Which Live Activity push this was (start | update | end) and whether it
-- lit the screen (an `alert` in the payload). Alerting Live Activity pushes
-- count toward the per-device hourly limit, starts toward the daily cap,
-- and starts become countable without matching worker log lines
-- (Live Activity review 2026-10-01).
ALTER TABLE push.notifications_sent ADD COLUMN live_event text;
ALTER TABLE push.notifications_sent ADD COLUMN alerting boolean;

-- Whether the device ever proved that its cards arrive: a token report or a
-- dismissal (the app reports dismissals even where it fails to report
-- tokens). Only a device that never did falls back to notifications, after
-- `live_unconfirmed` starts in a row without either (livectl.can_start).
ALTER TABLE push.devices ADD COLUMN live_confirmed boolean NOT NULL
  DEFAULT false;
ALTER TABLE push.devices ADD COLUMN live_unconfirmed int NOT NULL DEFAULT 0;
-- The evidence so far: a reported token or activity id, or a dismissal.
UPDATE push.devices d SET live_confirmed = true WHERE EXISTS (
  SELECT 1 FROM push.live_activities la WHERE la.device_id = d.id
    AND (la.activity_token IS NOT NULL OR la.activity_id IS NOT NULL
         OR la.state ? 'dismissed'));
