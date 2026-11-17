"""Windows-compat changes: red tests.

W1 chunk transfer must use portable utilities (tail|head, present in omp's
  pi-builtins) instead of dd (absent from pi-builtins)
W2 spawn on Windows: resolve omp via shutil.which, wrap .cmd shims with
  cmd /c, use CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW instead of
  start_new_session
W3 teardown on Windows: taskkill /T /F instead of os.killpg
"""
import threading

import pytest

from conftest import load_plugin


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
    env._stderr_tail = []
    env._ids = __import__("itertools").count()
    env._ready = threading.Event()
    env._lock = threading.Lock()
    env._publish_live = lambda *a, **k: None
    env._finalize_live = lambda *a, **k: None
    return env


# ---------------------------------------------------------------- W1
def test_chunk_reads_use_portable_utilities(plugin, tmp_path, monkeypatch):
    import base64
    seq = iter([_FakeUUID("a" * 32), _FakeUUID("b" * 32)])
    monkeypatch.setattr(plugin.uuid, "uuid4", lambda: next(seq))
    size_marker = f"__OMPSZ_{'a' * 12}__"
    cwd_marker = f"__OMPCW_{'b' * 12}__"
    payload = b"hello world!!"
    head = base64.b64encode(payload).decode()
    out = f"{size_marker}4000{size_marker}\n{head}\n{cwd_marker}/nonexistent-x\n"

    chunk_cmds = []

    def req(m, t, **p):
        cmd = p.get("command", "")
        if "tail -c +" in cmd:
            chunk_cmds.append(cmd)
            return {"output": "", "exitCode": 0}
        return {"output": out, "exitCode": 0}

    env = _make_env(plugin, tmp_path)
    env._request = req
    env._execute_locked("true", str(tmp_path), 30, None)
    assert chunk_cmds, "no chunk read issued (tail/head form)"
    assert not any("dd " in c for c in chunk_cmds)


# ---------------------------------------------------------------- W2
class _ReadyProc:
    pid = 4242

    def __init__(self, ready):
        self._ready = ready
        self.stdin = None
        self.stdout = iter(())
        self.stderr = None

    def poll(self):
        return None


def test_spawn_windows_uses_which_cmd_shim_and_creationflags(plugin, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "IS_WINDOWS", True)
    monkeypatch.setattr(plugin.shutil, "which",
                        lambda name: r"C:\node\omp.cmd")

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        env._ready.set()
        return _ReadyProc(env._ready)

    env = _make_env(plugin, tmp_path)
    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    env._spawn()

    assert captured["cmd"][:3] == ["cmd", "/c", r"C:\node\omp.cmd"], captured["cmd"]
    assert "start_new_session" not in captured["kw"]
    flags = captured["kw"].get("creationflags", 0)
    assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP missing"
    assert flags & 0x08000000, "CREATE_NO_WINDOW missing"


def test_spawn_posix_unchanged(plugin, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "IS_WINDOWS", False)
    monkeypatch.setattr(plugin.shutil, "which", lambda name: "/usr/bin/omp")

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        env._ready.set()
        return _ReadyProc(env._ready)

    env = _make_env(plugin, tmp_path)
    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    env._spawn()
    assert captured["cmd"][0] == "/usr/bin/omp"
    assert captured["kw"].get("start_new_session") is True


# ---------------------------------------------------------------- W3
class _KillableProc:
    pid = 4242

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_kill_process_windows_uses_taskkill(plugin, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "IS_WINDOWS", True)
    monkeypatch.setattr(plugin.subprocess, "run",
                        lambda args, **k: calls.append(args))
    env = _make_env(plugin, tmp_path)
    env._process = _KillableProc()
    env._kill_process()
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
