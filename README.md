# Balcony Solar Optimizer (Micro-Tracker) - hackPHS 2026, theme "Into the Skies"

A dual-axis (pan + tilt) mount for one 178 x 142 x 2 mm KEJNYT 12 V 3.6 W panel. Four edge LDRs steer it. A Raspberry Pi 5 is the brain (no Arduino boards), an ADS1115 reads the LDRs, an INA219 measures panel output, and a Vercel dashboard shows it. Servos are the kit MG90S/SG90. Build is printed parts, hot glue and a few screws.

## Folders
- 1_print_files/step and /stl : the CAD parts (see table). STL files are already in print orientation.
- 2_cad_source : microtracker_cad.py (parametric model), verify.py (checks), export.py, make_svgs.py, build_viewer.py, export_viewer.py (regenerate the SVGs and the viewer), horn_ref.step, verify_out.txt (last result: TOTAL FAILS 0).
- 3_project_docs : spec.md, cad-spec.md, checklist.html, the 3D viewer (3d-model.html = index.html) and the SVGs, all updated to the v6 design. The viewer uses the real CAD meshes.
- 4_your_original_cad : your six original STEP files (not modified).
- pi : tracker.py (sensors, control, CSV log, outbox) and sender.py (pushes the outbox to Vercel).
- web : the Helios dashboard for Vercel (static page in public/, API routes in api/, Neon schema and seed in scripts/).

## Parts to print (v6)
| File | Qty | Orientation | Notes |
|---|---|---|---|
| 1_base | 1 | as shown, 30 tall | 3 x M4 to the board (4th hole removed for the wire exit); servo pocket +1.5 mm at the wire end; big wire opening |
| 2_lid | 1 | as shown | 4 x M3 countersunk (already printed and fits) |
| 3_turntable_with_arms | 1 | standing, 125 tall | PETG preferred; 4 perimeters, 30% infill; keyed slot for the horn bar; centre-screw hole through the top |
| 4_tray | 1 | plate on bed, ribs up | 207 x 168; 2 perimeters / 10% infill saves hours; keyed slot for the horn bar in the left boss |
| 5_pivot_pin | 1 | head down | 14 mm pin, M5 bolt clamps it (already printed) |
| 6_fin | 4 | flat | LDR shade fins (already printed) |

Horn: use the straight 2-arm horn that came with the MG90S (31.2 x 6.9 x 2 mm bar, hub 4.7 tall). Not printed.
Reprint after the latest changes: base, turntable, tray. Lid, pin and fins are unchanged.

## Assembly
1. Base to the board (3 x M4). 2. MG90S in the pocket (wires out the wide opening). 3. Lid with 4 x M3. 4. Press a horn on the pan spline, add the small centre screw. 5. Lower the turntable so the bar drops into its slot (sits on the lid ring). 6. Tilt servo on arm A (flange on the outer face, body through the window). 7. Horn on the tilt spline, tray boss slot over the bar, pin through arm B into the other boss, M5 bolt to clamp. 8. Panel on the tray (engraved outline), LDRs in the 4 holes, fins beside them. Glue only if you want the horn locked in.

## Screws
4 x M4 (3 used on the base), 4 x M3 countersunk (lid), 1 x M5 about 30 mm (pin), M2 servo screws, the small horn centre screws that came with the servos.

## Electronics decisions made
- Servo power: any 5 V source of about 2 A that is not the Pi (the MB102 module with a 9 V wall adapter also works; a 9 V battery does not). Common ground to a Pi GND pin.
- LDR: GL5528 (in the kit, matches a 10k divider). Never mix 5528 and 5539. If buying, buy 20-50, measure under one lamp, keep the 4 best-matched.
- Software limits: pan +/-80, tilt 15 to 85 degrees.

## What was verified in software (verify.py)
Valid single solids; no static overlaps (base, lid, servos, turntable, tray, pin, both horns); tilt sweep -10 to 180 deg with first collision at 100 deg; horn pockets 0.25 mm clearance; roof above the pan horn 3.7 mm; pin fit 0.30 mm; swing circle 273 mm; head mass about 252 g and holding torque 0.64 kg-cm (about 30% of MG90S stall); bed fit (tray 207 x 168, turntable 100 x 100 x 125). STEP files re-imported valid.

## NOT verified
- Nothing past the base, lid, fins and the first pin has been printed and test-fitted. Print the tray and turntable, then dry-fit before gluing.
- The horn spline engagement depth (2.5 mm) and horn geometry come from the STEP file; hold your real horn against the slot first.
- Panel mass 130 g is an estimate. A 250 g panel gives about 1.1 kg-cm.
- Tilt servo wire exit side.

## Print time (crude)
About 300 g PLA/PETG. Roughly 8-9 h on a fast printer, about 25 h on an Ender-class printer at 4 perimeters. To get under 18 h on a slow printer: tray at 2 perimeters / 10% infill and 0.28 mm layers.
Materials: PETG for the turntable and anything thin; PLA is fine for base, lid and tray.

## Design history (short)
v5 had a bigger frame and 254 mm arms. v6 rebuilt it from your own STEP files: arms cut to 100 mm above the turntable, pan servo pocket and tilt servo window added, 4th LDR hole added, bolt holes cut from 15 to 4 + 4, tray fits a 220 bed, hard stops dropped in favour of servo range plus software limits, wire channel enlarged, horn slots added.
To change a dimension: edit the numbers at the top of microtracker_cad.py, then run  python verify.py  and  python export.py  (needs CadQuery; pip install --use-feature=truststore cadquery fixes the SSL error).

## Dashboard (Vercel + Neon)
The sample history on the dashboard (1 hour at 1 Hz) is reconstructed from value ranges observed while building, not a raw recording.

One-time setup:
1. Create a Neon database and a Vercel project from the `web` folder (Root Directory = `web`).
2. In Vercel set `DATABASE_URL` (Neon connection string) and `INGEST_SECRET` (a long random string). Copy both into `web/.env` for the scripts (see `web/.env.example`).
3. `cd web && npm install && npm run migrate && npm run seed` (use `npm run seed:dry` first to preview the data; `-- --reset` replaces existing rows).
4. Deploy (`vercel --prod`, or push to the linked Git repo).

On the Pi, in two SSH sessions:
```
python tracker.py      # sensors, control, writes outbox.jsonl, logs/*.csv
python sender.py       # reads outbox.jsonl, POSTs to /api/ingest
```
`sender.py` reads `MT_API_URL` (https://your-app.vercel.app/api/ingest) and `MT_SHARED_SECRET` (same value as `INGEST_SECRET`) from `pi/.env` (see `pi/.env.example`). It keeps working through Wi-Fi drops and sends the backlog when the link returns. The page shows OFFLINE after 5 seconds without data.

Dashboard routes: `/api/ingest` (POST, secret required), `/api/latest`, `/api/energy-history`, `/api/ab`, `/api/export` (CSV). The page shows pan and tilt on its 0-180 bars with 90 as the center (pan + 90, tilt + 45), and LDR counts scaled to 0-1023. Lux, temperature and humidity have no sensor on this build and show n/a. Run `cd web && npm test` for the logic checks.

## Image credits

The solar panel image on the dashboard is cropped and straightened from "Photovoltaic Solar Panel.jpg" by AcornToOak on Wikimedia Commons, licensed CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0). The photoresistor image is cropped from "LDR07 Light-dependent CdS photoresistor.jpg" by BenTheWikiMan on Wikimedia Commons, public domain.
