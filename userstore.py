"""YAML-backed user store for the DPD Early-Warning app.

Users live in a YAML file (default: users.yaml next to this module) so the app
needs only READ access to your MySQL database. Passwords are hashed with
PBKDF2-SHA256 (stdlib only). Each user has a role: "admin" or "viewer".

Copied from sql-template-runner/userstore.py.

File shape:
    users:
      - username: admin
        password_hash: pbkdf2_sha256$...
        role: admin
        created_at: "2026-06-26 10:00:00"
"""
import hashlib
import hmac
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Project timezone — IST (fixed +5:30, no DST). Matches mongostore.IST.
IST = timezone(timedelta(hours=5, minutes=30))

import yaml

USERS_FILE = Path(os.getenv("USERS_FILE") or (Path(__file__).resolve().parent / "users.yaml"))
PBKDF2_ROUNDS = 200_000
ROLES = ("admin", "viewer")

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
def _read() -> dict:
    if not USERS_FILE.exists():
        return {"users": []}
    data = yaml.safe_load(USERS_FILE.read_text()) or {}
    if not isinstance(data.get("users"), list):
        data["users"] = []
    return data


def _write(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    tmp.replace(USERS_FILE)  # atomic-ish replace


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def list_users() -> list:
    """All users (includes password_hash — callers must strip before returning to clients)."""
    return _read()["users"]


def get_user(username: str):
    for u in _read()["users"]:
        if u.get("username") == username:
            return u
    return None


def authenticate(username: str, password: str):
    u = get_user((username or "").strip())
    if u and verify_password(password, u.get("password_hash", "")):
        return u
    return None


def count_admins(users: list) -> int:
    return sum(1 for u in users if u.get("role") == "admin")


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------
def add_user(username: str, password: str, role: str = "viewer") -> None:
    username = (username or "").strip()
    if not username:
        raise ValueError("Username is required")
    if not password:
        raise ValueError("Password is required")
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}")
    with _lock:
        data = _read()
        if any(u.get("username") == username for u in data["users"]):
            raise ValueError(f"User '{username}' already exists")
        data["users"].append({
            "username": username,
            "password_hash": hash_password(password),
            "role": role,
            "created_at": _now(),
        })
        _write(data)


def set_password(username: str, password: str) -> None:
    if not password:
        raise ValueError("Password is required")
    with _lock:
        data = _read()
        for u in data["users"]:
            if u.get("username") == username:
                u["password_hash"] = hash_password(password)
                _write(data)
                return
        raise ValueError(f"User '{username}' not found")


def set_role(username: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Role must be one of {ROLES}")
    with _lock:
        data = _read()
        target = next((u for u in data["users"] if u.get("username") == username), None)
        if target is None:
            raise ValueError(f"User '{username}' not found")
        if target.get("role") == "admin" and role != "admin" and count_admins(data["users"]) <= 1:
            raise ValueError("Cannot demote the last remaining admin")
        target["role"] = role
        _write(data)


def delete_user(username: str) -> None:
    with _lock:
        data = _read()
        target = next((u for u in data["users"] if u.get("username") == username), None)
        if target is None:
            raise ValueError(f"User '{username}' not found")
        if target.get("role") == "admin" and count_admins(data["users"]) <= 1:
            raise ValueError("Cannot delete the last remaining admin")
        data["users"] = [u for u in data["users"] if u.get("username") != username]
        _write(data)
