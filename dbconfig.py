"""Read-only MySQL connection settings for the DPD Early-Warning app.

Stored in db_config.yaml (managed in-app by admins) with .env values (DB_HOST,
DB_PORT, …) as fallback. The database stays read-only — the app only issues the
SELECT lookup in lookup.sql — but the password must be stored in recoverable
form so the app can authenticate (exactly like a .env file). The file is
git-ignored and written with 0600 permissions.

Adapted from sql-template-runner/dbconfig.py.
"""
import os
from pathlib import Path

import yaml

DBCONFIG_FILE = Path(os.getenv("DBCONFIG_FILE") or (Path(__file__).resolve().parent / "db_config.yaml"))
FIELDS = ("host", "port", "user", "password", "database")


def _env_defaults() -> dict:
    # user/password default to the SHARED LOS credentials — the consent DB is the same
    # LOS/Engrow MySQL as the portfolio, only different tables (per Prayaan). So you set
    # DB_HOST/DB_PORT/DB_NAME (+ the consent query) and the creds are reused automatically.
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER") or os.getenv("LOS_USER") or "root",
        "password": os.getenv("DB_PASSWORD") or os.getenv("LOS_PASS") or "",
        "database": os.getenv("DB_NAME") or "",
    }


def is_configured() -> bool:
    return DBCONFIG_FILE.exists()


def has_connection() -> bool:
    """A real MySQL target is set — an explicit DB_HOST (or a saved db_config.yaml). The
    localhost fallback does NOT count as configured, so a filled consent query can't silently
    try to hit 127.0.0.1. Credentials themselves fall back to the shared LOS creds."""
    return is_configured() or bool(os.getenv("DB_HOST"))


def load() -> dict:
    """Effective settings: db_config.yaml on top of .env defaults."""
    cfg = _env_defaults()
    if DBCONFIG_FILE.exists():
        data = yaml.safe_load(DBCONFIG_FILE.read_text()) or {}
        for k in FIELDS:
            if data.get(k) is not None:
                cfg[k] = data[k]
    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        cfg["port"] = 3306
    return cfg


def save(host: str, port, user: str, password: str, database: str) -> None:
    data = {
        "host": (host or "").strip() or "127.0.0.1",
        "port": int(port or 3306),
        "user": (user or "").strip(),
        "password": password or "",
        "database": (database or "").strip(),
    }
    DBCONFIG_FILE.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    try:
        os.chmod(DBCONFIG_FILE, 0o600)
    except OSError:
        pass


def mysql_kwargs() -> dict:
    """Keyword args for pymysql.connect()."""
    c = load()
    return {
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "password": c["password"],
        "database": c["database"] or None,
        "charset": "utf8mb4",
        "connect_timeout": 10,
    }


def public() -> dict:
    """Settings safe to send to the client — never the raw password."""
    c = load()
    return {
        "host": c["host"],
        "port": c["port"],
        "user": c["user"],
        "database": c["database"],
        "password_set": bool(c["password"]),
        "configured": is_configured(),
    }
