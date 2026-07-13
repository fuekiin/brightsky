CREATE TABLE biowetter (
  id               serial PRIMARY KEY,
  zone_id          text NOT NULL,
  zone_name        text NOT NULL,
  date             date NOT NULL,
  period           text NOT NULL,
  weather_class    text,
  effects          jsonb,
  recommendations  jsonb,
  last_update      timestamptz NOT NULL,
  next_update      timestamptz,
  sender           text,

  CONSTRAINT biowetter_key UNIQUE (zone_id, date, period)
);

CREATE TABLE uv_index (
  id           serial PRIMARY KEY,
  city         text NOT NULL,
  date         date NOT NULL,
  uv_index     smallint NOT NULL,
  last_update  timestamptz NOT NULL,
  next_update  timestamptz,
  sender       text,

  CONSTRAINT uv_index_key UNIQUE (city, date)
);

CREATE TABLE thermal_hazard (
  id           serial PRIMARY KEY,
  city         text NOT NULL,
  timestamp    timestamptz NOT NULL,
  level        text NOT NULL,
  last_update  timestamptz NOT NULL,
  next_update  timestamptz,
  sender       text,

  CONSTRAINT thermal_hazard_key UNIQUE (city, timestamp)
);
