"""Validate the configured MongoDB connection.

Reads MONGO_MOCK / MONGO_URI / MONGO_DB from .env, connects, pings, ensures
indexes + seeds (roles, bootstrap admin), and prints collection counts.

    python check_db.py
"""
from dotenv import load_dotenv

load_dotenv()

import db


def main():
    print(f"mode = {'mongomock (in-memory)' if db.MONGO_MOCK else 'mongodb'}")
    if not db.MONGO_MOCK:
        print(f"uri  = {db._safe_uri()}")
    print(f"db   = {db.MONGO_DB}")
    try:
        db.get_db()  # connects + pings + ensures indexes/seed
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ connection FAILED: {e}")
        raise SystemExit(1)
    h = db.health()
    print("\n✓ connected" if h["connected"] else f"\n✗ not connected: {h['error']}")
    for name, count in h["collections"].items():
        print(f"   {name:<15} {count}")


if __name__ == "__main__":
    main()
