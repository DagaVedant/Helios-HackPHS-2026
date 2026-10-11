// Fills Neon with a 1 hour, 1 Hz dataset generated from the value ranges observed while building:
//   panel volts 8.4-8.9 V, current 10-100 mA, LDR counts on the ADS1115 scale,
//   pan +/-80 deg, tilt 15-85 deg, the same control law as pi/tracker.py.
// It is a model of the tracker's behaviour, not a raw capture. See the README.
//
// Usage: node scripts/seed.mjs [--dry-run] [--reset] [--end 2026-10-11T18:00:00Z] [--seed 7]
import { neon } from "@neondatabase/serverless";
import { loadEnv } from "./env.mjs";
import { energyStep } from "../api/_lib.js";

const SECONDS = 3600;
const START_TRACK_S = 20;
const STEPS_PER_S = 20;
const GAIN = 8, DEAD = 0.03, CAP = 3, MIN_STEP = 0.4;
const PAN = [-80, 80], TILT = [15, 85];

function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const smooth = (x) => { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); };

export function generate({ endMs = Date.now(), seed = 7 } = {}) {
  const rand = mulberry32(seed);
  const noise = (s) => (rand() * 2 - 1) * s;
  const startMs = endMs - (SECONDS - 1) * 1000;

  let pan = 0, tilt = 45;
  const moving = { pan: false, tilt: false };
  const rows = [];

  const axisStep = (err, axis, lo, hi, pos) => {
    const a = Math.abs(err);
    if (moving[axis]) { if (a < DEAD) moving[axis] = false; }
    else if (a >= DEAD * 1.5) moving[axis] = true;
    if (!moving[axis]) return pos;
    let step = GAIN * err;
    if (Math.abs(step) < MIN_STEP) step = step > 0 ? MIN_STEP : -MIN_STEP;
    return clamp(pos + clamp(step, -CAP, CAP), lo, hi);
  };

  for (let t = 0; t < SECONDS; t++) {
    // where the lamp is, in the mount's pan/tilt frame, and how bright it is
    const early = 1 - smooth(t / 90); // lamp starts near the home pose, then wanders
    const az = clamp(5 + 52 * Math.sin(t / 430 + 0.6) + 22 * Math.sin(t / 141 + 1.9) + 6 * Math.sin(t / 37), -78, 78) * (1 - early) + 22 * early;
    const el = clamp(52 + 24 * Math.sin(t / 360 + 2.1) + 9 * Math.sin(t / 113) + 3 * Math.sin(t / 29), 17, 83) * (1 - early) + 62 * early;
    let b = 0.75 + 0.15 * Math.sin(t / 520) + 0.08 * Math.sin(t / 97);
    if (t >= 1500 && t < 1560) b *= 0.55; // someone walks past the lamp
    if (t >= 2600 && t < 2640) b *= 0.6;
    b = clamp(b, 0.25, 1);

    const tracking = t >= START_TRACK_S;
    let exN = 0, ezN = 0, moved = false;
    for (let k = 0; k < STEPS_PER_S; k++) {
      exN = Math.tanh((az - pan) / 35) * 0.6;
      ezN = Math.tanh((el - tilt) / 35) * 0.6;
      if (!tracking) continue;
      const p0 = pan, t0 = tilt;
      pan = axisStep(exN, "pan", PAN[0], PAN[1], pan);
      tilt = axisStep(ezN, "tilt", TILT[0], TILT[1], tilt);
      moved = pan !== p0 || tilt !== t0;
    }

    const base = (5000 + 11000 * b) * (1 + noise(0.05));
    const ldr = (v) => Math.round(clamp(v * (1 + noise(0.045)), 0, 26400));
    const left = ldr(base * (1 - exN)), right = ldr(base * (1 + exN));
    const bottom = ldr(base * (1 - ezN)), top = ldr(base * (1 + ezN));

    const mis = Math.hypot(az - pan, el - tilt) * Math.PI / 180;
    const align = Math.pow(Math.max(0, Math.cos(Math.min(mis, 1.2))), 1.5);
    const bn = (b - 0.25) / 0.75;
    const ma = clamp(10 + 90 * bn * align + noise(1.2), 10, 100);
    const volts = clamp(8.4 + 0.5 * clamp((ma - 10) / 90, 0, 1) + noise(0.015), 8.4, 8.9);

    rows.push({
      ts: new Date(startMs + t * 1000),
      volts: +volts.toFixed(3),
      amps: +(ma / 1000).toFixed(4),
      watts: +((volts * ma) / 1000).toFixed(5),
      ldr_left: left, ldr_right: right, ldr_top: top, ldr_bottom: bottom,
      pan: +pan.toFixed(1), tilt: +tilt.toFixed(1),
      state: !tracking ? "FIXED" : moved ? "TRACK" : "LOCKED",
    });
  }

  // cumulative energy, same bookkeeping as /api/ingest
  let prevTs = null, prevE = 0;
  for (const r of rows) {
    const { intervalWh, energyWh } = energyStep(prevTs, prevE, r.ts.getTime(), r.watts);
    r.interval_wh = intervalWh;
    r.energy_wh = energyWh;
    prevTs = r.ts.getTime();
    prevE = energyWh;
  }

  // A/B test: 7 s of the fixed pose before tracking started, 7 s of tracking after it settled
  const mean = (a, b) => rows.slice(a, b).reduce((s, r) => s + r.watts, 0) / (b - a);
  const fixed = mean(10, 17) * 1000, tracking = mean(28, 35) * 1000;
  const ab = {
    ts: new Date(startMs + 36 * 1000),
    fixed_mw: +fixed.toFixed(3),
    tracking_mw: +tracking.toFixed(3),
    gain_pct: +(((tracking - fixed) / fixed) * 100).toFixed(1),
  };
  return { rows, ab };
}

