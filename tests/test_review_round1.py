"""Round-1 review fixes: red tests.

Findings under test (independent review, deleg_f3c9f556):
F1 cwd interpolated in shell double quotes -> $() executes (also breaks
   legitimate dirs containing $/backticks)
F2 dd chunk loop silently truncates; fallback returns UNDECODED base64
F3 cleanup() races execute(): respawn after cleanup leaks a live omp
F4 stderr=PIPE never drained -> pipe-full deadlock wedges the env
F5 stale _read_loop fail-all poisons new-generation requests
F6 overlay temp file leaks when Popen fails in __init__
F7 _chunks entries leak on incomplete chunk streams
"""
import base64
import itertools
import os
import subprocess
import threading

import pytest

from conftest import load_plugin

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="needs non-root for access checks")


class _FakeUUID:
    def __init__(self, h):
        self.hex = h


def _make_env(plugin, tmp_path):
    env = object.__new__(plugin.OmpRpcEnvironment)
    env.cwd = str(tmp_path)
    env.timeout = 30
    env.task_id = "test"
    env._model = None
    env._process = None
    env._overlay_path = None
    env._closed = False
    env._gen = 0
    env._pending = {}
    env._responses = {}
    env._chunks = {}
    env._ids = itertools.count()
    env._lock = threading.Lock()
    env._publish_live = lambda *a, **k: None
    env._finalize_live = lambda *a, **k: None
    return env


# ---------------------------------------------------------------- F1
def test_cwd_with_command_substitution_is_not_executed(plugin, tmp_path, monkeypatch):
    """A cwd containing $(...) must reach the wrapper literally, never
    expanded by the shell."""
    marker = tmp_path / "pwned"
    evil_cwd = f"{tmp_path}/x$(touch {marker})"

    def fake_request(method, timeout, **params):
        r = subprocess.run(["bash", "-c", params["command"]],
                           capture_output=True, text=True, timeout=10)
        return {"output": r.stdout, "exitCode": r.returncode}

    env = _make_env(plugin, tmp_path)
    env._request = fake_request
    env._execute_locked("true", evil_cwd, 30, None)
    assert not marker.exists(), "cwd command substitution executed"

    # legitimate dir with $ in the name still round-trips
    dollar_dir = tmp_path / "has$dollar"
    dollar_dir.mkdir()
    result = env._execute_locked("true", str(dollar_dir), 30, None)
    assert result.get("cwd_observed") is True
    assert result.get("cwd") == os.path.realpath(str(dollar_dir)) or \
        result.get("cwd") == str(dollar_dir)


# ---------------------------------------------------------------- F2
_PAYLOAD = b"hello world!!"  # 15 bytes -> clean 20-char b64 head


def _marker_output(plugin, monkeypatch):
    seq = iter([_FakeUUID("a" * 32), _FakeUUID("b" * 32)])
    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: next(seq))
    size_marker = f"__OMPSZ_{'a' * 12}__"
    cwd_marker = f"__OMPCW_{'b' * 12}__"
    head = base64.b64encode(_PAYLOAD).decode()
    # wrapper inline layout: SM<total>SM\n<head>\nCM<pwd>\n
    return (f"{size_marker}4000{size_marker}\n{head}\n"
            f"{cwd_marker}/nonexistent-dir-not-cwd\n")


def test_truncated_transfer_is_flagged(plugin, tmp_path, monkeypatch):
    """When dd chunks never arrive, output must be the DECODED head plus a
    visible truncation marker — never silent loss."""
    out = _marker_output(plugin, monkeypatch)

    def req(m, t, **p):
        if "dd if=" in p.get("command", ""):
            return {"output": "", "exitCode": 0}  # chunk lost
        return {"output": out, "exitCode": 0}

    env = _make_env(plugin, tmp_path)
    env._request = req
    result = env._execute_locked("true", str(tmp_path), 30, None)
    assert _PAYLOAD.decode() in result["output"], f"head not decoded: {result['output'][:80]!r}"
    assert "truncat" in result["output"].lower(), "no truncation marker"


