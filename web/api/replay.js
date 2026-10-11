import { db, handle, send, mapLatest } from "./_lib.js";

export default handle(async (req, res) => {
  const sql = db();
  const rows = await sql`
    SELECT ts, volts, amps, watts, ldr_left, ldr_right, ldr_top, ldr_bottom, pan, tilt, state
    FROM energy_samples ORDER BY ts DESC LIMIT 3600`;
  const samples = rows.reverse().map((s) => {
    const m = mapLatest(
      {
        volts: s.volts, amps: s.amps, watts: s.watts, pan: s.pan, tilt: s.tilt, state: s.state,
        ldr: { left: s.ldr_left, right: s.ldr_right, top: s.ldr_top, bottom: s.ldr_bottom },
      },
      new Date(s.ts).getTime(),
    );
    return { v: m.v, ma: m.ma, w: m.w, l1: m.l1, l2: m.l2, l3: m.l3, l4: m.l4, pan: m.pan, tilt: m.tilt, status: m.status };
  });
  send(res, 200, { samples }, { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=600" });
});
