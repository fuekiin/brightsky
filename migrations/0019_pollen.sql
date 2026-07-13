CREATE TABLE pollen (
  id               serial PRIMARY KEY,
  region_id        smallint NOT NULL,
  partregion_id    smallint NOT NULL,
  region_name      text NOT NULL,
  partregion_name  text,
  species          text NOT NULL,
  date             date NOT NULL,
  index            text NOT NULL,
  severity         real,
  last_update      timestamptz NOT NULL,
  next_update      timestamptz,
  sender           text,

  CONSTRAINT pollen_key UNIQUE (region_id, partregion_id, species, date)
);
