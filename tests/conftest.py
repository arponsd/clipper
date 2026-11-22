"""Shared pytest setup: every test gets its own empty data dir (settings.json + jobs/).

config.data_dir() reads AUTOCLIPPER_DATA_DIR on every call, so setting the env var is enough;
nothing in a test can touch the real ./data directory.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data))
    return data
