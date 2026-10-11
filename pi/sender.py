#!/usr/bin/env python3
"""
Pushes the tracker's samples to the Vercel dashboard over Wi-Fi.

tracker.py appends one JSON line per second to outbox.jsonl. This script tails that file
and POSTs new lines to /api/ingest with the x-shared-secret header. It only makes outbound
HTTPS requests. If the network drops, lines stay in outbox.jsonl and are sent in batches
when it returns (nothing is lost while this script is running; it remembers its place in
.sender_offset across restarts).

Run it in a second SSH session next to tracker.py:
  python sender.py
  python sender.py --from-start     # send everything already in outbox.jsonl
  python sender.py --dry-run        # print what would be sent, send nothing

Config (pi/.env, gitignored, or real environment variables):
  MT_API_URL=https://your-app.vercel.app/api/ingest
  MT_SHARED_SECRET=same value as INGEST_SECRET on Vercel

Standard library only, no pip install needed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTBOX = HERE / "outbox.jsonl"
OFFSET_FILE = HERE / ".sender_offset"

BATCH_MAX = 50
POLL_S = 1.0
TIMEOUT_S = 8.0
BACKOFF_MAX_S = 30.0
MAX_LINE_BYTES = 64 * 1024

def load_env() -> None:
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def read_offset(start_at_end: bool) -> int:
    size = OUTBOX.stat().st_size if OUTBOX.exists() else 0
    try:
        off = int(OFFSET_FILE.read_text().strip())
        return off if 0 <= off <= size else 0
    except (OSError, ValueError):
        return size if start_at_end else 0

def write_offset(off: int) -> None:
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(off))
    tmp.replace(OFFSET_FILE)

def read_new(offset: int) -> tuple[list[dict], int]:
    """Returns (parsed objects, new offset). Only whole lines are consumed."""
    if not OUTBOX.exists():
        return [], 0
    with OUTBOX.open("rb") as f:
        f.seek(offset)
        chunk = f.read(BATCH_MAX * 1024)
    items: list[dict] = []
    consumed = 0
    while len(items) < BATCH_MAX:
        nl = chunk.find(b"\n", consumed)
        if nl == -1:
            break
        line = chunk[consumed:nl]
        consumed = nl + 1
        if not line.strip() or len(line) > MAX_LINE_BYTES:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items, offset + consumed

def post(url: str, secret: str, samples: list[dict], ab: dict | None) -> None:
    body: dict = {"samples": samples}
    if ab is not None:
        body["ab_result"] = ab
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "x-shared-secret": secret},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        resp.read()

def main() -> int:
    ap = argparse.ArgumentParser(description="Send tracker samples to Vercel")
    ap.add_argument("--from-start", action="store_true", help="send the whole outbox first")
    ap.add_argument("--dry-run", action="store_true", help="print batches, send nothing")
    args = ap.parse_args()

    load_env()
    url = os.environ.get("MT_API_URL", "").strip()
    secret = os.environ.get("MT_SHARED_SECRET", "").strip()
    if not args.dry_run and (not url or not secret):
        print("Set MT_API_URL and MT_SHARED_SECRET in pi/.env (see .env.example).")
        return 1
    local = url.startswith(("http://localhost", "http://127.0.0.1"))
    if url and not url.startswith("https://") and not local:
        print("MT_API_URL must be https:// (the secret is sent in a header).")
        return 1

    if args.from_start and OFFSET_FILE.exists():
        OFFSET_FILE.unlink()
    offset = 0 if args.from_start else read_offset(start_at_end=True)
    print(f"Sending {OUTBOX} from byte {offset} to {url or '(dry run)'}")

    sent = 0
    backoff = 0.0
    last_report = 0.0
    try:
        while True:
            items, new_offset = read_new(offset)
            if new_offset < offset:
                offset = 0
                continue
            samples = [i for i in items if i.get("type") != "ab"]
            ab_items = [i for i in items if i.get("type") == "ab"]
            ab = ab_items[-1] if ab_items else None

            if samples or ab:
                if args.dry_run:
                    print(json.dumps({"samples": samples, "ab_result": ab})[:300])
                else:
                    try:
                        post(url, secret, samples, ab)
                    except urllib.error.HTTPError as e:
                        if e.code in (400, 413):

                            print(f"\nserver rejected batch ({e.code}), skipping it")
                        elif e.code == 401:
                            print("\n401: MT_SHARED_SECRET does not match INGEST_SECRET on Vercel")
                            backoff = BACKOFF_MAX_S
                            time.sleep(backoff)
                            continue
                        else:
                            backoff = min(BACKOFF_MAX_S, max(1.0, backoff * 2))
                            print(f"\nHTTP {e.code}, retrying in {backoff:.0f}s")
                            time.sleep(backoff)
                            continue
                    except (urllib.error.URLError, OSError) as e:
                        backoff = min(BACKOFF_MAX_S, max(1.0, backoff * 2))
                        print(f"\nnetwork down ({e}), retrying in {backoff:.0f}s")
                        time.sleep(backoff)
                        continue
                backoff = 0.0
                sent += len(samples)
                offset = new_offset
                write_offset(offset)
            elif new_offset > offset:
                offset = new_offset
                write_offset(offset)

            now = time.monotonic()
            if now - last_report >= 2.0:
                last_report = now
                print(f"\rsent {sent} samples", end="", flush=True)

            if not items:
                time.sleep(POLL_S)
    except KeyboardInterrupt:
        print(f"\nStopped. Sent {sent} samples.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
