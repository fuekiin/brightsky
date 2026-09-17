CREATE TABLE radar3d_frames (
  product     text NOT NULL,
  timestamp   timestamptz NOT NULL,
  path        text NOT NULL,
  sites       smallint,
  created_at  timestamptz NOT NULL DEFAULT current_timestamp,

  CONSTRAINT radar3d_frames_key UNIQUE (product, timestamp)
);
