"""Admin CLI for DPD Early-Warning users (MongoDB).

Bootstrap/manage accounts. With MONGO_MOCK=true the data is in-memory per
process, so the running server seeds its own admin (admin/admin123) — use this
CLI against a real MongoDB (MONGO_MOCK=false, MONGO_URI=...).

Examples:
    python manage_users.py add alice --role operator
    python manage_users.py list
    python manage_users.py role alice admin
    python manage_users.py passwd alice
    python manage_users.py delete alice
"""
import argparse
import getpass
import sys

import rbac
import mongostore as store

ROLES = list(rbac.ROLE_PERMISSIONS)


def _prompt_password() -> str:
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Confirm password: "):
        print("✗ passwords do not match")
        sys.exit(1)
    return pw


def main():
    parser = argparse.ArgumentParser(description="Manage DPD Early-Warning users (MongoDB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a user")
    p_add.add_argument("username")
    p_add.add_argument("--password")
    p_add.add_argument("--role", choices=ROLES, default="viewer")

    sub.add_parser("list", help="list users")

    p_pw = sub.add_parser("passwd", help="reset a user's password")
    p_pw.add_argument("username")
    p_pw.add_argument("--password")

    p_role = sub.add_parser("role", help="change a user's role")
    p_role.add_argument("username")
    p_role.add_argument("role", choices=ROLES)

    p_del = sub.add_parser("delete", help="delete a user")
    p_del.add_argument("username")

    args = parser.parse_args()
    try:
        if args.cmd == "add":
            store.add_user(args.username, args.password or _prompt_password(), args.role)
            print(f"✓ added '{args.username}' ({args.role})")
        elif args.cmd == "list":
            users = store.list_users()
            if not users:
                print("(no users yet)")
            for u in users:
                print(f"  • {u['username']:<20} {u.get('role','viewer'):<9} created {u.get('created_at','')}")
        elif args.cmd == "passwd":
            store.set_password(args.username, args.password or _prompt_password())
            print(f"✓ password updated for '{args.username}'")
        elif args.cmd == "role":
            store.set_role(args.username, args.role)
            print(f"✓ '{args.username}' is now {args.role}")
        elif args.cmd == "delete":
            store.delete_user(args.username)
            print(f"✓ deleted '{args.username}'")
    except Exception as e:  # noqa: BLE001
        print(f"✗ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
