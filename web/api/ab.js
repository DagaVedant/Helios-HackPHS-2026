import { db, handle, send } from "./_lib.js";

export default handle(async (req, res) => {
  const sql = db();
  const [row] = await sql`SELECT ts, fixed_mw, tracking_mw, gain_pct FROM ab_results ORDER BY ts DESC LIMIT 1`;
  send(res, 200, row ? {
    timestamp: new Date(row.ts).toISOString(),
    fixed_mw: Number(row.fixed_mw),
    tracking_mw: Number(row.tracking_mw),
    gain_pct: Number(row.gain_pct),
  } : { empty: true });
});
