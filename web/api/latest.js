import { db, handle, send, mapLatest } from "./_lib.js";

export default handle(async (req, res) => {
  const sql = db();
  const [row] = await sql`SELECT payload, received_at FROM latest WHERE id = 1`;
  if (row) {
    const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
    send(res, 200, mapLatest(payload, new Date(row.received_at).getTime()));
    return;
  }

  const [s] = await sql`
    SELECT ts, volts, amps, watts, ldr_left, ldr_right, ldr_top, ldr_bottom, pan, tilt, state
    FROM energy_samples ORDER BY ts DESC LIMIT 1`;
  if (!s) {
    send(res, 200, { empty: true, online: false, age_s: null });
    return;
  }
  const payload = {
    volts: s.volts, amps: s.amps, watts: s.watts, pan: s.pan, tilt: s.tilt, state: s.state,
    ldr: { left: s.ldr_left, right: s.ldr_right, top: s.ldr_top, bottom: s.ldr_bottom },
  };
  send(res, 200, mapLatest(payload, new Date(s.ts).getTime()));
});
