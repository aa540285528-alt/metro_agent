#!/usr/bin/env python3
"""Run a bounded backup operation while owning the governed publication lease."""

from __future__ import annotations

import argparse
import hmac
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from metro_agent.knowledge.publication_lock import PublicationLock


PUBLICATION_LOCK_KEY = "knowledge:publication"


def _redis_client():
    import redis

    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )


def run_backup(command: Sequence[str] = ()) -> int:
    """Keep the same token-checked lease for the complete child operation."""
    with PublicationLock(_redis_client(), PUBLICATION_LOCK_KEY) as lock:
        lock.assert_held()
        if not command:
            return 0
        return subprocess.run(list(command), check=False).returncode


def hold_lock(*, token_file: Path, release_file: Path) -> int:
    """Hold one renewable lease until the coordinating host releases it.

    The host must stop the services and create the volume archives itself.  It
    receives this exact lock token through a 0700 backup directory and proves
    ownership with ``verify-token`` before every knowledge-critical boundary.
    """
    with PublicationLock(_redis_client(), PUBLICATION_LOCK_KEY) as lock:
        try:
            with token_file.open("x", encoding="utf-8") as output:
                output.write(f"{lock.token}\n")
        except OSError as exc:
            raise RuntimeError("could not write publication lock token") from exc

        while not release_file.exists():
            lock.assert_held()
            time.sleep(0.2)
        lock.assert_held()
    return 0


def verify_token(token: str) -> int:
    """Fail closed unless ``token`` is the lease currently held in Redis."""
    try:
        current = _redis_client().get(PUBLICATION_LOCK_KEY)
    except Exception as exc:
        raise RuntimeError("could not verify publication lock token") from exc
    if not isinstance(current, str) or not hmac.compare_digest(current, token):
        raise RuntimeError("publication lock token is no longer held")
    return 0


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guard knowledge backup with its publication lock.")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("child", nargs=argparse.REMAINDER)
    hold = commands.add_parser("hold")
    hold.add_argument("--token-file", type=Path, required=True)
    hold.add_argument("--release-file", type=Path, required=True)
    verify = commands.add_parser("verify-token")
    verify.add_argument("--token", required=True)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    if args.command == "hold":
        return hold_lock(token_file=args.token_file, release_file=args.release_file)
    if args.command == "verify-token":
        return verify_token(args.token)
    child = list(args.child)
    if child[:1] == ["--"]:
        child = child[1:]
    return run_backup(child)


if __name__ == "__main__":
    raise SystemExit(main())
