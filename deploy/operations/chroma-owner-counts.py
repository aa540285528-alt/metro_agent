#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

import chromadb


def owner_counts(path: Path) -> list[tuple[str, str, int]]:
    client = chromadb.PersistentClient(path=str(path))
    counts: list[tuple[str, str, int]] = []
    for collection in client.list_collections():
        name = collection.name if hasattr(collection, "name") else str(collection)
        metadatas = client.get_collection(name).get(include=["metadatas"])[
            "metadatas"
        ]
        owners = Counter(
            str(metadata["user_id"])
            for metadata in metadatas
            if metadata and metadata.get("user_id") is not None
        )
        counts.extend((name, owner, count) for owner, count in owners.items())
    return sorted(counts)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: chroma-owner-counts.py memory DIRECTORY", file=sys.stderr)
        return 2
    label, directory = sys.argv[1:]
    if label != "memory":
        print("only the local memory Chroma store has per-user owner counts", file=sys.stderr)
        return 2
    for collection, owner, count in owner_counts(Path(directory)):
        print(f"{label}\t{collection}\t{owner}\t{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
