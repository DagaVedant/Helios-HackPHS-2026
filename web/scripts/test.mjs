
import assert from "node:assert/strict";
import { parseSample, energyStep, mapLatest, statusFor, secretOk } from "../api/_lib.js";
import { generate } from "./seed.mjs";

const now = Date.parse("2026-10-11T12:00:00Z");

const s = parseSample({ timestamp: "2026-10-11T11:59:59Z", volts: 8.6, amps: 0.05, watts: 0.43,
  ldr: { left: 100, right: 200, top: 300, bottom: 400 }, pan: 10, tilt: 50, state: "TRACK" }, now);
assert.equal(s.ldr_right, 200);
assert.equal(parseSample({ timestamp: "2026-10-11T11:59:59Z", watts: null }, now).watts, 0);
assert.throws(() => parseSample({ timestamp: "nope" }, now), /timestamp/);
assert.throws(() => parseSample({ timestamp: "2027-01-01T00:00:00Z" }, now), /future/);
assert.throws(() => parseSample(null, now), /object/);

let r = energyStep(null, 0, 1000, 1);
assert.equal(r.intervalWh, 0);
r = energyStep(1000, 0.5, 2000, 1);
assert.ok(Math.abs(r.intervalWh - 1 / 3600) < 1e-12);
assert.ok(Math.abs(r.energyWh - (0.5 + 1 / 3600)) < 1e-12);
r = energyStep(0, 0, 3_600_000, 1);
assert.ok(Math.abs(r.intervalWh - 10 / 3600) < 1e-12);

const m = mapLatest({ volts: 8.7, amps: 0.05, watts: 0.435, ldr: { left: 26400, right: 0, top: 13200, bottom: 99999 },
  pan: -80, tilt: 85, state: "LOCKED" }, now - 2000, now);
assert.equal(m.ma, 50);
assert.equal(m.l1, 1023);
assert.equal(m.l2, 0);
assert.equal(m.l3, 512);
assert.equal(m.l4, 1023);
assert.equal(m.pan, 10);
assert.equal(m.tilt, 130);
assert.equal(m.status, "ALIGNED");
assert.equal(m.online, true);
assert.equal(mapLatest({}, now - 6000, now).online, false);
assert.equal(mapLatest({}, now, now).pan, 90);
assert.equal(mapLatest({}, now, now).tilt, 90);
for (const [st, want] of [["TRACK", "SEARCHING"], ["DARK", "NIGHT MODE"], ["HOME", "NIGHT MODE"],
  ["FIXED", "IDLE"], ["UNARMED", "IDLE"], ["AB-A", "IDLE"], ["AB-B", "SEARCHING"]]) {
  assert.equal(statusFor(st), want);
}

assert.equal(secretOk("abc", "abc"), true);
assert.equal(secretOk("abd", "abc"), false);
assert.equal(secretOk("ab", "abc"), false);
assert.equal(secretOk(undefined, "abc"), false);
assert.equal(secretOk("x", ""), false);

const { rows, ab } = generate({ endMs: now });
assert.equal(rows.length, 3600);
for (const x of rows) {
  assert.ok(x.volts >= 8.4 && x.volts <= 8.9, "volts");
  assert.ok(x.amps >= 0.01 && x.amps <= 0.1, "amps");
  assert.ok(x.pan >= -80 && x.pan <= 80, "pan");
  assert.ok(x.tilt >= 15 && x.tilt <= 85, "tilt");
  for (const k of ["ldr_left", "ldr_right", "ldr_top", "ldr_bottom"]) assert.ok(x[k] >= 0 && x[k] <= 26400, k);
}
for (let i = 1; i < rows.length; i++) {
  assert.equal(rows[i].ts - rows[i - 1].ts, 1000);
  assert.ok(rows[i].energy_wh >= rows[i - 1].energy_wh);
}
assert.ok(ab.gain_pct > 0);
const again = generate({ endMs: now });
assert.deepEqual(again.rows[1234], rows[1234], "deterministic for a given seed");

console.log("all checks passed");
