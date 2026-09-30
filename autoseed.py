"""
autoseed.py — Fill the check-in timeline automatically (fixes "Check-ins Recorded: 0").

Why the dashboard shows 0 check-ins on Vercel:
  * seed_db.py only inserts participants, never check-ins.
  * Vercel's filesystem is ephemeral, so anything seeded locally is gone in the deployment.

Fix: seed from data/demo_data.json whenever the checkins table is empty.
By default P402's Month 6 check-in (id 008) is HELD BACK, because you enter it live in the demo.
Set TRIALGUARD_HOLD_BACK="" to seed everything, or e.g. "P402:008,P209:005" to hold back more.
"""
import json
import os
from pathlib import Path

import db

DATA_FILE = Path(__file__).parent / "data" / "demo_data.json"


def seed_if_empty() -> None:
    if db.get_stats().get("checkins", 0) > 0:
        return
    held = {h.strip() for h in os.environ.get("TRIALGUARD_HOLD_BACK", "P402:008").split(",") if h.strip()}
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for p in data["participants"]:
        db.upsert_participant(p["id"], p["display_name"], p["trial_id"], p["status"])
        for ci in p["checkins"]:
            if f"{p['id']}:{ci['checkin_id']}" in held:
                continue
            db.save_checkin(
                checkin_id=f"checkin:{p['id']}:{ci['checkin_id']}",
                participant_id=p["id"],
                occurred_at=ci["occurred_at"],
                raw_text=ci["text"],
            )
