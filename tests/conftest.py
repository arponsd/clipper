"""Shared pytest setup.

Autouse fixtures for every test:
- its own empty data dir (settings.json + jobs/). config.data_dir() reads AUTOCLIPPER_DATA_DIR on every
  call, so nothing in a test can touch the real ./data directory;
- a suite-wide guard against real services (TESTS-2): no ANTHROPIC_* env vars (config.load_dotenv() puts the
  project's real key into os.environ at import), HOME/XDG_CONFIG_HOME point at tmp_path (no real `ant`
  profile), the pipeline's real-client factories raise, and sockets may only connect to loopback.
  A test that really needs a factory must patch it with an explicit fake, or carry the `real_clients` marker.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(data))
    return data


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX path or other non-IP address: local by definition
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # any other host name would need DNS + network


@pytest.fixture(autouse=True)
def _no_real_services(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("ANTHROPIC_"):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    if request.node.get_closest_marker("real_clients") is None:
        from autoclipper import pipeline

        def real_client(_settings):
            raise RuntimeError("real client in tests")

        for name in ("_make_llm", "_make_voicebox", "_make_transcriber"):
            monkeypatch.setattr(pipeline, name, real_client)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(sock: socket.socket, address: object) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise RuntimeError(f"network access in tests is blocked: connect to {address!r}")

    def guarded_connect(self: socket.socket, address: object):
        _check(self, address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object):
        _check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
