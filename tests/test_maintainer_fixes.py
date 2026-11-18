"""Catalog-review fixes: child-env minimization, profile-aware private live dir,
model from plugin config.

Maintainer findings (PR #156829 review at 3323c8fb):
1. Popen for omp passed no env=, so omp and every model-run command saw
   Hermes' full environment including provider/bot keys.
2. ~/.hermes/omp-live/ was hardcoded (ignoring the active profile) and world-
   readable by default perms while holding command text + full last output.
3. (recommended) model read from plugin config rather than HERMES_OMP_MODEL.
"""
import json
import os
import stat
import subprocess

import pytest


class FakeCtx:
    """Duck-type of the plugin context's config surface."""

    def __init__(self, config=None):
        self._config = config or {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)


# --- fix 1: minimal child env -------------------------------------------------

def test_child_env_strips_secrets(plugin, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-canary-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-canary")
    monkeypatch.setenv("HERMES_BOT_TOKEN", "bot-canary")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("TMPDIR", "/tmp")
    env = plugin._child_env()
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "HERMES_BOT_TOKEN" not in env
    # the maintainer-named baseline survives
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/tester"
    assert env["TERM"] == "xterm-256color"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "C"
    assert env["TMPDIR"] == "/tmp"


def test_child_env_passthrough_only_when_declared(plugin, monkeypatch):
    monkeypatch.setenv("OMP_PROVIDER_KEY", "needs-to-pass")
    monkeypatch.setenv("SOME_OTHER_KEY", "must-not-pass")
    monkeypatch.setattr(plugin, "_PLUGIN_CTX",
                        FakeCtx({"env_passthrough": ["OMP_PROVIDER_KEY"]}))
    env = plugin._child_env()
    assert env.get("OMP_PROVIDER_KEY") == "needs-to-pass"
    assert "SOME_OTHER_KEY" not in env


def test_child_env_passthrough_without_ctx(plugin, monkeypatch):
    monkeypatch.setenv("OMP_PROVIDER_KEY", "x")
    monkeypatch.setattr(plugin, "_PLUGIN_CTX", None)
    env = plugin._child_env()
    assert "OMP_PROVIDER_KEY" not in env  # no ctx -> nothing extra passes


def test_spawn_passes_scrubbed_env(plugin, monkeypatch):
    """The env computed by _child_env (not os.environ) reaches Popen."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-canary-secret")
    captured = {}

    class Boom(OSError):
        pass

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        raise Boom("stop after kwargs captured")

    monkeypatch.setattr(plugin.subprocess, "Popen", fake_popen)
    with pytest.raises(plugin._OmpRpcError):
        plugin.OmpRpcEnvironment(cwd=os.path.expanduser("~"), timeout=5,
                                 task_id="env-test", model=None)
    assert "env" in captured, "Popen got no env= — full Hermes env leaks"
    assert captured["env"] is not os.environ
    assert "OPENAI_API_KEY" not in captured["env"]


# --- fix 2: profile-aware, private live dir -----------------------------------

def test_live_dir_follows_hermes_home(plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert plugin._live_dir() == os.path.join(str(tmp_path), "omp-live")


def test_live_dir_private_perms(plugin, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    old_umask = os.umask(0o022)  # make perms assertions meaningful on any host umask
    try:
        env = object.__new__(plugin.OmpRpcEnvironment)
        env.task_id = "perm-test"
        env._process = None
        spool = tmp_path / "spool.txt"
        spool.write_text("output")
        env._publish_live(str(spool), "echo hi", str(tmp_path))
        live = plugin._live_dir()
        assert stat.S_IMODE(os.stat(live).st_mode) == 0o700
        meta = os.path.join(live, "last.json")
        assert stat.S_IMODE(os.stat(meta).st_mode) == 0o600
        # content still correct
        with open(meta) as f:
            assert json.load(f)["command"] == "echo hi"
    finally:
        os.umask(old_umask)


def test_private_write_tightens_preexisting_loose_file(plugin, tmp_path, monkeypatch):
    """os.open's mode is creation-only; a leftover 0644 file must get pinned to 0600."""
    old_umask = os.umask(0o022)
    try:
        path = tmp_path / "last.json"
        path.write_text('{"old": 1}')
        os.chmod(path, 0o644)
        plugin._private_write(str(path), '{"new": 2}')
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert json.loads(path.read_text()) == {"new": 2}
    finally:
        os.umask(old_umask)


# --- fix 3: model from plugin config ------------------------------------------

def test_model_from_plugin_config(plugin, monkeypatch):
    captured = {}

    def fake_env(**kw):
        captured.update(kw)
        return "env-stub"

    monkeypatch.setattr(plugin, "OmpRpcEnvironment", fake_env)
    monkeypatch.setattr(plugin, "_PLUGIN_CTX", FakeCtx({"model": "cfg-model"}))
    monkeypatch.delenv("HERMES_OMP_MODEL", raising=False)
    provider = plugin.OmpProvider()
    provider.create_environment(cwd="/tmp", timeout=5)
    assert captured["model"] == "cfg-model"


def test_model_env_var_is_fallback(plugin, monkeypatch):
    captured = {}

    def fake_env(**kw):
        captured.update(kw)
        return "env-stub"

    monkeypatch.setattr(plugin, "OmpRpcEnvironment", fake_env)
    monkeypatch.setattr(plugin, "_PLUGIN_CTX", None)
    monkeypatch.setenv("HERMES_OMP_MODEL", "env-model")
    provider = plugin.OmpProvider()
    provider.create_environment(cwd="/tmp", timeout=5)
    assert captured["model"] == "env-model"
