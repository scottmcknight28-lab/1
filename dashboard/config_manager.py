"""Thread-safe config.yaml read/write for the dashboard."""

import threading
from pathlib import Path
from typing import Any

import yaml

_lock = threading.Lock()


def read_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def write_config(path: str, data: dict) -> None:
    with _lock:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def update_bidding(path: str, fields: dict[str, Any]) -> None:
    """Merge `fields` into the bidding section and save."""
    cfg = read_config(path)
    cfg.setdefault("bidding", {}).update(fields)
    write_config(path, cfg)
