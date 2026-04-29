# Changelog

## 0.22.0

The headline change is **fast-launch via a tiny C launcher (`clichec`)**.
Help, completion, unknown-command, and per-command llm-help are now served
in 2–7 ms instead of ~50 ms — without touching the canonical Python
dispatch path. Real downstream tools `pip install`'d on supported
platforms get this transparently, no action needed.

### Added

- **`clichec`** — single-file C launcher (`cliche/clichec.c`, ~2100 lines,
  no dependencies beyond libc). Reads cliche's runtime cache JSON
  directly, services `--help` / `--llm-help` / unknown-cmd / shell
  completion, defers to Python on real dispatch and on anything outside
  its declared coverage. Falls through to Python on any unexpected exit
  code (defer, signals, OOM-kill) so a misbehaving clichec can never
  brick a binary.

- **Auto-applied fast-shim wrapper.** `cliche install` and (via a one-time
  self-upgrade in `cliche.launcher`) `pip install` of any cliche-derived
  CLI replace the pip-generated Python shim with a small POSIX shell
  wrapper that exec's clichec, falling back to the Python launcher when
  clichec defers. Self-healing: if pip ever overwrites the wrapper, the
  next invocation rewrites it back.

- **Pre-built wheels for `linux-x86_64`, `linux-aarch64`, `macos-arm64`**
  ship `cliche/_bin/clichec` so users on those platforms skip the
  compile-on-install step entirely. Other platforms (Windows, BSD,
  Linux ARMv7, Intel Macs) get the existing `py3-none-any` fallback that
  compiles `clichec.c` on first need via `ensure_built()`.

- **`SHIM` column in `cliche ls`** showing whether each binary is a
  fast-shim (`c`) or pip's stock Python shim (`py`), with a footer
  summary of the count by mode.

- **Shell completion served from C.** Tab-completes top-level commands,
  groups, subcommand names, flag names (long + short), `--no-` variants
  for default-True bools, enum values for positional arguments and flag
  values. Set-equal with what argcomplete emits via Python.

- **`<cmd> --help` and `<group> <cmd> --help`** rendered from cache by
  clichec. Argparse-style output with usage line, `POSITIONAL ARGUMENTS`
  / `OPTIONS` sections, enum choice braces, default values, short flags.
  Information parity with Python's argparse output; formatting (line
  wrapping, metavar case) is allowed to differ.

- **Levenshtein-based "Did you mean: …" suggestion** on unknown commands.
  Both `cliche.run`'s Python path and clichec's C path use the same
  algorithm with the same `len(cmd)//2 + 1` threshold. Suggestion appears
  for both top-level and grouped commands.

- **`CLICHE_NO_FAST_SHIM` env flag** for permanent opt-out — when set,
  `cliche install` skips auto-apply and the launcher's self-upgrade
  short-circuits. Combine with `pip install --force-reinstall <pkg>` to
  revert an existing fast-shim back to a Python shim.

- **Yellow one-time "no compiler" hint** at `cliche install` time when
  neither a wheel-bundled binary nor a C compiler is available. Prints
  the apt/dnf/pacman/apk/xcode-select command for the host OS. Surfaces
  only at install (never per-invocation), respects `NO_COLOR` /
  `FORCE_COLOR`.

- **Parity test suite** (`tests/test_clichec_parity.py`) — 33 cases
  covering byte-exact stdout/stderr/returncode parity for top-level
  help, llm-help, unknown-cmd, completion candidate sets, plus
  content-only parity for argparse-formatted per-command help. Pre-runs
  every (Python, clichec) subprocess pair concurrently; per-test
  overhead is a dict lookup. Plus integration tests for auto-apply and
  for the wrapper's exit-code routing under simulated SIGSEGV/OOM/etc.

### Fixed

- Single-command-dispatch CLIs (`@cli def my_tool(name): ...`) no longer
  break under clichec — when the typo path detects exactly one
  ungrouped function whose name matches the binary, clichec defers
  to Python so the value-as-positional dispatch contract holds.

- Shell wrapper preserves `sys.argv[0]` across the Python fallback so
  argparse and single-command-dispatch see the binary name, not `-c`.

### Internal

- Cache schema bumped to `version: 2.2`, with a new `cliche_version`
  field. Older caches are auto-rewritten on next Python invocation.

## 0.21.0

(See git log for prior history.)
