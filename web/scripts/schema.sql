CREATE TABLE IF NOT EXISTS energy_samples (
  ts          timestamptz PRIMARY KEY,
  volts       real,
  amps        real,
  watts       real NOT NULL,
  ldr_left    real,
  ldr_right   real,
  ldr_top     real,
  ldr_bottom  real,
  pan         real,
  tilt        real,
  state       text,
  interval_wh double precision NOT NULL,
  energy_wh   double precision NOT NULL
);

CREATE TABLE IF NOT EXISTS latest (
  id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  payload     jsonb NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ab_results (
  ts           timestamptz PRIMARY KEY DEFAULT now(),
  fixed_mw     real NOT NULL,
  tracking_mw  real NOT NULL,
  gain_pct     real NOT NULL
);
