"""Windows verification probe for the omp Hermes backend.

Run this on a Windows machine AFTER installing omp
(``irm https://omp.sh/install.ps1 | iex``) and Python 3.10+, from a clone
of this repo:

    python probe_windows.py

It drives the real plugin code (``__init__.py`` next to this file) against
the real omp binary and prints PASS/FAIL per check. Everything must pass
before the backend is considered Windows-supported.
"""
import importlib.util
import os
import sys
import types

# stub the single Hermes import so the plugin loads standalone
_pkg = types.ModuleType("agent")
_tep = types.ModuleType("agent.terminal_env_provider")
class TerminalEnvironmentProvider:
    name = ""
_tep.TerminalEnvironmentProvider = TerminalEnvironmentProvider
_pkg.terminal_env_provider = _tep
sys.modules.setdefault("agent", _pkg)
sys.modules["agent.terminal_env_provider"] = _tep

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "omp_plugin", os.path.join(_here, "__init__.py"))
p = importlib.util.module_from_spec(spec)
sys.modules["omp_plugin"] = p
spec.loader.exec_module(p)

assert p.IS_WINDOWS, "this probe must run on Windows"
HOME = os.path.expanduser("~")
failures = 0


def check(label, fn):
    global failures
    try:
        fn()
        print(f"PASS  {label}")
    except Exception as e:
        failures += 1
        print(f"FAIL  {label}: {e!r}")


env = p.OmpRpcEnvironment(cwd=HOME, timeout=60, task_id="win-probe", model=None)
try:
    def small():
        r = env.execute("echo hello-win", HOME)
        assert r["returncode"] == 0 and "hello-win" in r["output"], r
    check("small output + rc", small)

    def big():
        r = env.execute("seq 1 30000", HOME)
        lines = r["output"].splitlines()
        assert r["returncode"] == 0, r["returncode"]
        assert len(lines) == 30000 and lines[0] == "1" and lines[-1] == "30000", \
            f"got {len(lines)} lines"
        assert "truncat" not in r["output"].lower(), "truncation marker present"
    check("300KB lossless chunk transfer (tail|head builtins)", big)

    def cwd_roundtrip():
        r = env.execute("true", HOME)
        assert r.get("cwd_observed") is True, f"no cwd observed: {r}"
    check("cwd round-trip through wrapper", cwd_roundtrip)

    def rc():
        r = env.execute("(exit 7)", HOME)
        assert r["returncode"] == 7, r
    check("rc propagation via subshell exit", rc)

    def envpersistence():
        env.execute("export WINPROBE_VAR=persisted", HOME)
        r = env.execute("echo $WINPROBE_VAR", HOME)
        assert "persisted" in r["output"], r
    check("embedded-shell env persistence", envpersistence)

    def devnull():
        # the wrapper's cd guard uses 2>/dev/null; a bad cwd must fail
        # gracefully (rc 2, no crash) rather than wedge the env
        r = env.execute("true", "Q:\\definitely-not-here")
        assert r["returncode"] == 2, r
        r2 = env.execute("echo still-alive", HOME)
        assert "still-alive" in r2["output"], r2
    check("2>/dev/null cd-guard + env survives bad cwd", devnull)

    def cmd_shim():
        import shutil
        found = shutil.which("omp")
        assert found, "omp not on PATH"
        print(f"      (omp resolved to: {found})")
    check("omp resolvable on PATH", cmd_shim)
finally:
    def teardown():
        env.cleanup()
        assert env._process is None
    check("teardown (taskkill tree, no leak)", teardown)

print("WINDOWS PROBE: ALL PASS" if failures == 0 else f"WINDOWS PROBE: {failures} FAILURE(S)")
sys.exit(1 if failures else 0)
