"""Manage reviewer accounts.

    python -m src.backend.users add alice        # prompts for a password
    docker compose exec backend python -m src.backend.users add alice
"""

import argparse
import asyncio
import getpass

from src.backend.auth import hash_password
from src.backend.config import load_settings
from src.backend.db import DB


async def _add(username: str, password: str) -> None:
    db = DB(load_settings().database_url, max_size=1)
    await db.open()
    try:
        await db.create_user(username, hash_password(password))
    finally:
        await db.close()


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("username")
    a = p.parse_args()
    pw = getpass.getpass(f"password for {a.username}: ")
    if len(pw) < 8 or pw != getpass.getpass("again: "):
        raise SystemExit("passwords must match and be at least 8 characters")
    asyncio.run(_add(a.username, pw))
    print(f"created {a.username}")


if __name__ == "__main__":
    main()
