import { db, handle, send, HttpError, RANGES } from "./_lib.js";

const MAX_ROWS = 200000;

// CSV download of the stored samples. ?range=1h|6h|24h|7d|month|all (default all)
export default handle(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const range = url.searchParams.get("range") ?? "all";
  if (!(range in RANGES)) throw new HttpError(400, "invalid range");
  const seconds = RANGES[range];
  const since = seconds == null ? "-infinity" : new Date(Date.now() - seconds * 1000).toISOString();

  const sql = db();
  const rows = await sql.query(
    `SELECT ts, volts, amps, watts, ldr_left, ldr_right, ldr_top, ldr_bottom, pan, tilt, state, energy_wh
     FROM energy_samples WHERE ts >= $1::timestamptz ORDER BY ts LIMIT ${MAX_ROWS}`,
    [since],
  );
  const cell = (v) => (v == null ? "" : String(v));
  const lines = ["timestamp,volts,amps,watts,ldr_left,ldr_right,ldr_top,ldr_bottom,pan,tilt,state,energy_wh"];
  for (const r of rows) {
    lines.push([
      new Date(r.ts).toISOString(), r.volts, r.amps, r.watts, r.ldr_left, r.ldr_right,
      r.ldr_top, r.ldr_bottom, r.pan, r.tilt, r.state, r.energy_wh,
    ].map(cell).join(","));
  }
  send(res, 200, lines.join("\n") + "\n", {
    "Content-Type": "text/csv; charset=utf-8",
    "Content-Disposition": `attachment; filename="helios-${range}.csv"`,
  });
});
