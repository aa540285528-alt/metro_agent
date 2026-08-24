from __future__ import annotations

import argparse
import sys
from getpass import getpass
from typing import Protocol

from metro_agent.auth.database import create_auth_session_factory
from metro_agent.auth.service import AuthService


class UserCreator(Protocol):
    def create_user(
        self, username: str, password: str, role: str, actor_user_id: int | None
    ): ...


def run_bootstrap(auth_service: UserCreator, username: str) -> int:
    try:
        password = getpass("Password: ")
        confirmation = getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("Error: password input was cancelled", file=sys.stderr)
        return 1

    if password != confirmation:
        print("Error: passwords do not match", file=sys.stderr)
        return 1

    try:
        auth_service.create_user(username, password, "admin", None)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Error: bootstrap failed; check authentication database", file=sys.stderr)
        return 1

    print("Administrator created successfully")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first administrator")
    parser.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    try:
        auth_service = AuthService(create_auth_session_factory())
    except Exception:
        print("Error: authentication database configuration failed", file=sys.stderr)
        return 1
    return run_bootstrap(auth_service, args.username)


if __name__ == "__main__":
    raise SystemExit(main())
