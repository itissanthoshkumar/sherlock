"""Mark zombie cycles (RUNNING/COLLECTING whose thread died with the server)
as ERROR so a new cycle can start. Safe: only touches non-terminal cycles."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "vendor"))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import db  # noqa: E402

d = db.get_db()
r = d.cycles.update_many(
    {"status": {"$in": ["RUNNING", "COLLECTING"]}},
    {"$set": {"status": "ERROR", "error": "Interrupted: server restarted mid-cycle"}},
)
print(f"marked {r.modified_count} stale cycle(s) as ERROR")
for c in d.cycles.find({}, {"_id": 0, "id": 1, "month": 1, "status": 1}).sort("id", -1).limit(5):
    print(f"  cycle {c['id']} · {c.get('month')} · {c['status']}")