function summary(rows) {
  const col = (k) => rows.map((r) => r[k]);
  const r = (k, d = 2) => `${Math.min(...col(k)).toFixed(d)}..${Math.max(...col(k)).toFixed(d)}`;
  return [
    `rows        ${rows.length}`,
    `volts       ${r("volts")}   mean ${(col("volts").reduce((a, b) => a + b, 0) / rows.length).toFixed(2)}`,
    `amps (mA)   ${(Math.min(...col("amps")) * 1000).toFixed(1)}..${(Math.max(...col("amps")) * 1000).toFixed(1)}`,
    `watts       ${r("watts", 3)}`,
    `ldr left    ${r("ldr_left", 0)}`,
    `pan / tilt  ${r("pan", 1)} / ${r("tilt", 1)}`,
    `energy      ${rows[rows.length - 1].energy_wh.toFixed(4)} Wh`,
  ].join("\n");
}

async function main() {
  const args = process.argv.slice(2);
  const flag = (n) => args.includes(n);
  const val = (n, d) => (args.includes(n) ? args[args.indexOf(n) + 1] : d);
  const endMs = val("--end") ? new Date(val("--end")).getTime() : Date.now();
  if (Number.isNaN(endMs)) throw new Error("bad --end");
  const { rows, ab } = generate({ endMs, seed: Number(val("--seed", 7)) });

  console.log(summary(rows));
  console.log(`A/B         fixed ${ab.fixed_mw} mW, tracking ${ab.tracking_mw} mW, gain ${ab.gain_pct}%`);
  if (flag("--dry-run")) return;

  loadEnv();
  if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL is not set (web/.env)");
  const sql = neon(process.env.DATABASE_URL);

  if (flag("--reset")) {
    await sql`TRUNCATE energy_samples`;
    await sql`TRUNCATE ab_results`;
  } else {
    const [{ n }] = await sql`SELECT count(*)::int AS n FROM energy_samples WHERE ts >= ${rows[0].ts.toISOString()}`;
    if (n > 0) throw new Error("table already has rows in that window; pass --reset or a later --end");
  }
  const [{ e }] = await sql`SELECT COALESCE(max(energy_wh), 0) AS e FROM energy_samples`;
  const offset = Number(e);

  const BATCH = 500;
  for (let i = 0; i < rows.length; i += BATCH) {
    const c = rows.slice(i, i + BATCH);
    await sql.query(
      `INSERT INTO energy_samples
         (ts, volts, amps, watts, ldr_left, ldr_right, ldr_top, ldr_bottom, pan, tilt, state, interval_wh, energy_wh)
       SELECT * FROM unnest($1::timestamptz[], $2::real[], $3::real[], $4::real[], $5::real[], $6::real[],
                            $7::real[], $8::real[], $9::real[], $10::real[], $11::text[],
                            $12::double precision[], $13::double precision[])
       ON CONFLICT (ts) DO NOTHING`,
      [
        c.map((r) => r.ts.toISOString()), c.map((r) => r.volts), c.map((r) => r.amps), c.map((r) => r.watts),
        c.map((r) => r.ldr_left), c.map((r) => r.ldr_right), c.map((r) => r.ldr_top), c.map((r) => r.ldr_bottom),
        c.map((r) => r.pan), c.map((r) => r.tilt), c.map((r) => r.state),
        c.map((r) => r.interval_wh), c.map((r) => r.energy_wh + offset),
      ],
    );
    process.stdout.write(`\rinserted ${Math.min(i + BATCH, rows.length)}/${rows.length}`);
  }
  await sql`INSERT INTO ab_results (ts, fixed_mw, tracking_mw, gain_pct)
            VALUES (${ab.ts.toISOString()}, ${ab.fixed_mw}, ${ab.tracking_mw}, ${ab.gain_pct})
            ON CONFLICT (ts) DO NOTHING`;
  console.log("\ndone.");
}

if (import.meta.url === `file:///${process.argv[1].replace(/\\/g, "/")}` || process.argv[1]?.endsWith("seed.mjs")) {
  main().catch((e) => { console.error(e.message); process.exit(1); });
}
