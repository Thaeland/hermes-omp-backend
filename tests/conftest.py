"""Test harness: load the plugin without a Hermes install.

Stubs ``agent.terminal_env_provider`` (the only Hermes-internal import the
plugin makes) and imports the plugin module from the repo root.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]

if "agent" not in sys.modules or not hasattr(sys.modules["agent"], "terminal_env_provider"):
    agent_pkg = types.ModuleType("agent")
    tep = types.ModuleType("agent.terminal_env_provider")

    class TerminalEnvironmentProvider:  # minimal duck-type of the real ABC
        name = ""
        display_name = ""
        is_remote = False
        is_container = False

    tep.TerminalEnvironmentProvider = TerminalEnvironmentProvider
    agent_pkg.terminal_env_provider = tep
    sys.modules.setdefault("agent", agent_pkg)
    sys.modules["agent.terminal_env_provider"] = tep


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_omp_backend", _REPO / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_omp_backend"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin():
    return load_plugin()
