#!/usr/bin/env python3
"""Run a bounded backup operation while owning the governed publication lease."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Sequence

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


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guard knowledge backup with its publication lock.")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("child", nargs=argparse.REMAINDER)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    child = list(args.child)
    if child[:1] == ["--"]:
        child = child[1:]
    return run_backup(child)


if __name__ == "__main__":
    raise SystemExit(main())
