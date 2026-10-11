import { db, handle, send, HttpError, RANGES, TIME_WINDOWS } from "./_lib.js";

const MAX_POINTS = 600;

// Same contract as the old Flask /energy-history route.
export default handle(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const range = url.searchParams.get("range") ?? "24h";
  const view = url.searchParams.get("view") ?? "cumulative";
  const timeRange = url.searchParams.get("time_range") ?? "full-day";
  const tz = url.searchParams.get("tz") ?? "UTC";

  if (!(range in RANGES)) throw new HttpError(400, "invalid range");
  if (view !== "cumulative" && view !== "average") throw new HttpError(400, "invalid view");
  if (!(timeRange in TIME_WINDOWS)) throw new HttpError(400, "invalid time_range");
  if (!/^[A-Za-z0-9_+\-\/]{1,64}$/.test(tz)) throw new HttpError(400, "invalid tz");

  const sql = db();
  const seconds = RANGES[range];
  const since = seconds == null ? "-infinity" : new Date(Date.now() - seconds * 1000).toISOString();

  const [{ total }] = await sql`SELECT COALESCE(MAX(energy_wh), 0) AS total FROM energy_samples`;
  const total_wh = Number(total);

  if (view === "average") {
    const [lo, hi] = TIME_WINDOWS[timeRange];
    let rows;
    try {
      rows = await sql.query(
        `WITH s AS (
           SELECT interval_wh, watts,
                  (extract(hour FROM ts AT TIME ZONE $2::text) * 60 + extract(minute FROM ts AT TIME ZONE $2::text))::int AS mod
           FROM energy_samples WHERE ts >= $1::timestamptz)
         SELECT (mod / 30) * 30 AS minute_of_day,
                avg(interval_wh) AS average_interval_wh, avg(watts) AS average_watts, count(*)::int AS samples
         FROM s WHERE mod >= $3 AND mod < $4 GROUP BY 1 ORDER BY 1`,
        [since, tz, lo, hi],
      );
    } catch (e) {
      if (/time zone/i.test(String(e.message))) throw new HttpError(400, "invalid tz");
      throw e;
    }
    send(res, 200, {
      range, view, time_range: timeRange, total_wh,
      average_readings: rows.map((r) => ({
        minute_of_day: Number(r.minute_of_day),
        average_interval_wh: Number(r.average_interval_wh),
        average_watts: Number(r.average_watts),
        samples: Number(r.samples),
      })),
    });
    return;
  }

  // cumulative: bucket so the chart never gets more than ~MAX_POINTS points
  let span = seconds;
  if (span == null) {
    const [{ span_s }] = await sql`
      SELECT COALESCE(extract(epoch FROM (max(ts) - min(ts))), 0) AS span_s FROM energy_samples`;
    span = Number(span_s);
  }
  const bucket = Math.max(1, Math.ceil(span / MAX_POINTS));
  const rows = await sql.query(
    `SELECT extract(epoch FROM date_bin(make_interval(secs => $1::double precision), ts, TIMESTAMPTZ '2000-01-01'))::double precision AS timestamp,
            max(energy_wh) AS energy_wh, avg(watts) AS watts
     FROM energy_samples WHERE ts >= $2::timestamptz GROUP BY 1 ORDER BY 1`,
    [bucket, since],
  );
  const readings = rows.map((r) => ({
    timestamp: Number(r.timestamp),
    energy_wh: Number(r.energy_wh),
    watts: Number(r.watts),
  }));
  const window_wh = readings.length ? readings[readings.length - 1].energy_wh - readings[0].energy_wh : 0;
  send(res, 200, { range, view, total_wh, window_wh, readings });
});
