import { neon } from "@neondatabase/serverless";
import { timingSafeEqual } from "node:crypto";

export function db() {
  if (!process.env.DATABASE_URL) throw new HttpError(500, "DATABASE_URL is not set");
  return neon(process.env.DATABASE_URL);
}

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function send(res, status, body, headers = {}) {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  for (const [k, v] of Object.entries(headers)) res.setHeader(k, v);
  if (typeof body === "string") {
    res.end(body);
  } else {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(body));
  }
}

export function handle(fn) {
  return async (req, res) => {
    try {
      await fn(req, res);
    } catch (e) {
      const status = e instanceof HttpError ? e.status : 500;
      if (status === 500) console.error(e);
      send(res, status, { error: status === 500 ? "Server error" : e.message });
    }
  };
}

export function secretOk(provided, expected) {
  if (!expected || typeof provided !== "string") return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

// ---- Pi sample -> stored row -------------------------------------------

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Validates one sample from sender.py. Returns a normalized object or throws. */
export function parseSample(raw, nowMs = Date.now()) {
  if (!raw || typeof raw !== "object") throw new HttpError(400, "sample must be an object");
  const ts = new Date(raw.timestamp);
  if (Number.isNaN(ts.getTime())) throw new HttpError(400, "bad timestamp");
  if (ts.getTime() > nowMs + 5 * 60_000) throw new HttpError(400, "timestamp is in the future");
  const ldr = raw.ldr && typeof raw.ldr === "object" ? raw.ldr : {};
  const watts = num(raw.watts);
  return {
    ts,
    volts: num(raw.volts),
    amps: num(raw.amps),
    watts: watts === null ? 0 : Math.max(0, watts),
    ldr_left: num(ldr.left),
    ldr_right: num(ldr.right),
    ldr_top: num(ldr.top),
    ldr_bottom: num(ldr.bottom),
    pan: num(raw.pan),
    tilt: num(raw.tilt),
    state: typeof raw.state === "string" ? raw.state.slice(0, 16) : null,
  };
}

/** Energy bookkeeping: gaps longer than 10 s count as 10 s so outages don't invent energy. */
export function energyStep(prevTsMs, prevEnergyWh, tsMs, watts) {
  const dt = prevTsMs == null ? 0 : Math.min(Math.max((tsMs - prevTsMs) / 1000, 0), 10);
  const intervalWh = (watts * dt) / 3600;
  return { intervalWh, energyWh: (prevEnergyWh ?? 0) + intervalWh };
}

// ---- stored row -> page fields -------------------------------------------

export const LDR_MAX_COUNTS = 26400; // 3.3 V of the ADS1115's +/-4.096 V range at gain 1
export const OFFLINE_AFTER_S = 5;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const scaleLdr = (v) => (v == null ? 0 : Math.round(clamp(v / LDR_MAX_COUNTS, 0, 1) * 1023));

export function statusFor(state) {
  switch (state) {
    case "LOCKED":
      return "ALIGNED";
    case "TRACK":
    case "AB-B":
      return "SEARCHING";
    case "DARK":
    case "HOME":
      return "NIGHT MODE";
    default:
      return "IDLE"; // FIXED, UNARMED, AB-A
  }
}

/** Maps the Pi sample to the field names the Helios page already uses. */
export function mapLatest(p, receivedAtMs, nowMs = Date.now()) {
  const ageS = Math.max(0, (nowMs - receivedAtMs) / 1000);
  return {
    v: p.volts ?? 0,
    ma: (p.amps ?? 0) * 1000,
    w: p.watts ?? 0,
    lux: null, // no light sensor on this build
    t: null,
    h: null,
    l1: scaleLdr(p.ldr?.left),
    l2: scaleLdr(p.ldr?.right),
    l3: scaleLdr(p.ldr?.top),
    l4: scaleLdr(p.ldr?.bottom),
    // pan -80..80 and tilt 15..85 are shown on the page's 0..180 bars with 90 as center
    pan: clamp((p.pan ?? 0) + 90, 0, 180),
    tilt: clamp((p.tilt ?? 45) + 45, 0, 180),
    status: statusFor(p.state),
    state: p.state ?? null,
    age_s: Math.round(ageS * 10) / 10,
    online: ageS <= OFFLINE_AFTER_S,
  };
}

// ---- energy history ranges -------------------------------------------

export const RANGES = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, month: 2592000, all: null };
export const TIME_WINDOWS = {
  "full-day": [0, 1440],
  night: [0, 360],
  morning: [360, 720],
  midday: [540, 900],
  afternoon: [720, 1080],
  evening: [1080, 1440],
};
