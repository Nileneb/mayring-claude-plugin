"""Tests für hooks/_device.py — Device↔Cloud-Kanal (#5)."""
import importlib
import os
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(_HOOKS))


@pytest.fixture
def device(tmp_path, monkeypatch):
    """_device-Modul frisch importiert mit tmp-Pfaden (kein echtes ~/.config)."""
    mod = importlib.import_module("_device")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_DEVICE_ID_FILE", tmp_path / "device_id")
    monkeypatch.setattr(mod, "_JWT_FILE", tmp_path / "hook.jwt")
    return mod


def test_device_id_generates_and_persists(device):
    first = device.device_id()
    assert first.startswith("dev_")
    # zweiter Aufruf liest die persistierte id → stabil
    assert device.device_id() == first
    assert (device._DEVICE_ID_FILE).read_text().strip() == first


def test_device_headers_carry_device_id(device):
    headers = device.device_headers()
    assert headers["X-Device-Id"] == device.device_id()


def test_capabilities_default_read_only(device, monkeypatch):
    monkeypatch.delenv("PI_WORKER_CAPABILITIES", raising=False)
    assert device.capabilities() == ["read"]


def test_capabilities_from_env(device, monkeypatch):
    monkeypatch.setenv("PI_WORKER_CAPABILITIES", "read, write")
    assert device.capabilities() == ["read", "write"]


def test_reporting_is_best_effort_without_token(device):
    # Kein JWT-File → kein Token → Calls dürfen NICHT raisen, nichts senden.
    assert not device._JWT_FILE.exists()
    device.register_device()
    device.report_hook_event("SessionStart")
    device.heartbeat()  # alle no-op, kein Fehler


def test_post_swallows_network_errors(device, monkeypatch):
    device._JWT_FILE.write_text("fake-token")

    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(device.urllib.request, "urlopen", boom)
    # darf trotz Netzfehler nicht raisen (Telemetrie blockiert Hook nie)
    device.report_hook_event("Stop")
    device.register_device()
