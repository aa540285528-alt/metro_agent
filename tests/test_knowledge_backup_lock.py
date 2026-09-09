from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "operations" / "with-knowledge-publication-lock.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("knowledge_backup_lock", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TokenRenewingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def eval(self, *args: object) -> int:
        self.calls.append(args)
        return 1


def test_verify_token_uses_atomic_compare_and_pexpire(monkeypatch) -> None:
    module = _load_module()
    redis = _TokenRenewingRedis()
    monkeypatch.setattr(module, "_redis_client", lambda: redis)

    assert module.verify_token("release-token") == 0
    script, count, key, token, ttl = redis.calls[0]
    assert "pexpire" in str(script).lower()
    assert count == 1
    assert key == "knowledge:publication"
    assert token == "release-token"
    assert ttl == 900_000
