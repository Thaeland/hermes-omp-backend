"""omp terminal backend for Hermes.

Runs Hermes shell commands through a long-lived ``omp --mode rpc`` session
(oh-my-pi, https://github.com/can1357/oh-my-pi). Each Hermes task gets one
omp process; commands are dispatched over the JSONL RPC ``bash`` surface.

Semantics (probed against omp 18.4.3):
- env persists across commands (like the local backend's snapshot); cwd does
  NOT persist, so this plugin cd's per command and reports the final pwd via
  a trailing marker (Hermes ``cwd_observed`` contract).
- stderr is merged into output by omp.
- the RPC sink caps inline output at 50KB (hardcoded) and clips lines at 512
  columns. To stay lossless for Hermes file-tool reads, every command spools
  its output to a temp file and transfers it as base64 chunks (first chunk
  inline, remainder via dd reads). A ``--config`` overlay disables the column
  cap for the session.
- ``abort_bash`` is session-global, so execute() is serialized per env.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

logger = logging.getLogger(__name__)

# Inline budget per RPC response: omp's sink caps at 50KB; keep headroom for
# the size/cwd markers. Must stay a multiple of 4 for base64 alignment.
_INLINE_B64_BYTES = 36_000
_READY_TIMEOUT = 30.0

# Live-observation surface: output.log symlinks to the running command's
# spool, then holds the final output; last.json describes the last command.
# Lives under the ACTIVE profile's Hermes home (never a hardcoded ~), created
# 0700 with 0600 files: it records command text and the last command's full
# output, so it must not be world-readable.
def _live_dir() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home() / "omp-live")
    except Exception:
        home = (os.environ.get("HERMES_HOME") or "").strip() \
            or os.path.expanduser("~/.hermes")
        return os.path.join(home, "omp-live")


def _private_makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _private_write(path: str, data: str) -> None:
    # O_NOFOLLOW: a planted symlink (e.g. by a prompt-injected command in the
    # very dir we manage) must not make the gateway truncate its target.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        # os.open's mode only applies at creation; an already-existing file
        # keeps its old perms — chmod pins it to 0600 either way.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


# Plugin context, captured at register() so config-backed knobs (model,
# env_passthrough) are readable from environment instances.
_PLUGIN_CTX = None

# Baseline allowlist for the omp child env. omp (and every command the model
# runs through it) must NOT see Hermes' provider/bot credentials — the built-in
# local backend strips those, and so does this backend via _child_env().
_CHILD_ENV_ALLOW = frozenset(
    ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG",
     "TMPDIR", "TMP", "TEMP")
    + (("SYSTEMROOT", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "SYSTEMNAME",
        "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "ALLUSERSPROFILE",
        "PROGRAMDATA", "LOCALAPPDATA", "APPDATA") if os.name == "nt" else ())
)


def _child_env() -> dict:
    """Minimal environment for the omp child process.

    Only the baseline allowlist (PATH/HOME/LANG/LC_*/TERM/TMPDIR + Windows
    system vars) passes automatically; anything else — e.g. a provider API key
    omp should use — must be user-declared in the plugin config as
    ``env_passthrough: [NAME, ...]`` and is copied by name from os.environ.
    """
    env = {}
    for key, val in os.environ.items():
        # Case-sensitive on POSIX (distinct vars); case-insensitive on Windows
        # (the env block itself is case-insensitive, casing is arbitrary).
        probe = key.upper() if IS_WINDOWS else key
        if probe in _CHILD_ENV_ALLOW or key.startswith("LC_"):
            env[key] = val
    passthrough: list = []
    if _PLUGIN_CTX is not None:
        try:
            raw = _PLUGIN_CTX.get_config("env_passthrough", []) or []
        except Exception:
            raw = []
        if isinstance(raw, str):
            passthrough = [raw]
        elif isinstance(raw, (list, tuple)):
            passthrough = [n for n in raw if isinstance(n, str)]
        elif raw:
            # e.g. YAML `env_passthrough: ON` parses to True — degrade to
            # nothing passed, never brick every spawn with a TypeError.
            logger.warning("omp backend: env_passthrough config %r is not a "
                           "list of env var names; ignoring it", raw)
    for name in passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    return env

# Overlay disabling omp's per-line column cap (would corrupt base64 chunks).
# artifactSpillThreshold is also raised, though the RPC bash sink hardcodes its
# own 50KB inline cap — the spool pattern sidesteps it either way.
_CONFIG_OVERLAY = "tools:\n  outputMaxColumns: 0\n  artifactSpillThreshold: 32768\n"

def _usable_cwd(path: str) -> bool:
    """True when *path* is a directory this process can actually enter.

    Hermes hands the provider the session's raw recorded cwd, which can be
    a path the gateway user cannot access (e.g. /root) or a deleted worktree.
    Popen raises PermissionError/FileNotFoundError on such a cwd and the env
    is never created — which also means the poisoned cwd record is never
    repaired, locking every later terminal call out of the session.
    """
    return bool(path) and os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def _sh_squote(s: str) -> str:
    """POSIX single-quote escape — the only safe way to embed an arbitrary
    string (cwd, paths) in a shell script. Shell double quotes (what
    json.dumps produces) still expand ``$()``, backticks and ``$VAR``."""
    return "'" + s.replace("'", "'\\''") + "'"


def _drain_stderr(proc, sink=None) -> None:
    """Read ``proc.stderr`` to EOF so the OS pipe never fills up (classic
    subprocess pipe-full deadlock: omp blocks on its next stderr write and
    the whole RPC stream wedges). When a sink list is given, keeps roughly
    the last 16 KB for startup-error reporting."""
    try:
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                break
            if sink is not None:
                sink.append(chunk)
                if sum(map(len, sink)) > 16384:
                    del sink[:-2]
    except Exception:
        pass


IS_WINDOWS = os.name == "nt"

# Windows process-creation flags (numeric: the subprocess attributes only
# exist on Windows, but tests monkeypatch IS_WINDOWS on any platform).
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


class _OmpRpcError(RuntimeError):
    pass


class OmpRpcEnvironment:
    """BaseEnvironment duck type backed by one ``omp --mode rpc`` process."""

    def __init__(self, cwd: str, timeout: int, task_id: str, model: Optional[str]):
        self.cwd = cwd or os.path.expanduser("~")
        self.timeout = timeout
        self.task_id = task_id
        self._model = model
        self._lock = threading.Lock()          # serialize commands (abort_bash is global)
        self._write_lock = threading.Lock()
        self._ids = itertools.count()
        self._gen = 0
        self._closed = False
        self._pending: Dict[str, tuple] = {}
        self._responses: Dict[str, dict] = {}
        self._chunks: Dict[str, dict] = {}
        self._stderr_tail: list = []
        self._ready = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._overlay_path: Optional[str] = None
        try:
            self._spawn()
        except BaseException:
            # A failed constructor means cleanup() never runs; never leave
            # the config overlay behind on ANY spawn failure (missing
            # binary, exit-126 PermissionError, startup crash, no ready frame).
            if self._overlay_path:
                try:
                    os.unlink(self._overlay_path)
                except OSError:
                    pass
                self._overlay_path = None
            raise

    # --- process lifecycle -------------------------------------------------
    def _spawn(self) -> None:
        if self._closed:
            return  # teardown won the race; never resurrect a closed env
        self._gen += 1
        if self._overlay_path is None:
            fd, self._overlay_path = tempfile.mkstemp(prefix="hermes-omp-cfg-", suffix=".yml")
            with os.fdopen(fd, "w") as f:
                f.write(_CONFIG_OVERLAY)
        if not _usable_cwd(self.cwd):
            fallback = os.path.expanduser("~")
            logger.warning("omp backend: cwd %r is not an accessible directory; "
                           "spawning in %s instead", self.cwd, fallback)
            self.cwd = fallback
        omp_bin = shutil.which("omp") or "omp"
        cmd = [omp_bin, "--mode", "rpc", "--no-ui", "--no-session",
               "--config", self._overlay_path]
        if omp_bin.lower().endswith((".cmd", ".bat")):
            # npm on Windows installs .cmd shims, which CreateProcess
            # cannot launch directly.
            cmd = ["cmd", "/c"] + cmd
        if self._model:
            cmd += ["--model", self._model]
        self._ready.clear()
        popen_kwargs: dict = dict(
            cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=_child_env(),  # never hand omp (or model-run commands) Hermes' secrets
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = (_CREATE_NEW_PROCESS_GROUP
                                             | _CREATE_NO_WINDOW)
        else:
            popen_kwargs["start_new_session"] = True
        try:
            self._process = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as e:
            # A wiped npm prefix (e.g. after `hermes update`) lands here as
            # FileNotFoundError; a cli.ts that lost its exec bit lands here as
            # PermissionError (exit 126). A bare OSError at tool dispatch
            # blinds every execution path with an opaque traceback; name the
            # fix instead.
            raise _OmpRpcError(
                "omp binary not found or not executable on PATH — install it "
                "with the official installer (curl -fsSL https://omp.sh/install"
                " | sh, or irm https://omp.sh/install.ps1 | iex — then open a "
                "NEW terminal) or via npm (`npm install -g "
                "@oh-my-pi/pi-coding-agent bun`; if it exists but fails with "
                "Permission denied, chmod +x its src/cli.ts), then restart "
                "the Hermes gateway: the terminal tool registers at boot"
            ) from e
        self._stderr_tail = []
        threading.Thread(target=_drain_stderr,
                         args=(self._process, self._stderr_tail), daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()
        deadline = time.time() + _READY_TIMEOUT
        while time.time() < deadline:
            if self._ready.wait(0.25):
                return
            if self._process.poll() is not None:
                err = ""
                try:
                    time.sleep(0.1)  # let the stderr drain reach EOF
                    err = "".join(self._stderr_tail)[:500]
                except Exception:
                    pass
                raise _OmpRpcError(f"omp exited at startup (rc={self._process.returncode}): {err}")
        raise _OmpRpcError("omp RPC did not emit a ready frame within 30s")

    def _read_loop(self) -> None:
        proc = self._process
        assert proc is not None and proc.stdout is not None
        gen = self._gen
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "ready":
                self._ready.set()
                continue
            if msg.get("type") == "rpc_chunk":
                self._handle_chunk(msg)
                continue
            rid = msg.get("id")
            if rid and rid in self._pending:
                self._responses[rid] = msg
                self._pending[rid][1].set()
        # stdout closed: fail waiters of THIS spawn generation only — a
        # stale loop must never poison requests registered after respawn.
        self._fail_all_pending(gen, "omp RPC stream closed")

    def _fail_all_pending(self, gen: int, reason: str) -> None:
        for rid, (egen, ev) in list(self._pending.items()):
            if egen != gen:
                continue
            self._responses.setdefault(rid, {"success": False, "error": reason})
            ev.set()

    def _handle_chunk(self, msg: dict) -> None:
        """Reassemble chunked frames (responses >1MB arrive as rpc_chunk)."""
        cid = msg.get("chunkId")
        if not cid:
            return
        state = self._chunks.setdefault(cid, {"parts": {}, "count": msg.get("count")})
        if len(self._chunks) > 64:  # bound: incomplete streams must not accumulate
            # Eviction is insertion-order; with >64 concurrent streams an
            # in-flight one could be evicted (its waiter then times out and
            # the command retries). Accepted: 64 concurrent >1MB streams is
            # far beyond real use.
            for stale in list(self._chunks)[: len(self._chunks) - 64]:
                self._chunks.pop(stale, None)
        idx, data = msg.get("index"), msg.get("data")
        if not isinstance(idx, int) or not isinstance(data, str):
            return
        try:
            state["parts"][idx] = base64.b64decode(data).decode("utf-8")
        except Exception:
            return
        if state["count"] and len(state["parts"]) == state["count"]:
            try:
                full = json.loads("".join(state["parts"][i] for i in range(state["count"])))
            except Exception:
                full = None
            del self._chunks[cid]
            if isinstance(full, dict) and full.get("id") in self._pending:
                rid = full["id"]
                self._responses[rid] = full
                self._pending[rid][1].set()

    def _send(self, obj: dict) -> None:
        proc = self._process
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise _OmpRpcError("omp RPC process is not running")
        with self._write_lock:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

    def _request(self, cmd_type: str, timeout: float, **payload: Any) -> dict:
        rid = f"h{next(self._ids)}"
        ev = threading.Event()
        self._pending[rid] = (self._gen, ev)
        try:
            self._send({"id": rid, "type": cmd_type, **payload})
            if not ev.wait(timeout):
                raise TimeoutError(f"omp RPC {cmd_type} timed out after {timeout}s")
            resp = self._responses.pop(rid, {})
        finally:
            self._pending.pop(rid, None)
        if not resp.get("success", False):
            raise _OmpRpcError(f"omp RPC {cmd_type} failed: {resp.get('error')}")
        return resp.get("data") or {}

    # --- Hermes BaseEnvironment contract ------------------------------------
    def execute(self, command: str, cwd: str = "", *, timeout: Optional[int] = None,
                stdin_data: Optional[str] = None, **kwargs: Any) -> dict:
        effective_timeout = timeout or self.timeout
        effective_cwd = cwd or self.cwd
        if self._closed:
            raise _OmpRpcError("omp environment is closed (cleanup already ran)")
        with self._lock:
            try:
                return self._execute_locked(command, effective_cwd, effective_timeout, stdin_data)
            except _OmpRpcError as e:
                logger.warning("omp env error (%s); respawning once", e)
                self._restart()
                return self._execute_locked(command, effective_cwd, effective_timeout, stdin_data)

    def _restart(self) -> None:
        if self._closed:
            return
        self._kill_process()
        self._spawn()

    def _kill_process(self) -> None:
        proc = self._process
        if proc is None:
            return
        if IS_WINDOWS:
            # No POSIX process groups: taskkill the whole tree.
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
            except Exception:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()

    def _publish_live(self, spool: str, command: str, cwd: str) -> None:
        """Expose the running command for observation.

        ``<hermes-home>/omp-live/output.log`` is a symlink to the live spool
        while
        a command runs (``tail -f`` works), then a real copy of the final
        output afterwards; ``last.json`` carries the
        command, cwd, task, pid and timing.
        """
        try:
            live = _live_dir()
            _private_makedirs(live)
            meta = {"command": command[:2000], "cwd": cwd, "task_id": self.task_id,
                    "pid": self._process.pid if self._process else None,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": "running"}
            _private_write(os.path.join(live, "last.json"), json.dumps(meta, indent=1))
            link = os.path.join(live, "output.log")
            # thread ident included: two concurrent tasks in ONE gateway
            # process share a pid and would collide on the temp name.
            tmp_link = link + f".new.{os.getpid()}.{threading.get_ident()}"
            try:
                os.symlink(spool, tmp_link)
                os.replace(tmp_link, link)
            except OSError:
                try:
                    os.unlink(tmp_link)
                except OSError:
                    pass
        except OSError:
            pass  # observability must never break execution

    def _finalize_live(self, spool: str, rc, started: float) -> None:
        try:
            live = _live_dir()
            link = os.path.join(live, "output.log")
            real = spool + ".final"
            try:
                shutil.copyfile(spool, real)
                try:
                    os.chmod(real, 0o600)
                except OSError:
                    pass
                os.replace(real, link)
            except OSError:
                pass
            meta_path = os.path.join(live, "last.json")
            meta = {}
            # Never read through a planted symlink: an attacker-controlled
            # target's content would be echoed back into it below.
            if not os.path.islink(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
            meta.update({"status": "done", "exit_code": rc,
                         "duration_s": round(time.time() - started, 2)})
            _private_write(meta_path, json.dumps(meta, indent=1))
        except OSError:
            pass

    def _execute_locked(self, command: str, cwd: str, timeout: int,
                        stdin_data: Optional[str]) -> dict:
        started = time.time()
        final_rc = None
        size_marker = f"__OMPSZ_{uuid.uuid4().hex[:12]}__"
        cwd_marker = f"__OMPCW_{uuid.uuid4().hex[:12]}__"
        fd, spool = tempfile.mkstemp(prefix="hermes-omp-out-")
        os.close(fd)
        # Pre-create the b64 target here (mkstemp => 0600): shell redirection
        # would otherwise create it fresh under the inherited umask (022 ->
        # 0644), leaving a world-readable copy of the full output in TMPDIR.
        fd_b, b64_file = tempfile.mkstemp(prefix="hermes-omp-b64-")
        os.close(fd_b)
        self._publish_live(spool, command, cwd)
        stdin_redir = ""
        stdin_path = None
        try:
            if stdin_data is not None:
                fd2, stdin_path = tempfile.mkstemp(prefix="hermes-omp-stdin-")
                with os.fdopen(fd2, "w") as f:
                    f.write(stdin_data)
                stdin_redir = f" < {json.dumps(stdin_path)}"
            # Spool real output to a file; inline only: size, first base64
            # chunk, final cwd. rc propagates via a SUBSHELL exit — a bare
            # `exit` would terminate omp's persistent session shell and wipe
            # env vars for every later command (probed on 18.4.3).
            wrapped = (
                f"if cd {_sh_squote(cwd)} 2>/dev/null; then\n"
                f"{{\n{command}\n}} > {json.dumps(spool)} 2>&1{stdin_redir} ; __rc=$?\n"
                f"base64 -w0 {json.dumps(spool)} > {json.dumps(b64_file)}\n"
                f"printf '\\n{size_marker}%s{size_marker}\\n' \"$(wc -c < {json.dumps(b64_file)})\"\n"
                f"head -c {_INLINE_B64_BYTES} {json.dumps(b64_file)}\n"
                f"printf '\\n{cwd_marker}%s\\n' \"$(pwd)\"\n"
                f"if [ $__rc -ne 0 ]; then (exit $__rc); fi\n"
                f"else\n"
                f"printf 'omp backend: requested working directory %s is not accessible; "
                f"command was not run\\n' {_sh_squote(cwd)}\n"
                f"printf '\\n{cwd_marker}%s\\n' \"$(pwd)\"\n"
                f"(exit 2)\n"
                f"fi"
            )
            try:
                # Small grace for the wrapper's post-processing (base64 +
                # markers); the timeout itself is enforced by our abort below,
                # not by omp (RPC bash has no timeout param).
                data = self._request("bash", timeout + 3, command=wrapped)
            except TimeoutError:
                try:
                    self._send({"id": "abort", "type": "abort_bash"})
                except Exception:
                    pass
                return {"output": f"[Command timed out after {timeout} seconds]", "returncode": 124}

            if data.get("cancelled"):
                # omp aborted the foreground command (e.g. a nested abort);
                # present Hermes' timeout convention.
                return {"output": f"[Command timed out after {timeout} seconds]", "returncode": 124}

            raw_inline = data.get("output", "")
            rc = data.get("exitCode")
            final_rc = rc
            if rc is None:
                rc = 0

            # Parse markers: inline layout is <b64size line>\n<b64 head>\n<cwd line>.
            b64_total = None
            si = raw_inline.find(size_marker)
            if si != -1:
                rest = raw_inline[si + len(size_marker):]
                ei = rest.find(size_marker)
                if ei != -1:
                    try:
                        b64_total = int(rest[:ei].strip())
                    except ValueError:
                        b64_total = None
                    raw_inline = raw_inline[:si] + rest[ei + len(size_marker):]
            new_cwd = None
            ci = raw_inline.rfind(cwd_marker)
            if ci != -1:
                tail = raw_inline[ci + len(cwd_marker):].strip().splitlines()
                if tail and _usable_cwd(tail[0]):
                    new_cwd = tail[0]
                raw_inline = raw_inline[:ci].rstrip("\n")

            b64_parts = [raw_inline.lstrip("\n")]
            if b64_total is not None:
                # Remaining base64 windows read straight from the b64 file:
                # dd with a block size equal to the inline budget, O(1)/chunk.
                offset = min(len(b64_parts[0]), b64_total)
                block = 0
                while offset < b64_total:
                    block += 1
                    # tail|head instead of dd: dd is absent from omp's
                    # embedded shell utilities (pi-builtins), so the
                    # portable form also makes the wrapper Windows-ready.
                    chunk_cmd = (f"tail -c +{block * _INLINE_B64_BYTES + 1} "
                                 f"{json.dumps(b64_file)} | head -c {_INLINE_B64_BYTES}")
                    try:
                        cdata = self._request("bash", 60, command=chunk_cmd)
                    except (_OmpRpcError, TimeoutError):
                        break
                    piece = (cdata.get("output") or "").strip()
                    if not piece:
                        break
                    b64_parts.append(piece)
                    offset += len(piece)
                truncated = offset < b64_total
                # Known edge (accepted): a corrupt chunk whose junk chars are
                # dropped by b64decode(validate=False) yet still decodes can
                # slip through unflagged. Offsets prove volume, not integrity.
                try:
                    output = base64.b64decode("".join(b64_parts)).decode("utf-8", errors="replace")
                except Exception:
                    # Last resort: decode the inline head alone (trimmed to a
                    # whole base64 group) — never hand back undecoded b64.
                    truncated = True
                    head = b64_parts[0]
                    head = head[: len(head) - len(head) % 4]
                    try:
                        output = base64.b64decode(head).decode("utf-8", errors="replace")
                    except Exception:
                        output = ""
                if truncated:
                    output += (f"\n[omp backend: output truncated — received "
                               f"{offset} of {b64_total} base64 bytes]")
            else:
                output = raw_inline

            result: dict = {"output": output, "returncode": int(rc)}
            if new_cwd:
                self.cwd = new_cwd
                result["cwd_observed"] = True
                result["cwd"] = new_cwd
            return result
        finally:
            self._finalize_live(spool, final_rc, started)
            for p in (spool, b64_file, stdin_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def cleanup(self) -> None:
        # Set the flag BEFORE killing: a concurrent execute() must not
        # respawn the process after teardown (see _restart/_spawn guards).
        self._closed = True
        self._kill_process()
        self._process = None
        if self._overlay_path:
            try:
                os.unlink(self._overlay_path)
            except OSError:
                pass
            self._overlay_path = None


class OmpProvider(TerminalEnvironmentProvider):
    name = "oh-my-pi-terminal-backend"
    display_name = "oh-my-pi terminal backend"
    # Runs on THIS host through omp's embedded shell: same filesystem, same
    # paths — not remote, not a container; keep approval guards on.
    is_remote = False
    is_container = False

    @property
    def description(self) -> str:
        return "Run commands through an oh-my-pi (omp) RPC session shell on this host."

    @property
    def env_description(self) -> str:
        return "an oh-my-pi (omp) embedded-shell session on the local host"

    def is_available(self) -> bool:
        return shutil.which("omp") is not None

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        if not self.is_available():
            logger.error("omp backend: 'omp' binary not found on PATH "
                         "(npm install -g @oh-my-pi/pi-coding-agent bun)")
            return False
        return True

    def setup_instructions(self):
        return [
            "Install omp:  npm install -g @oh-my-pi/pi-coding-agent bun",
            "Configure a model: ~/.omp/agent/models.yml (omp.sh/docs) or run `omp` once.",
            "Optional plugin config: `model` to pin a model for Hermes commands,",
            "`env_passthrough: [NAME, ...]` to let named env vars reach the omp child.",
        ]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None,
                           container_config: Optional[Dict[str, Any]] = None,
                           **kwargs: Any):
        model = None
        if _PLUGIN_CTX is not None:
            try:
                raw_model = _PLUGIN_CTX.get_config("model")
            except Exception:
                raw_model = None
            if isinstance(raw_model, str) and raw_model.strip():
                model = raw_model.strip()
            elif raw_model:
                logger.warning("omp backend: model config %r is not a string; "
                               "ignoring it", raw_model)
        if not model:
            model = os.environ.get("HERMES_OMP_MODEL") or None
        return OmpRpcEnvironment(cwd=cwd, timeout=timeout, task_id=task_id, model=model)


def register(ctx):
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx
    ctx.register_terminal_environment_provider(OmpProvider())
