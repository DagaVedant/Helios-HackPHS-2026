import { db, handle, send, HttpError, secretOk, parseSample, energyStep } from "./_lib.js";

const MAX_BATCH = 100;

// POST from pi/sender.py. Body: one sample, an array, or { samples: [...], ab_result? }.
export default handle(async (req, res) => {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    throw new HttpError(405, "POST only");
  }
  if (!secretOk(req.headers["x-shared-secret"], process.env.INGEST_SECRET)) {
    throw new HttpError(401, "bad or missing x-shared-secret");
  }

  const body = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
  const list = Array.isArray(body) ? body : Array.isArray(body?.samples) ? body.samples : body ? [body] : [];
  const ab = body && !Array.isArray(body) ? body.ab_result : null;
  if (list.length === 0 && !ab) throw new HttpError(400, "no samples");
  if (list.length > MAX_BATCH) throw new HttpError(413, `max ${MAX_BATCH} samples per request`);

  const samples = list.map((s) => parseSample(s)).sort((a, b) => a.ts - b.ts);
  const sql = db();
  let stored = 0;

  if (samples.length) {
    const [last] = await sql`SELECT ts, energy_wh FROM energy_samples ORDER BY ts DESC LIMIT 1`;
    let prevTs = last ? new Date(last.ts).getTime() : null;
    let prevEnergy = last ? Number(last.energy_wh) : 0;

    for (const s of samples) {
      const t = s.ts.getTime();
      if (prevTs != null && t <= prevTs) continue; // duplicate or out of order
      const { intervalWh, energyWh } = energyStep(prevTs, prevEnergy, t, s.watts);
      await sql`
        INSERT INTO energy_samples
          (ts, volts, amps, watts, ldr_left, ldr_right, ldr_top, ldr_bottom, pan, tilt, state, interval_wh, energy_wh)
        VALUES
          (${s.ts.toISOString()}, ${s.volts}, ${s.amps}, ${s.watts}, ${s.ldr_left}, ${s.ldr_right},
           ${s.ldr_top}, ${s.ldr_bottom}, ${s.pan}, ${s.tilt}, ${s.state}, ${intervalWh}, ${energyWh})
        ON CONFLICT (ts) DO NOTHING`;
      prevTs = t;
      prevEnergy = energyWh;
      stored++;
    }

    const newest = samples[samples.length - 1];
    const payload = {
      volts: newest.volts, amps: newest.amps, watts: newest.watts,
      ldr: { left: newest.ldr_left, right: newest.ldr_right, top: newest.ldr_top, bottom: newest.ldr_bottom },
      pan: newest.pan, tilt: newest.tilt, state: newest.state, sample_ts: newest.ts.toISOString(),
    };
    await sql`
      INSERT INTO latest (id, payload, received_at) VALUES (1, ${JSON.stringify(payload)}::jsonb, now())
      ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload, received_at = EXCLUDED.received_at`;
  }

  if (ab) {
    const fixed = Number(ab.fixed_mw);
    const tracking = Number(ab.tracking_mw);
    if (!(fixed > 0) || !Number.isFinite(tracking)) throw new HttpError(400, "bad ab_result");
    const gain = Number.isFinite(Number(ab.gain_pct)) ? Number(ab.gain_pct) : ((tracking - fixed) / fixed) * 100;
    const at = ab.timestamp ? new Date(ab.timestamp) : new Date();
    if (Number.isNaN(at.getTime())) throw new HttpError(400, "bad ab_result timestamp");
    await sql`
      INSERT INTO ab_results (ts, fixed_mw, tracking_mw, gain_pct)
      VALUES (${at.toISOString()}, ${fixed}, ${tracking}, ${gain}) ON CONFLICT (ts) DO NOTHING`;
  }

  send(res, 200, { ok: true, stored });
});
