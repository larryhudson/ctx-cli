"""Sync state tracking for incremental ingestion."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ctx.config import get_data_dir
from ctx.models import Source

SYNC_STATE_FILENAME = "sync_state.json"


def _get_sync_state_path() -> Path:
    return get_data_dir() / SYNC_STATE_FILENAME


def _read_state() -> dict[str, str]:
    path = _get_sync_state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_state(state: dict[str, str]) -> None:
    path = _get_sync_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def get_last_synced_at(source: Source) -> datetime | None:
    """Read the last sync timestamp for a source."""
    state = _read_state()
    iso_str = state.get(source.value)
    if iso_str is None:
        return None
    return datetime.fromisoformat(iso_str)


def set_last_synced_at(source: Source, timestamp: datetime) -> None:
    """Write the last sync timestamp for a source."""
    state = _read_state()
    state[source.value] = timestamp.isoformat()
    _write_state(state)


def get_all_sync_state() -> dict[str, datetime]:
    """Read all sync timestamps."""
    state = _read_state()
    return {key: datetime.fromisoformat(value) for key, value in state.items()}
