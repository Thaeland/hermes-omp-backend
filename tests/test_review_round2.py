"""Round-2 residual: F6 completion.

The overlay temp file must be removed on EVERY constructor failure path,
and no bare OSError may escape the constructor:
- PermissionError from Popen (npm prefix with a non-executable cli.ts —
  the known exit-126 failure mode)
- omp exiting at startup / missing ready frame (Popen succeeded, so the
  FileNotFoundError branch never runs)
"""
import os

import pytest

from conftest import load_plugin


def _overlay_count():
    return len([f for f in os.listdir(load_plugin().tempfile.gettempdir())
                if f.startswith("hermes-omp-cfg-")])


class _InstantExitProc:
    pid = -1
    returncode = 1
    stdin = None
    stderr = None
    stdout = iter(())  # lets _read_loop exit cleanly

    def poll(self):
        return 1

    def wait(self, timeout=None):
        return 1

    def kill(self):
        pass


def test_permission_error_wrapped_and_no_overlay_leak(plugin, tmp_path, monkeypatch):
    def fake_popen(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    n0 = _overlay_count()
    with pytest.raises(plugin._OmpRpcError) as ei:
        plugin.OmpRpcEnvironment(cwd=str(tmp_path), timeout=30, task_id="t", model=None)
    assert "npm" in str(ei.value).lower()
    assert _overlay_count() == n0, "overlay leaked on PermissionError"


def test_startup_exit_no_overlay_leak(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin.subprocess, "Popen",
                        lambda *a, **k: _InstantExitProc())
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    n0 = _overlay_count()
    with pytest.raises(plugin._OmpRpcError) as ei:
        plugin.OmpRpcEnvironment(cwd=str(tmp_path), timeout=30, task_id="t", model=None)
    assert "startup" in str(ei.value).lower()
    assert _overlay_count() == n0, "overlay leaked on startup exit"
