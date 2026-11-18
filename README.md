# oh-my-pi terminal backend for Hermes Agent

A terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
Hermes's `terminal`, file and code tools run their shell commands through a
long-lived [`omp --mode rpc`](https://github.com/can1357/oh-my-pi) session
shell on this host. You get omp's embedded-shell environment persistence
(env vars survive across commands, like the local backend's snapshot) while
Hermes keeps its own approval guards — this is a same-host backend
(`is_remote = False`, `is_container = False`), so nothing is sandboxed away.

## Install

```bash
hermes plugins install Thaeland/oh-my-pi-terminal-backend
```

Requires the `omp` CLI on `PATH`. Any official install method works — the
plugin speaks omp's RPC protocol (≥ 17.0.9), not npm:

```bash
# official installer (macOS/Linux) — self-contained, no bun needed
curl -fsSL https://omp.sh/install | sh

# official installer (Windows, PowerShell)
irm https://omp.sh/install.ps1 | iex

# or via npm/bun (needs bun reachable for the launcher, and postinstall
# scripts allowed: npm i -g --allow-scripts=omp,bun @oh-my-pi/pi-coding-agent bun)
npm install -g @oh-my-pi/pi-coding-agent bun
```

A source build (`git clone` + `bun install`) works too — this backend was
developed against one. `npx`/`bunx` ephemeral runs do NOT work: the plugin
spawns `omp` by name, so it must be a stable binary on `PATH`.

Two PATH caveats:
- **Gateway visibility:** Hermes serving processes (e.g. systemd user
  services) snapshot `PATH` at boot. The installer's "add to PATH"
  instruction fixes your interactive shell, not the service — make sure the
  directory holding `omp` is on the *service's* PATH (a symlink into
  `~/.local/bin` is the robust fix), then restart the gateway.
- **Windows:** supported — verified on Windows 11 Pro with the official
  installer (`irm https://omp.sh/install.ps1 | iex`, omp 18.22.1) via
  `probe_windows.py` (all checks pass: spawn, lossless chunking, env/cwd
  persistence, spool slicing, `2>/dev/null`). The wrapper uses `tail`/`head`
  from omp's own shell (no `dd`), resolves npm `.cmd` shims via `cmd /c`,
  and tears the process tree down with `taskkill`. After installing, open a
  NEW terminal — PATH changes don't reach already-open ones.
  macOS is untested and needs GNU coreutils (`base64 -w0` is GNU-only);
  Linux is the primary platform.

omp also needs a model configured — `~/.omp/agent/models.yml`, provider
sign-in, or run `omp` once interactively.

**omp version compatibility:** tested continuously across omp 18.4.3 →
18.22.0 (the whole 18.x line this backend has run in production). The floor
is omp **17.0.9**, where the RPC protocol gained the negotiated lossless
output framing (`rpc_chunk` frames, `protocolVersion` in the ready frame)
that this backend relies on for large outputs. Older omp versions predate
the framing protocol and are not supported.

## Use it

```bash
hermes config set terminal.backend oh-my-pi-terminal-backend
systemctl --user restart hermes-gateway   # or restart your Hermes service
```

The terminal tool registers at gateway boot, so a restart is required after
installing or after making `omp` resolvable on `PATH`.

Optional plugin config (in `config.yaml` under
`plugins.entries.oh-my-pi-terminal-backend.config`):

- `model`: pin the model omp uses for Hermes commands (omp's own default
  otherwise). The `HERMES_OMP_MODEL` env var still works as a fallback.
- `env_passthrough`: list of environment variable NAMES (e.g.
  `[ANTHROPIC_API_KEY]`) whose values are handed to the omp child. The omp
  process — and every command the model runs through it — otherwise gets a
  minimal environment (`PATH`, `HOME`, `LANG`/`LC_*`, `TERM`, `TMPDIR`, plus
  Windows system vars), so Hermes' provider and bot credentials never reach
  it, matching the built-in local backend's policy.

## How it works

- One `omp --mode rpc --no-ui --no-session` process per Hermes task; commands
  are dispatched over the JSONL RPC `bash` surface, serialized per env
  (omp's `abort_bash` is session-global).
- cwd does not persist inside omp's shell, so each command is wrapped with a
  `cd`, and the final `pwd` is reported back through Hermes's
  `cwd_observed` contract. Reported directories are only accepted when the
  gateway user can actually enter them (`isdir` + `R_OK|X_OK`), so a stray
  unreadable path (e.g. `/root`) can never poison the session cwd.
- omp's RPC sink caps inline output (50 KB, 512 columns). To stay lossless
  for Hermes file-tool reads, every command spools its output to a temp file
  and transfers it as base64 chunks (first chunk inline, remainder read back
  in shell slices via `tail -c +N | head -c M`); a `--config` overlay
  disables the column cap for the session.
- Live observation: `<hermes-home>/omp-live/output.log` (the ACTIVE profile's
  Hermes home, via `get_hermes_home()`) symlinks to the running command's
  spool (`tail -f` works), then holds the final output; `last.json` describes
  the last command. The directory is created 0700 and its files are written
  0600 — it records command text and output, so it stays private.

## Known limitation: `exit` in a command

omp's shell is persistent (that's the point — env vars survive across
commands), so a command containing a bare `exit` terminates that session
shell; the backend detects the dead shell and respawns it, but exported env
vars from earlier commands are lost. Use `bash -c 'exit 3'` or a subshell
`(exit 3)` when you want a non-zero rc without killing the session.

## Failure modes: when omp can't be found

Hermes never auto-falls-back to another terminal backend — that is core
policy — so this plugin fails legibly instead:

- **Missing at gateway boot:** the provider's requirement check fails
  (`omp` not on `PATH`), the gateway logs the reason with install
  instructions, and the terminal tool is **not registered** for new
  sessions. Chat and everything else keeps working. Fixing `omp` afterwards
  does NOT bring the tool back in a running session — the toolset is frozen
  at boot, so restart the gateway after repairing.
- **Missing mid-run** (e.g. an update wiped the npm prefix while the
  gateway was up): every terminal/file/code call fails with
  `omp binary not found or not executable on PATH — install it with npm
  install -g @oh-my-pi/pi-coding-agent bun ...` instead of an opaque
  traceback, and no temp files are leaked. The gateway itself is unaffected.

## Troubleshooting

- **`omp binary not found on PATH`** after a `hermes update`: the update can
  rebuild Hermes's managed node toolchain and drop npm globals. Reinstall
  (`npm install -g @oh-my-pi/pi-coding-agent bun`), re-point any
  `~/.local/bin/omp` symlink at the new prefix, and restart the gateway.
- **`omp exited at startup`**: check `omp --version` in the gateway's own
  environment; a missing `bun` or unreadable model config lands here.
- **Exit 126 / Permission denied**: the npm-installed `cli.ts` lost its exec
  bit — `chmod +x <prefix>/lib/node_modules/@oh-my-pi/pi-coding-agent/src/cli.ts`.

## Tests

```bash
uv venv .venv && uv pip install -p .venv/bin/python pytest
.venv/bin/python -m pytest tests/ -v
```

## Disclosure

No Python dependencies, no network calls of its own. It spawns the `omp` CLI
you installed, one long-lived process per Hermes task, with a MINIMAL child
environment (baseline vars only; extra names pass only if you declare them in
`env_passthrough`) so Hermes' own credentials never reach the child; the
spawned `omp` itself may make network calls to whatever model provider you
configured for omp (the plugin never talks to the network directly). Besides
the output spool it writes two more temp-file classes under your scratch dir:
an omp `--config` overlay per env and a stdin spool when a command carries
`stdin_data`. It also maintains the `<hermes-home>/omp-live/` observation log
described above (0700 dir, 0600 files).

## License

MIT — see [LICENSE](LICENSE).