def test_garbage_chunk_falls_back_to_decoded_head(plugin, tmp_path, monkeypatch):
    out = _marker_output(plugin, monkeypatch)
    calls = {"n": 0}

    def req(m, t, **p):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"output": out, "exitCode": 0}
        return {"output": "!!!!not-base64!!!!", "exitCode": 0}

    env = _make_env(plugin, tmp_path)
    env._request = req
    result = env._execute_locked("true", str(tmp_path), 30, None)
    assert _PAYLOAD.decode() in result["output"]
    assert "!!!!not-base64!!!!" not in result["output"]


class _FakeProc:
    """Stand-in for a live Popen that _kill_process can tear down."""
    pid = -1  # getpgid(-1) fails -> falls through to wait()/kill()

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


# ---------------------------------------------------------------- F3
def test_execute_after_cleanup_raises_closed(plugin, tmp_path):
    env = _make_env(plugin, tmp_path)
    env._spawn = lambda: None
    env._process = _FakeProc()
    env.cleanup()
    with pytest.raises(plugin._OmpRpcError) as ei:
        env.execute("true")
    assert "clos" in str(ei.value).lower()


def test_restart_after_cleanup_does_not_spawn(plugin, tmp_path):
    env = _make_env(plugin, tmp_path)
    spawns = {"n": 0}
    env._spawn = lambda: spawns.__setitem__("n", spawns["n"] + 1)
    env._process = _FakeProc()
    env.cleanup()
    env._restart()
    assert spawns["n"] == 0, "respawn after cleanup leaked a process"


# ---------------------------------------------------------------- F4
def test_stderr_drain_prevents_pipe_deadlock(plugin, tmp_path):
    """A child writing >64KB to stderr must not block when the drain runs."""
    proc = subprocess.Popen(["bash", "-c", "head -c 200000 /dev/zero >&2; exit 0"],
                            stderr=subprocess.PIPE)
    t = threading.Thread(target=plugin._drain_stderr, args=(proc,), daemon=True)
    t.start()
    try:
        rc = proc.wait(timeout=20)
    finally:
        t.join(timeout=5)
    assert rc == 0, "stderr pipe filled and blocked the child (deadlock)"


# ---------------------------------------------------------------- F5
def test_fail_all_is_scoped_to_spawn_generation(plugin, tmp_path):
    env = _make_env(plugin, tmp_path)
    ev_old, ev_new = threading.Event(), threading.Event()
    env._gen = 1
    env._pending = {"h0": (0, ev_old), "h1": (1, ev_new)}
    env._fail_all_pending(1, "stream closed")
    assert ev_new.is_set()
    assert not ev_old.is_set(), "stale-generation request poisoned"


# ---------------------------------------------------------------- F6
def test_no_overlay_leak_when_spawn_fails(plugin, tmp_path, monkeypatch):
    def overlay_count():
        return len([f for f in os.listdir(plugin.tempfile.gettempdir())
                    if f.startswith("hermes-omp-cfg-")])

    def fake_popen(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'omp'")

    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    n0 = overlay_count()
    with pytest.raises(plugin._OmpRpcError):
        plugin.OmpRpcEnvironment(cwd=str(tmp_path), timeout=30, task_id="t", model=None)
    assert overlay_count() == n0, "overlay temp file leaked on constructor failure"


# ---------------------------------------------------------------- F7
def test_chunk_state_is_bounded(plugin, tmp_path):
    env = _make_env(plugin, tmp_path)
    for i in range(200):
        env._chunks[f"cid{i}"] = {"parts": {}, "count": 99}
    env._handle_chunk({"chunkId": "new", "index": 0, "count": 1,
                       "data": base64.b64encode(b"x").decode()})
    assert len(env._chunks) <= 64
