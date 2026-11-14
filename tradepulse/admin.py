"""Admin CLI.

    python -m tradepulse.admin import-oms --db /path/to/skymatrix.db   # one-time copy of users + account owners
    python -m tradepulse.admin create-admin --username admin           # prompts for a password
"""

import argparse
import getpass
import sys

from .config import settings
from .store import Store
from .users import create_user, import_oms


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tradepulse.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-oms", help="copy users and account ownership from an OMS database (read-only)")
    imp.add_argument("--db", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--username", required=True)
    ca.add_argument("--name", default="Administrator")
    args = ap.parse_args(argv)
    store = Store(settings.DB_PATH)
    if args.cmd == "import-oms":
        print(import_oms(store, args.db))
        return 0
    pw = getpass.getpass("Password (min 8 chars): ")
    if pw != getpass.getpass("Repeat: "):
        print("passwords differ", file=sys.stderr)
        return 1
    create_user(store, args.username, args.name, "ADMIN", pw)
    print("created")
    return 0


if __name__ == "__main__":
    sys.exit(main())
