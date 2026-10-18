"""Runtime configuration and shared input validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("MEMORY_MESH_DB", str(ROOT / "memory_mesh.db")))
GRAPH_NAME = os.environ.get("FALKORDB_GRAPH", "memory_mesh_v2")
FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "127.0.0.1")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6380"))
HOST = os.environ.get("MEMORY_MESH_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEMORY_MESH_PORT", "8000"))
MAX_REQUEST_BYTES = 1_000_000
MAX_ID_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 4_000
MAX_MEMORY_LENGTH = 8_000


def clean_text(name: str, value: Any, *, max_length: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} cannot be blank")
    if len(value) > max_length:
        raise ValueError(f"{name} must be {max_length} characters or fewer")
    return value
