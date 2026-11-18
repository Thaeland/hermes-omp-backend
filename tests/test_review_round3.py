"""Round-3 adversarial review findings on d77f511.

F1 MED: b64 spill file created by shell redirection -> 0644 world-readable
    copy of full command output in TMPDIR.
F2 MED: malformed env_passthrough (YAML bool/int) bricks every spawn.
F3 LOW: _private_write follows planted symlinks (O_TRUNC without O_NOFOLLOW).
F4 LOW: case-insensitive allowlist match over-copies on POSIX.
F5 LOW: non-string model config crashes spawn.
F7 LOW: tmp_link name collides between concurrent tasks in one process.
"""
import json
import os
import re
import stat

import pytest


class FakeCtx:
    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)


# --- F2: malformed passthrough must degrade, not brick -------------------------

@pytest.mark.parametrize("bad", [True, 5, 3.5, {"A": "x"}, ["ok", 7, None]])
def test_malformed_passthrough_never_raises(plugin, monkeypatch, bad):
    monkeypatch.setattr(plugin, "_PLUGIN_CTX", FakeCtx({"env_passthrough": bad}))
    env = plugin._child_env()  # must not raise on ANY config shape
    assert isinstance(env, dict)
    assert "A" not in env      # dict keys must NOT pass through


# --- F4: POSIX allowlist match is case-sensitive --------------------------------

def test_posix_allowlist_case_sensitive(plugin, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX-only: Windows env block is case-insensitive by design")
    monkeypatch.setenv("Home", "/sneaky")
    monkeypatch.setenv("Tmpdir", "/sneaky")
    monkeypatch.setenv("HOME", "/real")
    env = plugin._child_env()
    assert "Home" not in env
    assert "Tmpdir" not in env
    assert env["HOME"] == "/real"


# --- F5: non-string model config is ignored, not spawned ------------------------

def test_model_non_string_ignored(plugin, monkeypatch):
    captured = {}
    monkeypatch.setattr(plugin, "OmpRpcEnvironment",
                        lambda **kw: captured.update(kw) or "stub")
    monkeypatch.setattr(plugin, "_PLUGIN_CTX", FakeCtx({"model": 12345}))
    monkeypatch.delenv("HERMES_OMP_MODEL", raising=False)
    plugin.OmpProvider().create_environment(cwd="/tmp", timeout=5)
    assert captured["model"] is None


# --- F3: private write refuses to follow a planted symlink -----------------------

def test_private_write_refuses_symlink(plugin, tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("precious")
    link = tmp_path / "last.json"
    os.symlink(str(target), str(link))
    with pytest.raises(OSError):
        plugin._private_write(str(link), '{"evil": 1}')
    assert target.read_text() == "precious"  # untouched


def test_finalize_live_skips_symlinked_meta(plugin, monkeypatch, tmp_path):
    """A planted last.json symlink must not be read through / echoed back."""
    import builtins
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    live = plugin._live_dir()
    os.makedirs(live, exist_ok=True)
    victim = tmp_path / "victim.json"
    victim.write_text('{"attacker": "payload"}')
    meta_path = os.path.join(live, "last.json")
    os.symlink(str(victim), meta_path)
    opened = []
    real_open = builtins.open
    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    spool = tmp_path / "spool"
    spool.write_text("out")
    env = object.__new__(plugin.OmpRpcEnvironment)
    env.task_id = "t"
    env._process = None
    env._finalize_live(str(spool), 0, 0)  # must not raise
    assert meta_path not in opened, "finalize read THROUGH the planted symlink"
    assert victim.read_text() == '{"attacker": "payload"}'  # not rewritten


# --- F1: b64 spill file is private from the moment the shell sees it -------------

def test_b64_spill_precreated_private(plugin, tmp_path):
    """The parent must create the b64 target (mkstemp, 0600) BEFORE the shell
    redirection runs; redirection into an existing file preserves its perms."""
    env = object.__new__(plugin.OmpRpcEnvironment)
    env.task_id = "b64-perm"
    env._process = None
    env._lock = __import__("threading").Lock()
    env._closed = False
    captured = {}

    class Sentinel(Exception):
        pass

    def fake_request(kind, timeout, **kw):
        cmd = kw["command"]
        m = re.search(r"base64 -w0 \S+ > (\S+)", cmd)
        assert m, "wrapper shape changed; update this probe"
        b64 = json.loads(m.group(1))
        captured["exists"] = os.path.exists(b64)
        captured["mode"] = stat.S_IMODE(os.stat(b64).st_mode) if captured["exists"] else None
        raise Sentinel()

    env._request = fake_request
    old_umask = os.umask(0o022)
    try:
        with pytest.raises(Sentinel):
            env._execute_locked("echo hi", str(tmp_path), 5, None)
    except Exception as e:
        if not isinstance(e, Sentinel):
            # _execute_locked may wrap; the probe already ran if captured filled
            if not captured:
                raise
    finally:
        os.umask(old_umask)
    assert captured.get("exists"), "b64 file not pre-created by the parent"
    assert captured["mode"] == 0o600, f"b64 spill is group/other-readable: {oct(captured['mode'])}"


# --- F7: temp symlink name unique per thread -------------------------------------

def test_tmp_link_unique_per_thread(plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = []
    real_symlink = os.symlink

    def spy(src, dst):
        seen.append(dst)
        return real_symlink(src, dst)

    monkeypatch.setattr(os, "symlink", spy)
    env = object.__new__(plugin.OmpRpcEnvironment)
    env.task_id = "link-uniq"
    env._process = None
    spool = tmp_path / "s1"
    spool.write_text("x")
    env._publish_live(str(spool), "c1", str(tmp_path))
    env2 = object.__new__(plugin.OmpRpcEnvironment)
    env2.task_id = "link-uniq"
    env2._process = None
    import threading
    t = threading.Thread(target=env2._publish_live, args=(str(spool), "c2", str(tmp_path)))
    t.start(); t.join()
    assert len(seen) == 2
    assert seen[0] != seen[1], "temp symlink name collides across concurrent publishes"
