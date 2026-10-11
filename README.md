# Balcony Solar Optimizer (Micro-Tracker) - hackPHS 2026, theme "Into the Skies"

A dual-axis (pan + tilt) mount for one 178 x 142 x 2 mm KEJNYT 12 V 3.6 W panel. Four edge LDRs steer it. A Raspberry Pi 5 is the brain (no Arduino boards), an ADS1115 reads the LDRs, an INA219 measures panel output, and a Vercel dashboard shows it. Servos are the kit MG90S/SG90.

## Folders
- pi : tracker.py (sensors, control, CSV log, outbox) and sender.py (pushes the outbox to Vercel).
- web : the Helios dashboard for Vercel (static page in public/, API routes in api/, Neon schema and seed in scripts/).

## Electronics decisions made
- Servo power: any 5 V source of about 2 A that is not the Pi (the MB102 module with a 9 V wall adapter also works; a 9 V battery does not). Common ground to a Pi GND pin.
- LDR: GL5528 (in the kit, matches a 10k divider). Never mix 5528 and 5539. If buying, buy 20-50, measure under one lamp, keep the 4 best-matched.
- Software limits: pan +/-80, tilt 15 to 85 degrees.

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
