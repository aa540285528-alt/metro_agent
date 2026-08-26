from __future__ import annotations

import os
from pathlib import Path


def configured_storage_path(variable: str, default: Path) -> Path:
    return Path(os.environ.get(variable, str(default)))
