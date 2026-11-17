# oh-my-pi (omp) terminal backend for Hermes Agent

A terminal backend for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
Hermes's `terminal`, file and code tools run their shell commands through a
long-lived [`omp --mode rpc`](https://github.com/can1357/oh-my-pi) session
shell on this host. You get omp's embedded-shell environment persistence
(env vars survive across commands, like the local backend's snapshot) while
Hermes keeps its own approval guards — this is a same-host backend
(`is_remote = False`, `is_container = False`), so nothing is sandboxed away.

## Install

```bash
hermes plugins install Thaeland/hermes-omp-backend
```

Requires the `omp` CLI on `PATH`:

```bash
npm install -g @oh-my-pi/pi-coding-agent bun
```

(`bun` is needed by omp's launcher. omp also needs a model configured —
`~/.omp/agent/models.yml` or run `omp` once.)

## Use it

```bash
hermes config set terminal.backend omp
systemctl --user restart hermes-gateway   # or restart your Hermes service
```

The terminal tool registers at gateway boot, so a restart is required after
installing or after making `omp` resolvable on `PATH`.

Optional: set `HERMES_OMP_MODEL` to pin the model omp uses for Hermes
commands (omp's own default otherwise).

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
  and transfers it as base64 chunks (first chunk inline, remainder via `dd`
  reads); a `--config` overlay disables the column cap for the session.
- Live observation: `~/.hermes/omp-live/output.log` symlinks to the running
  command's spool (`tail -f` works), then holds the final output;
  `~/.hermes/omp-live/last.json` describes the last command.

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
you installed, one long-lived process per Hermes task, and writes temp spool
files under your scratch directory plus the `~/.hermes/omp-live/`
observation log described above.

## License

MIT — see [LICENSE](LICENSE).
