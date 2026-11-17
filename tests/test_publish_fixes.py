"""Regression tests for the two publish-blocking bugs.

1. The cwd marker from omp's RPC output was accepted on a bare ``isdir``
   check, so a stray ``/root`` report (isdir=True, access=False for the
   gateway user) poisoned the session cwd — every later call then failed at
   dispatch. Acceptance must use the same accessibility test as spawn
   (``_usable_cwd``: isdir AND os.access R_OK|X_OK).

2. A missing ``omp`` binary raised a bare ``FileNotFoundError`` from Popen
   at tool dispatch, blinding every execution path with an opaque traceback
   (this is exactly the post-``hermes update`` outage). It must surface as
   ``_OmpRpcError`` naming the remediation.
"""
import os
import subprocess
import types

import pytest

from conftest import load_plugin

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="needs non-root for access checks")


class _FakeUUID:
    def __init__(self, h):
        self.hex = h


def _make_env(plugin, tmp_path):
    """An OmpRpcEnvironment with the process machinery stubbed out."""
    env = object.__new__(plugin.OmpRpcEnvironment)
    env.cwd = str(tmp_path)
    env.timeout = 30
    env.task_id = "test"
    env._model = None
    env._process = None
    env._overlay_path = None
    import threading
    env._lock = threading.Lock()
    env._publish_live = lambda *a, **k: None
    env._finalize_live = lambda *a, **k: None
    return env


def test_cwd_marker_rejects_inaccessible_dir(plugin, tmp_path, monkeypatch):
    """/root is a directory but not enterable by the gateway user: the
    marker must be rejected and the session cwd left untouched."""
    assert os.path.isdir("/root") and not os.access("/root", os.R_OK | os.X_OK)

    seq = iter([_FakeUUID("a" * 32), _FakeUUID("b" * 32)])
    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: next(seq))
    cwd_marker = f"__OMPCW_{'b' * 12}__"

    env = _make_env(plugin, tmp_path)
    env._request = lambda *a, **k: {"output": f"some output\n{cwd_marker}/root\n",
                                     "exitCode": 0}
    result = env._execute_locked("true", str(tmp_path), 30, None)

    assert "cwd_observed" not in result, f"inaccessible cwd leaked: {result}"
    assert env.cwd == str(tmp_path)


def test_cwd_marker_accepts_accessible_dir(plugin, tmp_path, monkeypatch):
    """Positive control: a real, enterable directory still propagates."""
    target = tmp_path / "target"
    target.mkdir()
    seq = iter([_FakeUUID("a" * 32), _FakeUUID("b" * 32)])
    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: next(seq))
    cwd_marker = f"__OMPCW_{'b' * 12}__"

    env = _make_env(plugin, tmp_path)
    env._request = lambda *a, **k: {"output": f"out\n{cwd_marker}{target}\n",
                                     "exitCode": 0}
    result = env._execute_locked("true", str(tmp_path), 30, None)

    assert result.get("cwd_observed") is True
    assert result.get("cwd") == str(target)
    assert env.cwd == str(target)


def test_spawn_missing_binary_gives_remediation(plugin, tmp_path, monkeypatch):
    """A missing omp binary must raise _OmpRpcError with install guidance,
    not a bare FileNotFoundError traceback."""
    def fake_popen(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'omp'")

    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    with pytest.raises(plugin._OmpRpcError) as ei:
        plugin.OmpRpcEnvironment(cwd=str(tmp_path), timeout=30,
                                 task_id="t", model=None)
    msg = str(ei.value).lower()
    assert "not found" in msg and "npm" in msg
