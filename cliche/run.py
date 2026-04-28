#!/usr/bin/env python3
"""
Fast CLI loader that uses pre-parsed cache to avoid importing all modules.
Only imports the specific module when a command is invoked.
"""
import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path

try:
    from cliche.abbrev import get_short_flags, build_var_names
    from cliche.docstring import parse_param_descriptions, get_description_without_params
except ImportError:
    from abbrev import get_short_flags, build_var_names
    from docstring import parse_param_descriptions, get_description_without_params


_KEY_ENUMS = {
    "Exchange", "Currency", "Side", "Service", "Location",
    "StrategyType", "Kind", "OrderType", "Report", "Datums",
    "DatumType", "Channel", "CommandType", "MDSChannel",
}


def compress_enums(enums: dict) -> dict:
    compressed = {}
    for name, values in enums.items():
        if name in _KEY_ENUMS:
            cleaned = [v for v in values if not v.startswith(("NULL_", "UNKNOWN_"))]
            if cleaned:
                compressed[name] = cleaned
    return compressed


# Module-level flag for --raw mode. Set in main() before parser / invoke runs.
# When True:
#   - color is suppressed everywhere (Colors.* returns unstyled)
#   - invoke_function prints the return value with plain `print(result)` instead
#     of `json.dumps(result, indent=2)` — so downstream `| jq`, `| awk`, pipes
#     into scripts, etc. get raw Python str output.
RAW_MODE = False


# Color formatting — disabled when output is not a TTY (piped/redirected)
def _supports_color(stream=None) -> bool:
    """Check if the given stream supports ANSI color codes."""
    if RAW_MODE:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if stream is None:
        stream = sys.stdout
    try:
        return hasattr(stream, "isatty") and stream.isatty()
    except ValueError:
        # Stream may be closed
        return False


class Colors:
    BLUE = "\x1b[1;36m"
    RED = "\x1b[1;31m"
    RESET = "\x1b[0m"

    @classmethod
    def blue(cls, text: str, stream=None) -> str:
        if _supports_color(stream):
            return f"{cls.BLUE}{text}{cls.RESET}"
        return text

    @classmethod
    def red(cls, text: str, stream=None) -> str:
        if _supports_color(stream):
            return f"{cls.RED}{text}{cls.RESET}"
        return text


def _cliche_version() -> str:
    """Read cliche's own package version lazily.

    Done on-demand (not at import) so the startup path of every user CLI
    doesn't pay for importlib.metadata (~20ms cold). Only --version, --cli,
    and cli_info() need this string.
    """
    try:
        from cliche import __version__ as v
        return v
    except ImportError:
        return "0.1.0"


def _resolve_pkg_version(pkg_name=None, install_dir=None):
    """Look up the user package's version string, or None if unknown.

    Tries (in order): `importlib.metadata.version(pkg_name)`, then the
    `version = "..."` line in the project's `pyproject.toml`. Returns None
    instead of raising — version info is purely informational.
    """
    if pkg_name:
        try:
            from importlib.metadata import version as _v, PackageNotFoundError
            try:
                return _v(pkg_name)
            except PackageNotFoundError:
                pass
        except Exception:
            pass

    # Fall back to reading pyproject.toml from the install directory.
    if install_dir:
        try:
            pyproject = os.path.join(os.path.dirname(install_dir), "pyproject.toml")
            if not os.path.exists(pyproject):
                pyproject = os.path.join(install_dir, "pyproject.toml")
            if os.path.exists(pyproject):
                with open(pyproject) as f:
                    m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']',
                                  f.read(), re.MULTILINE)
                    if m:
                        return m.group(1)
        except Exception:
            pass

    return None


def _collect_cli_info(cache_path=None, pkg_name=None, install_dir=None) -> list:
    """Return [(label, value), ...] describing the CLI + Python environment.

    Shared by --cli (human-readable dump) and --llm-help (commented header above
    the spec), so both surfaces stay in sync.
    """
    import os
    sv = sys.version_info
    python_version = f"{sv.major}.{sv.minor}.{sv.micro}"

    installed = False
    cli_dir = None
    try:
        with open(sys.argv[0]) as f:
            txt = f.read()
            installed = "cliche" in txt.lower() or "cli tool installed" in txt.lower()
            match = re.search(r'file_path = "([^"]+)"', txt)
            if match:
                cli_dir = match.group(1)
    except (FileNotFoundError, IOError):
        pass

    autocomplete = False
    name = os.path.basename(sys.argv[0])
    shell_configs = ["~/.bashrc", "~/.zshrc", "~/.bash_profile", "~/.zprofile"]
    for config in shell_configs:
        try:
            with open(os.path.expanduser(config)) as f:
                if f"register-python-argcomplete {name}" in f.read():
                    autocomplete = True
                    break
        except FileNotFoundError:
            pass

    python_dir = os.path.dirname(sys.executable)
    cache_display = cache_path or f"{sys.argv[0]}.json"

    pkg_version = _resolve_pkg_version(pkg_name, install_dir or cli_dir)

    info = [
        ("Executable", name),
        ("Executable path", sys.argv[0]),
        ("Cache path", str(cache_display)),
        ("Cliche version", _cliche_version()),
        ("Installed by cliche", str(installed)),
    ]
    if pkg_name:
        info.append(("Package name", pkg_name))
    if pkg_version:
        info.append(("Package version", pkg_version))
    if cli_dir:
        info.append(("CLI directory", cli_dir))
    info.extend([
        ("Autocomplete enabled", str(autocomplete)),
        ("Python Version", python_version),
        ("Python Interpreter", sys.executable),
        ("Python pip", f"{python_dir}/pip"),
    ])
    return info


def _docstring_style_summary(cache_path) -> str | None:
    """Return a `sphinx=N, google=N, ...` summary across all cached commands,
    or None if the cache isn't available."""
    try:
        from cliche.docstring import detect_style
    except ImportError:
        from docstring import detect_style
    try:
        if PRELOADED_CACHE is not None:
            data = PRELOADED_CACHE
        else:
            with open(cache_path or CACHE_PATH) as f:
                data = json.load(f)
    except (FileNotFoundError, IOError, json.JSONDecodeError):
        return None
    counts: dict[str, int] = {}
    for entry in data.get('files', {}).values():
        for func in entry.get('functions', ()):
            style = detect_style(func.get('docstring') or '')
            counts[style] = counts.get(style, 0) + 1
    if not counts:
        return None
    order = ('sphinx', 'google', 'numpy', 'freeform', 'missing')
    return ', '.join(f'{s}={counts[s]}' for s in order if counts.get(s))


def cli_info(cache_path=None, pkg_name=None, install_dir=None) -> None:
    """Outputs CLI and Python version info and exits."""
    print("CLI INFO:")
    for label, value in _collect_cli_info(cache_path, pkg_name, install_dir):
        print(f"  {label + ':':<21}", Colors.blue(value))
    summary = _docstring_style_summary(cache_path)
    if summary:
        print()
        print("DOCSTRING STYLES:")
        total = 0
        for part in summary.split(', '):
            style, count = part.split('=')
            print(f"  {style + ':':<21}", Colors.blue(count))
            total += int(count)
        print(f"  {'total:':<21}", Colors.blue(str(total)))


def colorize_help(message: str, stream=None) -> str:
    """Apply color formatting to help text (no-op when stream is not a TTY)."""
    if not _supports_color(stream):
        return message
    # Color the usage line
    lines = message.split("\n")
    if lines and lines[0].startswith("usage:"):
        lines[0] = Colors.blue(lines[0], stream)

    message = "\n".join(lines)

    # Color default values
    message = re.sub(
        r"Default:[^|]+",
        lambda m: Colors.blue(m.group(0)),
        message,
    )

    # Color short flags like -b, -c
    message = re.sub(
        r"(\n  -[a-zA-Z]),",
        lambda m: Colors.blue(m.group(1)) + ",",
        message,
    )

    # Color long flags like --base
    message = re.sub(
        r"(--[a-z0-9_-]+)",
        lambda m: Colors.blue(m.group(1)),
        message,
    )

    # Color -h, --help
    message = re.sub(
        r"(\n  -h, --help)",
        lambda m: Colors.blue(m.group(1)),
        message,
    )

    # Color positional argument choices {choice1,choice2,...}
    message = re.sub(
        r"(\n  \{[^}]+\})",
        lambda m: Colors.blue(m.group(1)),
        message,
    )

    return message


class CleanHelpFormatter(argparse.HelpFormatter):
    """Custom formatter that hides choices from usage line but shows in help."""

    def __init__(self, prog, indent_increment=2, max_help_position=24, width=None):
        super().__init__(prog, indent_increment, max_help_position, width)
        self._in_usage = False

    def _format_usage(self, usage, actions, groups, prefix):
        self._in_usage = True
        result = super()._format_usage(usage, actions, groups, prefix)
        self._in_usage = False
        return result

    def _metavar_formatter(self, action, default_metavar):
        # For choices in usage line, just show the dest name
        if self._in_usage and action.choices is not None:
            result = action.dest.upper()
            def format(tuple_size):
                if isinstance(result, tuple):
                    return result
                else:
                    return (result,) * tuple_size
            return format
        return super()._metavar_formatter(action, default_metavar)


class CleanArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with better error output - shows help before error."""

    llm_mode = False  # Class-level flag for LLM mode

    def __init__(self, *args, **kwargs):
        kwargs['formatter_class'] = CleanHelpFormatter
        super().__init__(*args, **kwargs)

    def print_help(self, file=None):
        if file is None:
            file = sys.stdout
        help_text = self.format_help()
        # Replace "options:" with "OPTIONS::"
        help_text = help_text.replace("options:", "OPTIONS::")
        help_text = help_text.replace("positional arguments:", "POSITIONAL ARGUMENTS:")
        help_text = colorize_help(help_text, stream=file)
        file.write(help_text)

    def error(self, message):
        if CleanArgumentParser.llm_mode:
            # Compact error for LLM consumption
            sys.stderr.write(f"error: {message}\n")
            sys.exit(2)
        # Print help first, then the error
        self.print_help(sys.stderr)
        sys.stderr.write(f"\n{Colors.red(message, stream=sys.stderr)}\n")
        sys.exit(2)

def _get_cache_dir() -> Path:
    """Get the cache directory using XDG_CACHE_HOME or ~/.cache fallback."""
    cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    cache_dir = Path(cache_home) / "cliche"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


# Default cache path (can be overridden by install_generator.py)
CACHE_PATH = _get_cache_dir() / "cli_cache.json"

# Source directory for cache regeneration (set by install_generator.py)
SOURCE_DIR = None

# Preloaded cache data (set by install_generator.py to avoid re-reading JSON)
PRELOADED_CACHE = None

# Project install directory (set by install_generator.py)
INSTALL_DIR = None

# User package name (set by runtime.run_package_cli when invoked as an
# installed user CLI). Used to look up the user's package version for
# `--version` / `--cli`. Remains None for ad-hoc `python run.py ...` runs.
PKG_NAME = None


class _LazyDefault:
    """Sentinel singleton indicating "evaluate the lazy-arg default fresh
    at dispatch time". Stored as the argparse `default=` for parameters
    whose default expression was recognised as a lazy Arg call (e.g.
    `DateArg("today")`). Replaced by a fresh Arg-class call just before
    the user function is invoked.
    """
    __slots__ = ()

    def __repr__(self) -> str:
        return "<lazy default>"


_LAZY_DEFAULT = _LazyDefault()


def load_cache():
    if PRELOADED_CACHE is not None:
        return PRELOADED_CACHE
    with open(CACHE_PATH) as f:
        return json.load(f)


def build_index(data):
    """Build lookup indices from cache data.

    Returns (commands, subcommands, enums, pydantic_models) — the fourth
    element is a set of class names the AST scanner identified as pydantic
    BaseModel subclasses. help_only mode uses this to decide whether an
    annotation is worth importing the user module to resolve.
    """
    commands = {}  # name -> func_info
    subcommands = {}  # group -> {name -> func_info}
    enums = data.get('enums', {})  # enum_name -> [values]
    pydantic_models: set = set(data.get('pydantic_models', []))

    for file_path, entry in data['files'].items():
        for func in entry['functions']:
            group = func.get('group')
            name = func['name'].replace('_', '-')
            func['cli_name'] = name

            if group:
                if group not in subcommands:
                    subcommands[group] = {}
                subcommands[group][name] = func
            else:
                commands[name] = func

    return commands, subcommands, enums, pydantic_models


def get_docstring_first_line(func):
    doc = func.get('docstring', '')
    if doc:
        return doc.split('\n')[0]
    return ''


def format_param_llm(param: dict) -> str:
    """Format a parameter for LLM-compact output: name?:type=default

    For bool params, show the actual flag to use:
    - default=true  -> --no-name (to disable)
    - default=false -> --name (to enable)
    """
    name = param['name']
    annotation = param.get('type_annotation', '')
    default = param.get('default')

    # Handle bool params specially - show the actual flag to use
    # Detect bool from annotation OR from default value being True/False
    is_bool = annotation and 'bool' in annotation.lower()
    if not is_bool and default is not None:
        is_bool = str(default).lower() in ('true', 'false')
    if is_bool and default is not None:
        default_lower = str(default).lower()
        flag_name = name.replace('_', '-')
        if default_lower == 'true':
            # Default is true, so --no-flag disables it
            return f'--no-{flag_name}'
        else:
            # Default is false, so --flag enables it
            return f'--{flag_name}'

    # Standard format for non-bool params
    parts = [name]
    if default is not None:
        parts[0] += '?'
    if annotation:
        parts.append(f':{annotation}')
    if default is not None:
        parts.append(f'={default}')

    return ''.join(parts)


def format_function_llm(func: dict, include_docstring: bool = True) -> str:
    """Format function as compact signature for LLM consumption."""
    name = func['name']
    params = func.get('parameters', [])

    # Filter out self/cls
    params = [p for p in params if p['name'] not in ('self', 'cls')]

    param_strs = [format_param_llm(p) for p in params]
    sig = f"{name}({', '.join(param_strs)})"

    # Add docstring as comment if present
    if include_docstring:
        doc = func.get('docstring', '')
        if doc:
            first_line = doc.strip().split('\n')[0].strip()
            if first_line and not first_line.startswith(':'):
                sig += f' # {first_line}'

    return sig


def print_llm_output(commands: dict, subcommands: dict, enums: dict,
                     filter_cmd: str = None, include_docstrings: bool = True,
                     prog_name: str = "run.py", install_dir: str = None, cwd: str = None,
                     output_format: str = "lines", cache_path=None, pkg_name=None):
    """Print compact LLM-friendly output."""
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Collect --cli env info once so both output formats embed the same
    # snapshot (version, interpreter, cache, autocomplete, etc.) alongside
    # the command spec.
    env_info = _collect_cli_info(cache_path, pkg_name=pkg_name, install_dir=install_dir)
    # The existing header already carries install_dir; drop it from the env
    # block to avoid repeating the same value.
    env_info = [(k, v) for (k, v) in env_info if k != "CLI directory"]

    if output_format == "json":
        _print_llm_output_json(commands, subcommands, enums, filter_cmd,
                               include_docstrings, prog_name, install_dir, cwd, timestamp,
                               env_info)
    else:
        _print_llm_output_lines(commands, subcommands, enums, filter_cmd,
                                include_docstrings, prog_name, install_dir, cwd, timestamp,
                                env_info)


def _print_llm_output_json(commands: dict, subcommands: dict, enums: dict,
                           filter_cmd: str, include_docstrings: bool,
                           prog_name: str, install_dir: str, cwd: str, timestamp: str,
                           env_info: list = None):
    """Print LLM output in minified JSON format."""
    output = {
        '_': (
            'Define: from cliche import cli; @cli decorator on typed functions auto-registers them. '
            f'{prog_name} scans for @cli and builds CLI. '
            f'Run: {prog_name} <cmd> [args] (space-separated, NOT colon). '
            'Ignore module paths in fn keys - use only the rightmost group and function name. '
            f'Example: devops.instruments:{{overview(...)}} → {prog_name} instruments overview. '
            'Syntax: fn(pos:Type, opt?:Type=default). '
            'No ? = positional arg (pass value directly). '
            '? = optional flag: use --name value (convert underscores to dashes). '
            f'Example: mds(exchange:X, base?:Y=Z) → {prog_name} mds binance_usdm --base BTC. '
            'bool? flags: --flag to enable, omit to use default. '
            'Lists/tuples/sets/frozensets: space-separated after flag (--items a b c). set/frozenset dedupe and do not preserve order. '
            'Output: stdout from print() is shown as-is; a non-None return value is auto-printed as JSON (or plain with --raw). '
            'Date/datetime defaults: `day: date = DateUtcArg("today")` / `when: datetime = DateTimeUtcArg("now")` re-eval per invocation; also "yesterday","tomorrow","+Nd","-Nd","+Nh","+Nm","YYYY-MM-DD". Non-Utc variants (DateArg/DateTimeArg) use local clock. Import from cliche. '
            'E section lists valid enum values. '
            f'For per-command detail (signature, types, defaults, docstrings) run: {prog_name} <cmd> --llm-help '
            f'(or {prog_name} <group> <cmd> --llm-help for subcommands).'
        ),
        'ts': timestamp,
        'install_dir': install_dir or '',
        'cwd': cwd or '',
        'env': {k: v for (k, v) in (env_info or [])},
        'fn': {},
        'E': {},
    }

    # Process commands (ungrouped functions)
    ungrouped = []
    for name, func in sorted(commands.items()):
        if filter_cmd and func['name'] != filter_cmd.replace('-', '_'):
            continue
        ungrouped.append(format_function_llm(func, include_docstrings))

    if ungrouped and not filter_cmd:
        output['fn']['_'] = ungrouped
    elif ungrouped:
        # Single command filter - just print the signature
        for sig in ungrouped:
            print(sig)
        return

    # Process subcommands (grouped functions)
    for group, funcs in sorted(subcommands.items()):
        if filter_cmd and filter_cmd != group:
            continue
        group_sigs = []
        for name, func in sorted(funcs.items()):
            group_sigs.append(format_function_llm(func, include_docstrings))
        if group_sigs:
            output['fn'][group] = group_sigs

    # Add enums (filtered to key enums only)
    output['E'] = compress_enums(enums)

    # Add global options
    output['opts'] = {
        '--pdb': 'Drop into debugger on error',
        '--pip [args]': "Run pip for this CLI's Python environment (e.g. --pip install pkg)",
        '--uv [args]': "Run uv targeting this CLI's Python environment (e.g. --uv pip install pkg, --uv sync)",
        '--pyspy N': 'Profile for N seconds with py-spy (speedscope JSON output)',
        '--raw': 'Print return value as-is (no JSON, no color) — good for pipes',
        '--notraceback': 'On error, print only ExcName: message (no traceback)',
        '--timing': 'Show timing information',
        '--skip-gen': 'Skip cache regeneration',
    }

    # Output minified JSON
    print(json.dumps(output, separators=(',', ':')))


def _print_llm_output_lines(commands: dict, subcommands: dict, enums: dict,
                            filter_cmd: str, include_docstrings: bool,
                            prog_name: str, install_dir: str, cwd: str, timestamp: str,
                            env_info: list = None):
    """Print LLM output in line-based format (~10% fewer tokens than JSON)."""
    lines = []

    # Header with full instructions
    lines.append(f"# {prog_name} CLI - Run: {prog_name} <cmd> [args] (space-separated)")
    lines.append(f"# Syntax: fn(pos:Type, opt?:Type=default). No ? = positional arg. ? = optional --flag value.")
    lines.append(f"# Bool flags shown as --flag or --no-flag (use as-is to toggle). Lists/tuples/sets/frozensets: --items a b c (space-separated; set/frozenset dedupe + unordered).")
    lines.append(f'# Date defaults: `day: date = DateUtcArg("today")` / `when: datetime = DateTimeUtcArg("now")` (also "yesterday","+Nd","-Nh","YYYY-MM-DD"; non-Utc variants use local clock).')
    lines.append(f"# Output: any print() inside the function goes to stdout; a non-None return value is auto-printed (JSON by default, plain with --raw).")
    lines.append(f"# For subcommands: {prog_name} <group> <function> [args]. Example: {prog_name} instruments overview")
    lines.append(f"# Per-command detail (full signature, types, defaults, docstrings): {prog_name} <cmd> --llm-help  or  {prog_name} <group> <cmd> --llm-help")
    lines.append(f"# now: {timestamp} | working_directory: {cwd or ''}")
    if install_dir:
        lines.append(f"# install_dir: {install_dir}")
    # --cli env snapshot (same info as `<prog> --cli`, inlined so an LLM
    # sees versions / interpreter / cache / autocomplete alongside the spec).
    for label, value in (env_info or []):
        lines.append(f"# {label}: {value}")
    lines.append("")

    # Process commands (ungrouped functions)
    ungrouped = []
    for name, func in sorted(commands.items()):
        if filter_cmd and func['name'] != filter_cmd.replace('-', '_'):
            continue
        ungrouped.append(format_function_llm(func, include_docstrings))

    if ungrouped and not filter_cmd:
        lines.append("## commands")
        lines.extend(ungrouped)
        lines.append("")
    elif ungrouped:
        # Single command filter - just print the signature
        for sig in ungrouped:
            print(sig)
        return

    # Process subcommands (grouped functions)
    for group, funcs in sorted(subcommands.items()):
        if filter_cmd and filter_cmd != group:
            continue
        group_sigs = []
        for name, func in sorted(funcs.items()):
            group_sigs.append(format_function_llm(func, include_docstrings))
        if group_sigs:
            lines.append(f"## subcommand: {group}")
            lines.extend(group_sigs)
            lines.append("")

    # Add global options
    lines.append("## options")
    lines.append("--pdb: Drop into debugger on error")
    lines.append("--pip [args]: Run pip for this CLI's Python env (e.g. --pip install pkg)")
    lines.append("--uv [args]: Run uv targeting this CLI's Python env (e.g. --uv pip install pkg, --uv sync)")
    lines.append("--pyspy N: Profile for N seconds with py-spy (speedscope JSON output)")
    lines.append("--raw: Print return value as-is (no JSON, no color) — good for pipes")
    lines.append("--notraceback: On error, print only ExcName: message (no traceback)")
    lines.append("--timing: Show timing information")
    lines.append("--skip-gen: Skip cache regeneration")
    lines.append("")

    # Add enums
    enums_compressed = compress_enums(enums)
    if enums_compressed:
        lines.append("## enums")
        for enum_name, values in sorted(enums_compressed.items()):
            lines.append(f"{enum_name}: {' '.join(values)}")

    print('\n'.join(lines))


def print_llm_command_help(func: dict, prog_name: str, cmd: str, group: str = None):
    """Print LLM-friendly help for a single @cli function, listing every param."""
    doc = func.get('docstring', '') or ''
    param_descs = parse_param_descriptions(doc)
    clean_desc = get_description_without_params(doc)

    params = [p for p in func.get('parameters', [])
              if p['name'] not in ('self', 'cls')
              and not p.get('is_args') and not p.get('is_kwargs')]
    positional = [p for p in params if p.get('default') is None]
    optional = [p for p in params if p.get('default') is not None]

    full_cmd = f"{group} {cmd}" if group else cmd

    print(f"# {prog_name} {full_cmd} — LLM help")
    print(f"# Syntax: pos:Type (required positional), opt?:Type=default (use --opt value, underscores->dashes).")
    print(f"# Bool: --flag to enable (default False) / --no-flag to disable (default True). Lists/tuples/sets/frozensets: space-separated.")
    if clean_desc:
        first = clean_desc.strip().splitlines()[0].strip()
        if first:
            print(f"# {first}")

    usage = [prog_name, full_cmd]
    for p in positional:
        usage.append(p['name'].upper())
    for p in optional:
        flag = p['name'].replace('_', '-')
        annotation = p.get('type_annotation', '')
        is_bool = (annotation and 'bool' in annotation.lower()) or \
                  str(p.get('default', '')).lower() in ('true', 'false')
        if is_bool:
            if str(p.get('default', '')).lower() == 'true':
                usage.append(f"[--no-{flag}]")
            else:
                usage.append(f"[--{flag}]")
        else:
            usage.append(f"[--{flag} VAL]")
    print(f"usage: {' '.join(usage)}")
    print()

    def _line(p):
        base = format_param_llm(p)
        desc = param_descs.get(p['name'], '')
        return f"{base}  # {desc}" if desc else base

    if positional:
        print("## positional (required, pass values directly)")
        for p in positional:
            print(_line(p))
        print()

    if optional:
        print("## optional (flags)")
        for p in optional:
            print(_line(p))
        print()

    print("## global options")
    print("--pdb: debugger on error | --pyspy N: profile Ns | --raw: plain output (no JSON/color)")
    print("--notraceback: terse errors | --timing: timing info | --llm-help: this view")
    print(f"# Top-level only (run on `{prog_name}` itself): --version, --cli, --pip, --uv, --skip-gen — see `{prog_name} --llm-help`")




def print_help(commands, subcommands, prog_name: str = "run.py"):
    """Print help similar to 1one --help."""
    print(f"{Colors.blue(f'usage: {prog_name} [-h] [--llm-help] [--pdb] [--pip] [--uv] [--pyspy N] [--timing] COMMAND ...')}\n")
    print("COMMANDS:")
    for name in sorted(commands.keys()):
        doc = get_docstring_first_line(commands[name])
        padded_name = f"    {name:20}"
        if doc:
            print(f"{Colors.blue(padded_name)}{doc[:50]}")
        else:
            print(Colors.blue(padded_name.rstrip()))

    print("\nSUBCOMMANDS:")
    for group in sorted(subcommands.keys()):
        funcs = sorted(subcommands[group].keys())
        padded_group = f"    {group:16}"
        print(f"{Colors.blue(padded_group)}({', '.join(funcs)})")

    print("\nCLICHE OPTIONS:")
    print(f"  {Colors.blue('-h')}, {Colors.blue('--help')}    Show this help message")
    print(f"  {Colors.blue('--version')}     Print the package version and exit")
    print(f"  {Colors.blue('--cli')}         Show CLI and Python version info (including package version)")
    print(f"  {Colors.blue('--llm-help')}         Show compact LLM-friendly help output")
    print(f"  {Colors.blue('--pdb')}         Drop into debugger on error")
    print(f"  {Colors.blue('--pip')}         Run pip for this CLI's Python environment")
    print(f"  {Colors.blue('--uv')}          Run uv targeting this CLI's Python environment")
    print(f"  {Colors.blue('--pyspy N')}     Profile for N seconds with py-spy (speedscope format)")
    print(f"  {Colors.blue('--raw')}         Print return value as-is (no JSON, no color)")
    print(f"  {Colors.blue('--notraceback')} On error, print only ExcName: message")
    print(f"  {Colors.blue('--skip-gen')}    Skip cache regeneration")
    print(f"  {Colors.blue('--timing')}      Show timing information")


def simplify_type_annotation(annotation: str) -> str:
    """Simplify type annotation for display (e.g., 'str | None' -> 'str', 'Currency.V' -> 'Currency')."""
    if not annotation:
        return annotation

    # Remove ' | None' or '| None' from union types
    simplified = re.sub(r'\s*\|\s*None\b', '', annotation)
    # Also handle 'None | X' -> 'X'
    simplified = re.sub(r'\bNone\s*\|\s*', '', simplified)
    # Handle Optional[X] -> X
    if simplified.startswith('Optional[') and simplified.endswith(']'):
        simplified = simplified[9:-1]
    # Remove .V suffix from enum types (e.g., Currency.V -> Currency)
    simplified = re.sub(r'\.V\b', '', simplified)
    return simplified.strip()


def is_multi_value_type(annotation: str) -> bool:
    """Check if the type annotation suggests multiple values (tuple, list, set, frozenset)."""
    if not annotation:
        return False
    lower = annotation.lower()
    return (lower.startswith('tuple[') or lower.startswith('list[')
            or lower.startswith('set[') or lower.startswith('frozenset['))


def _parse_date(s: str):
    """Accept YYYY-MM-DD (strict) for argparse `type=`."""
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_datetime(s: str):
    """Accept ISO-8601 (what fromisoformat handles) for argparse `type=`."""
    from datetime import datetime
    return datetime.fromisoformat(s)


def _parse_dict_annotation(annotation: str | None) -> tuple | None:
    """Extract (key_type, value_type) from `dict[K, V]` / `Dict[K, V]`.

    Returns (key_conv, value_conv) callables that argparse's `type=` can use
    per-element via _DictAction below. Returns None if the annotation isn't a
    parametrised dict (including the bare `dict` — no key/value info to use).
    """
    if not annotation:
        return None
    s = annotation.strip()
    # Match dict[K, V] AND dict[(K, V)] — the AST expr_to_string wraps a
    # subscript-tuple slice in parens, so `dict[str, int]` in source becomes
    # the string `dict[(str, int)]` after unparse. Tolerate both.
    m = re.match(
        r'^(?:dict|Dict)\[\s*\(?\s*([^,\]\(\)]+?)\s*,\s*([^,\]\(\)]+?)\s*\)?\s*\]\s*$',
        s,
    )
    if not m:
        return None
    # Keep in sync with type_from_annotation's type_map — currently just the
    # primitive + Path set, since dict key/value types are nearly always one
    # of these. Unknown names fall through to str (argparse will then accept
    # raw strings, which is the safest default for user-defined types).
    prim = {'str': str, 'int': int, 'float': float, 'bool': bool,
            'Path': Path, 'pathlib.Path': Path}
    key_conv = prim.get(m.group(1).strip(), str)
    val_conv = prim.get(m.group(2).strip(), str)
    return key_conv, val_conv


class _DictAction(argparse.Action):
    """Collect `--opt k=v --opt k2=v2` (or `--opt k=v k2=v2` with nargs) into a dict.

    The key/value converters come from the annotation — e.g. `dict[str, int]`
    gives str keys and int values. Raises ArgumentError on bad format so the
    user sees the standard argparse error, not a traceback.
    """
    def __init__(self, *args, key_type=str, value_type=str, **kwargs):
        self._key_type = key_type
        self._value_type = value_type
        super().__init__(*args, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        result = getattr(namespace, self.dest, None) or {}
        items = values if isinstance(values, list) else [values]
        for item in items:
            if '=' not in item:
                raise argparse.ArgumentError(
                    self, f"expected KEY=VALUE, got {item!r}"
                )
            k, v = item.split('=', 1)
            try:
                k_conv = self._key_type(k)
                v_conv = self._value_type(v)
            except (ValueError, TypeError) as e:
                raise argparse.ArgumentError(self, f"bad key/value {item!r}: {e}")
            result[k_conv] = v_conv
        setattr(namespace, self.dest, result)


def type_from_annotation(annotation: str):
    """Convert type annotation string to Python type."""
    if annotation is None:
        return str

    # Strip surrounding quotes from string annotations (e.g. '"pathlib.Path"' -> 'pathlib.Path')
    if len(annotation) >= 2:
        if (annotation[0] == '"' and annotation[-1] == '"') or \
           (annotation[0] == "'" and annotation[-1] == "'"):
            annotation = annotation[1:-1]

    # Handle common types. Date/datetime parsers live here so `x: date` on a
    # @cli function gets "2026-04-22" coerced to a real `date` object (and
    # datetime gets ISO parsing) instead of being passed as a raw str.
    type_map = {
        'str': str,
        'int': int,
        'float': float,
        'bool': bool,
        'Path': Path,
        'pathlib.Path': Path,
        'date': _parse_date,
        'datetime.date': _parse_date,
        'datetime': _parse_datetime,
        'datetime.datetime': _parse_datetime,
    }

    # Handle Optional, List, etc.
    if annotation.startswith('Optional['):
        inner = annotation[9:-1]
        return type_map.get(inner, str)

    # Containers: extract the element type so argparse `type=` coerces each
    # element. `list[Path]` → Path, `tuple[int, ...]` → int, etc. Plain `str`
    # falls through to no-op coercion. Enums stay as str here — they are
    # converted in convert_enum_args after parsing.
    # Note: the AST unparse can wrap tuple-subscript args in parens —
    # `tuple[int, ...]` comes in as `"tuple[(int, ...)]"`. Strip a leading
    # `(` and a trailing `)` from the inner slice before splitting.
    if annotation.startswith('list[') or annotation.startswith('List['):
        inner = annotation[annotation.index('[') + 1 : annotation.rindex(']')].strip()
        inner = inner.strip('()').strip()
        inner = inner.split(',')[0].strip()
        return type_map.get(inner, str)

    if annotation.startswith('tuple[') or annotation.startswith('Tuple['):
        inner = annotation[annotation.index('[') + 1 : annotation.rindex(']')].strip()
        inner = inner.strip('()').strip()
        # `tuple[int, ...]` — the element type is the first element.
        inner = inner.split(',')[0].strip()
        return type_map.get(inner, str)

    if (annotation.startswith('set[') or annotation.startswith('Set[')
            or annotation.startswith('frozenset[') or annotation.startswith('FrozenSet[')):
        inner = annotation[annotation.index('[') + 1 : annotation.rindex(']')].strip()
        inner = inner.strip('()').strip()
        inner = inner.split(',')[0].strip()
        return type_map.get(inner, str)

    # PEP 604 unions (`A | B [| C ...]`). Pick the most-specific mapped type
    # across parts, dropping None (only the *provided* value needs coercion).
    # Examples:
    #   Path | None  → Path
    #   None | Path  → Path
    #   str | Path   → Path   (Path more specific than str)
    #   Path | str   → Path   (first match wins when neither is str)
    #   int | float  → int    (first mapped wins)
    if ' | ' in annotation:
        parts = [p.strip() for p in annotation.split(' | ') if p.strip() != 'None']
        best = None
        for p in parts:
            t = type_map.get(p)
            if t is None:
                continue
            if best is None or best is str:  # anything beats unset or plain str
                best = t
        if best is not None:
            return best
        # Fall through to str if no part was in type_map.

    return type_map.get(annotation, str)


def parse_default(default_str: str, param_type):
    """Parse default value string to Python value."""
    if default_str is None:
        return None

    if default_str == 'None':
        return None
    if default_str == 'True' or default_str == 'true':
        return True
    if default_str == 'False' or default_str == 'false':
        return False

    # Handle tuple/list literals
    if default_str.startswith('(') and default_str.endswith(')'):
        import ast
        try:
            val = ast.literal_eval(default_str)
            if isinstance(val, (tuple, list)):
                return val
        except (ValueError, SyntaxError):
            pass
    if default_str.startswith('[') and default_str.endswith(']'):
        import ast
        try:
            val = ast.literal_eval(default_str)
            if isinstance(val, (tuple, list)):
                return val
        except (ValueError, SyntaxError):
            pass

    # Try to evaluate simple literals
    try:
        if param_type == int:
            return int(default_str)
        if param_type == float:
            return float(default_str)
    except ValueError:
        pass

    # String default (strip quotes)
    if default_str.startswith('"') and default_str.endswith('"'):
        return default_str[1:-1]
    if default_str.startswith("'") and default_str.endswith("'"):
        return default_str[1:-1]

    # Path("/tmp") / pathlib.Path('/tmp') call-form default. Extract the
    # single string-literal arg; argparse then applies `type=Path` to the
    # resulting string default and the function receives a real Path.
    # Non-literal inner expressions (e.g. `Path("/tmp") / "sub"`) don't
    # match and fall through — users with computed defaults still need
    # the sentinel-inside-the-function workaround.
    m = re.match(
        r'^(?:pathlib\.)?Path\(\s*["\']([^"\']*)["\']\s*\)$',
        default_str,
    )
    if m:
        return m.group(1)

    return default_str


def get_enum_from_annotation(annotation: str, enums: dict) -> list[str] | None:
    """Extract enum choices from type annotation like 'Exchange.V' or 'Exchange'."""
    if not annotation:
        return None

    # Handle patterns like "Exchange.V", "Exchange", "list[Exchange.V]"
    # Extract the enum name - prefer longest match to handle cases like
    # "MDSChannel" vs "Channel" where both might match
    best_match = None
    best_len = 0

    for enum_name in enums:
        # Quick check: enum name must appear in annotation
        if enum_name not in annotation:
            continue
        # Verify it's a word boundary match (not substring of another word)
        idx = annotation.find(enum_name)
        if idx > 0 and annotation[idx-1].isalnum():
            continue  # Part of a longer word
        end_idx = idx + len(enum_name)
        if end_idx < len(annotation) and annotation[end_idx].isalnum() and annotation[end_idx] != '.':
            continue  # Part of a longer word (but allow .V suffix)
        if len(enum_name) > best_len:
            best_match = enum_name
            best_len = len(enum_name)

    if best_match:
        return enums[best_match]
    return None


def _resolve_callable_type(annotation: str, module_name: str):
    """Resolve an unknown annotation to a user-defined callable in its module.

    Covers the "custom type" escape hatch: a user can write their own
    validator/parser function (or class) and use it as an annotation; argparse
    will call it per token via `type=`. Example:
        def Port(s: str) -> int:
            n = int(s)
            if not (1 <= n <= 65535):
                raise argparse.ArgumentTypeError(f"port out of range: {n}")
            return n

        @cli
        def serve(port: Port): ...

    Guard rails:
      - Only triggers for simple identifiers (`Port`, `URL`, `PositiveInt`).
        Parameterised forms (`list[Port]`, `Port | None`) fall through — the
        existing container/union code already decides whether to call us.
      - Skips names that are Enum subclasses or pydantic BaseModels —
        those have their own dedicated handling paths that do richer work.
      - Returns None on any resolution failure (missing module, missing
        attribute, non-callable) — caller keeps the `str` fallback.
    """
    if not annotation or not module_name:
        return None
    name = annotation.strip()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return None
    # Don't shadow the existing primitive / enum / pydantic paths.
    if name in {'str', 'int', 'float', 'bool', 'Path', 'date', 'datetime',
                'Optional', 'list', 'tuple', 'dict', 'List', 'Tuple', 'Dict'}:
        return None
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return None
    obj = getattr(mod, name, None)
    if obj is None or not callable(obj):
        return None
    # Let enums and pydantic models stay on their dedicated paths.
    try:
        import enum
        if isinstance(obj, type) and issubclass(obj, enum.Enum):
            return None
    except Exception:
        pass
    if _is_pydantic_model(obj):
        return None
    return obj


def _annotation_pydantic_name(annotation: str, pydantic_models) -> str | None:
    """Return the class name referenced by ``annotation`` if it's in
    ``pydantic_models`` — else None. Peels the same wrappers
    ``_resolve_annotation_class`` would (Optional, `| None`, list[X], dotted
    attribute access). Cheap string work, no imports.
    """
    if not annotation or not pydantic_models:
        return None
    s = annotation.strip()
    if s.startswith('Optional[') and s.endswith(']'):
        s = s[len('Optional['):-1].strip()
    s = re.sub(r'\s*\|\s*None\b', '', s)
    s = re.sub(r'\bNone\s*\|\s*', '', s).strip()
    m = re.match(r'^(?:list|List|tuple|Tuple)\[([^,\]]+)', s)
    if m:
        s = m.group(1).strip()
    cls_name = s.split('.')[-1]
    if cls_name in pydantic_models:
        return cls_name
    return None


def _resolve_annotation_class(annotation: str | None, module_name: str):
    """Best-effort: resolve a type-annotation string to the class object.

    Returns None on any failure (module doesn't import, name doesn't resolve,
    annotation is a complex expression). We strip one level of `list[...]`,
    `Optional[...]`, `X | None` wrappers — that's enough for the pydantic case
    where the user wrote `cfg: MyConfig` or `cfg: MyConfig | None`.
    """
    if not annotation or not module_name:
        return None
    s = annotation.strip()
    # Peel Optional[X] / `X | None` / `None | X`
    if s.startswith('Optional[') and s.endswith(']'):
        s = s[len('Optional['):-1].strip()
    s = re.sub(r'\s*\|\s*None\b', '', s)
    s = re.sub(r'\bNone\s*\|\s*', '', s).strip()
    # Peel list[X] / tuple[X, ...] — pydantic-list args are rare; still try for the inner type
    m = re.match(r'^(?:list|List|tuple|Tuple)\[([^,\]]+)', s)
    if m:
        s = m.group(1).strip()
    # Attribute access like `pkg.models.MyModel` — keep only the tail, importlib already gave us the module
    cls_name = s.split('.')[-1]
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', cls_name):
        return None
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return None
    return getattr(mod, cls_name, None)


def _is_pydantic_model(cls) -> bool:
    """True if cls subclasses pydantic.BaseModel (v1 or v2)."""
    import inspect
    if cls is None or not inspect.isclass(cls):
        return False
    try:
        return any(b.__name__ == 'BaseModel' for b in cls.__mro__)
    except Exception:
        return False


def _pydantic_fields(model_cls):
    """Return [(field_name, type_cls, default, required)] for a BaseModel.

    Handles both pydantic v2 (`model_fields` + FieldInfo) and v1 (`__fields__`
    + ModelField). Unknown / complex field types fall back to str, so argparse
    still does something sensible.
    """
    # v2
    model_fields = getattr(model_cls, 'model_fields', None)
    if model_fields:
        try:
            from pydantic_core import PydanticUndefined
        except ImportError:
            PydanticUndefined = object()
        out = []
        for fname, info in model_fields.items():
            ftype = getattr(info, 'annotation', None) or str
            fdefault = getattr(info, 'default', PydanticUndefined)
            required = fdefault is PydanticUndefined
            out.append((fname, ftype, None if required else fdefault, required))
        return out
    # v1
    legacy = getattr(model_cls, '__fields__', None)
    if legacy:
        out = []
        for fname, field in legacy.items():
            ftype = getattr(field, 'type_', None) or str
            fdefault = getattr(field, 'default', None)
            required = bool(getattr(field, 'required', False))
            out.append((fname, ftype, fdefault, required))
        return out
    return []


def build_parser_for_function(func, enums=None, prog_name: str = "run.py", help_only: bool = False, pydantic_models=None):
    """Build argparse parser for a specific function.

    ``help_only=True`` skips user-module imports used only by argparse's
    ``type=`` converter (``_resolve_callable_type``) — argparse's
    ``action='help'`` exits before that code path runs. For pydantic
    expansion we still need field metadata, so we consult ``pydantic_models``
    (names detected by the AST scanner at cache-build time) and only import
    the user module when an annotation actually references one. The
    combination: fast help for common cases, correct expanded help for
    pydantic-using functions — no user-module import when no pydantic model
    is involved.
    """
    # ``pydantic_models=None`` means "caller didn't supply scanner data" —
    # fall back to resolving every annotation (the pre-scanner behavior).
    # An explicit (possibly empty) set means "use the scanner gate", so
    # annotations not in the set skip `_resolve_annotation_class` and avoid
    # importing the user module. Real CLI invocations from ``main()`` always
    # pass the set; test callers that don't pass anything keep working.
    _use_pyd_gate = pydantic_models is not None
    if pydantic_models is None:
        pydantic_models = set()
    if enums is None:
        enums = {}

    doc = func.get('docstring', '')
    # Parse :param descriptions and use cleaned docstring
    param_descs = parse_param_descriptions(doc)
    clean_desc = get_description_without_params(doc)

    parser = CleanArgumentParser(
        prog=f"{prog_name} {func['cli_name']}",
        description=clean_desc or None,
        add_help=False,
    )

    # Add global CLI flags. Per-command help intentionally omits flags that
    # only make sense at the top level (--version, --cli, --pip, --uv,
    # --skip-gen) — those are listed by `<prog> --help` / `<prog> --llm-help`.
    global_group = parser.add_argument_group('CLICHE OPTIONS')
    global_group.add_argument('-h', '--help', action='help', help='Show this help message')
    global_group.add_argument('--llm-help', action='store_true', help="Show this command's compact LLM-friendly help")
    global_group.add_argument('--pdb', action='store_true', help='Drop into debugger on error')
    global_group.add_argument('--pyspy', type=int, default=0, metavar='N', help='Profile for N seconds with py-spy (speedscope format)')
    global_group.add_argument('--raw', action='store_true', help='Print return value as-is (no JSON pretty-print, no color) — good for pipes')
    global_group.add_argument('--notraceback', action='store_true', help='On error, print only ExcName: message (no traceback)')
    global_group.add_argument('--timing', action='store_true', help='Show timing information')

    params = func.get('parameters', [])

    # Get short flags for all parameters
    short_flags = get_short_flags(params)

    # Pydantic bindings collected during build — invoke_function consults this
    # to reassemble model instances from the flat field args argparse produces.
    pydantic_binds = []  # [(param_name, model_cls, [field_names])]
    module_name = func.get('module', '')

    for param in params:
        name = param['name']

        # Skip self, cls, *args, **kwargs
        if name in ('self', 'cls') or param.get('is_args') or param.get('is_kwargs'):
            continue

        annotation = param.get('type_annotation')
        default_str = param.get('default')
        lazy_arg = param.get('lazy_arg')   # set by AST scanner for DateArg("today") etc.

        param_type = type_from_annotation(annotation)
        # Custom type callables: if the annotation is an unknown simple name
        # (type_from_annotation fell back to str) and the function's module
        # defines a matching callable, use that as argparse `type=`. Lets
        # users plug in `Port`, `PositiveInt`, `URL`, etc. without pydantic:
        #     def Port(s: str) -> int: ...
        #     @cli
        #     def serve(port: Port): ...
        # argparse wraps any ValueError / ArgumentTypeError into a clean
        # "argument port: invalid Port value: '0'" error.
        if param_type is str and annotation and not help_only:
            resolved = _resolve_callable_type(annotation, module_name)
            if resolved is not None:
                param_type = resolved

        # Lazy-arg defaults (e.g. `day: date = DateArg("today")`):
        #  - argparse `type=` is overridden to the Arg class itself, so
        #    user-supplied --day values get the same rich grammar as the
        #    default expression ("today", "-7d", ISO, etc.).
        #  - default is set to the _LAZY_DEFAULT sentinel here; invoke_function
        #    replaces it with a fresh `DateArg("today")` call at dispatch time
        #    so every CLI invocation resolves "today" as of that moment,
        #    regardless of when the module was imported.
        if lazy_arg:
            from cliche import types as _nc_types
            arg_cls = getattr(_nc_types, lazy_arg["cls"], None)
            if arg_cls is not None:
                param_type = arg_cls
                default = _LAZY_DEFAULT
                default_str = "lazy"   # nonempty -> treated as "has default"
            else:
                default = parse_default(default_str, param_type)
        else:
            default = parse_default(default_str, param_type)

        # Determine if positional or optional
        has_default = default_str is not None
        short_flag = short_flags.get(name)

        # Check if this is an enum type
        enum_choices = get_enum_from_annotation(annotation, enums)

        # Get param description from docstring
        param_desc = param_descs.get(name, '')

        # Dict[K, V]: accept `--opt k=v` (repeatable) and build a dict. Must
        # run before the pydantic / positional / optional branches because dict
        # annotations look like "regular" types to everything else and would
        # end up as a plain string otherwise.
        dict_types = _parse_dict_annotation(annotation) if annotation else None
        if dict_types:
            key_conv, val_conv = dict_types
            display = simplify_type_annotation(annotation) if annotation else 'dict'
            help_text = f'|{display}| KEY=VALUE (repeatable) |'
            if param_desc:
                help_text = f'{help_text} {param_desc}'
            if has_default:
                var_names = build_var_names(name, short_flag, has_default=True)
                parser.add_argument(
                    *var_names, dest=name, action=_DictAction,
                    key_type=key_conv, value_type=val_conv,
                    default=default if isinstance(default, dict) else {},
                    nargs='*', metavar='KEY=VALUE', help=help_text,
                )
            else:
                parser.add_argument(
                    name, action=_DictAction, key_type=key_conv, value_type=val_conv,
                    nargs='+', metavar='KEY=VALUE', help=help_text,
                )
            continue

        # Pydantic: if the annotation names a BaseModel, expand its fields as
        # --<field> flags in a dedicated argument group and record the binding
        # so invoke_function can reconstruct the model instance. Skips enums
        # and primitive types — only triggers for real BaseModel subclasses.
        # With the scanner gate enabled, only import the user module when
        # the annotation names a class the AST scanner flagged as pydantic.
        # For every other annotation (str, int, Path, enums, custom type
        # callables) there's no pydantic expansion to do, and
        # _resolve_callable_type already handles the custom-type `type=`
        # path with its own primitive-name guard. This cuts ~hundreds of ms
        # off `cmd --help` AND `cmd` (missing-arg errors) for CLIs with
        # heavy transitive dependencies. Without the gate (test callers
        # that don't pass pydantic_models), keep the legacy always-resolve
        # path so behavior is backward compatible.
        if not annotation:
            annotation_cls = None
        elif _use_pyd_gate:
            pyd_candidate = _annotation_pydantic_name(annotation, pydantic_models)
            annotation_cls = (
                _resolve_annotation_class(annotation, module_name)
                if pyd_candidate else None
            )
        else:
            annotation_cls = _resolve_annotation_class(annotation, module_name)
        if _is_pydantic_model(annotation_cls):
            group = parser.add_argument_group(
                f'{annotation_cls.__name__} (bound to `{name}`)',
                description=param_desc or None,
            )
            field_names = []
            for fname, ftype, fdefault, frequired in _pydantic_fields(annotation_cls):
                flag = f'--{fname.replace("_", "-")}'
                # Map basic types to argparse type converters; unknown types fall back to str.
                ftype_conv = ftype if ftype in (str, int, float, bool) else str
                type_label = getattr(ftype, '__name__', str(ftype))
                if ftype is bool:
                    # Default reflects the FLAG, not the underlying param (see
                    # non-pydantic bool branch above for the reasoning).
                    if fdefault is True:
                        group.add_argument(
                            f'--no-{fname.replace("_", "-")}', dest=fname,
                            action='store_false', default=True,
                            help='|bool| Default: False |',
                        )
                    else:
                        group.add_argument(
                            flag, dest=fname, action='store_true',
                            default=False if fdefault is None else fdefault,
                            help=f'|bool| Default: {fdefault if fdefault is not None else False} |',
                        )
                elif frequired:
                    group.add_argument(
                        flag, dest=fname, type=ftype_conv, required=True,
                        help=f'|{type_label}| (required) |',
                    )
                else:
                    group.add_argument(
                        flag, dest=fname, type=ftype_conv, default=fdefault,
                        help=f'|{type_label}| Default: {fdefault} |',
                    )
                field_names.append(fname)
            pydantic_binds.append((name, annotation_cls, field_names))
            continue

        if param_type == bool:
            # Boolean flags. The help's `Default: X` describes the FLAG, not
            # the underlying param: for `--no-use-cache` the flag is off by
            # default (cache stays on), so Default: False reads correctly.
            # For `--verbose` the flag is off by default too. In both cases
            # "off" means "the inverting/enabling action does NOT fire".
            display_type = simplify_type_annotation(annotation) if annotation else 'bool'
            if param_desc:
                param_desc_suffix = f' {param_desc}'
            else:
                param_desc_suffix = ''

            if has_default and default:
                # Default True -> --no-flag to disable. Flag itself defaults
                # to "not passed" (False from the flag's perspective).
                help_text = f'|{display_type}| Default: False |{param_desc_suffix}'
                parser.add_argument(
                    f'--no-{name.replace("_", "-")}',
                    dest=name,
                    action='store_false',
                    default=True,
                    help=help_text,
                )
            else:
                # Default False or no default -> --flag to enable.
                display_default = default if default is not None else False
                help_text = f'|{display_type}| Default: {display_default} |{param_desc_suffix}'
                var_names = build_var_names(name, short_flag, has_default=True)
                parser.add_argument(
                    *var_names,
                    dest=name,
                    action='store_true',
                    default=False if default is None else default,
                    help=help_text,
                )
        elif has_default:
            # Optional argument with default
            var_names = build_var_names(name, short_flag, has_default=True)
            # Build help text with simplified type info
            display_type = simplify_type_annotation(annotation) if annotation else ''
            type_str = f'|{display_type}|' if display_type else ''
            # Simplify enum defaults (e.g., Currency.NULL_CURRENCY -> NULL_CURRENCY)
            display_default = default
            # For lazy-arg defaults, show the original source string (e.g.
            # "yesterday") rather than the sentinel's repr ("<lazy default>").
            if lazy_arg and default is _LAZY_DEFAULT:
                display_default = f'{lazy_arg["cls"]}("{lazy_arg["arg"]}")'
            elif isinstance(default, str) and '.' in default:
                display_default = default.split('.')[-1]
            if isinstance(display_default, str):
                if display_default == '':
                    display_default = '""'
                else:
                    display_default = display_default.replace('%', '%%')
            help_text = f'{type_str} Default: {display_default} |'
            if param_desc:
                help_text = f'{help_text} {param_desc}'
            kwargs = {
                'dest': name,
                'type': param_type,
                'default': default,
                'help': help_text,
            }
            if enum_choices:
                kwargs['choices'] = enum_choices
            if is_multi_value_type(annotation) or isinstance(default, (list, tuple)):
                kwargs['nargs'] = '*'
            parser.add_argument(*var_names, **kwargs)
        else:
            # Required positional argument
            display_type = simplify_type_annotation(annotation) if annotation else ''
            help_text = f'|{display_type}|' if display_type else ''
            if param_desc:
                help_text = f'{help_text} {param_desc}'
            kwargs = {
                'type': param_type,
                'help': help_text,
            }
            if enum_choices:
                kwargs['choices'] = enum_choices
            # For tuple/list types, accept multiple values
            if is_multi_value_type(annotation):
                kwargs['nargs'] = '+'
            parser.add_argument(name, **kwargs)

    # Attach for invoke_function — accessed via parsed_args owner, passed through.
    parser._pydantic_binds = pydantic_binds
    return parser


def find_enum_class(func_module, enum_name):
    """Find an enum class by searching the function's module and common locations."""
    # Try to import the function's module and look for the enum
    try:
        mod = importlib.import_module(func_module)
        # Check module's globals for the enum
        if hasattr(mod, enum_name):
            return getattr(mod, enum_name)
        # Check module's __dict__ for imported names
        for attr_name, attr_val in vars(mod).items():
            if attr_name == enum_name:
                return attr_val
    except ImportError:
        pass

    # Try common protobuf enum locations
    common_locations = [
        'protobuf.enums_pb2',
        'enums_pb2',
    ]
    for loc in common_locations:
        try:
            mod = importlib.import_module(loc)
            if hasattr(mod, enum_name):
                return getattr(mod, enum_name)
        except ImportError:
            pass

    return None


def convert_enum_args(func, kwargs, enums):
    """Convert string enum values to actual enum values."""
    params = {p['name']: p for p in func.get('parameters', [])}
    func_module = func.get('module', '')

    for key, value in list(kwargs.items()):
        param = params.get(key)
        if not param:
            continue

        annotation = param.get('type_annotation', '')
        if not annotation:
            continue

        # Check if this param uses an enum type
        for enum_name in enums.keys():
            if enum_name in annotation:
                enum_cls = find_enum_class(func_module, enum_name)
                if enum_cls is None:
                    continue

                # Strip any `Enum.` prefix coming from a source-parsed default
                # literal (e.g. `color: Color = Color.RED` stores the default
                # as the string "Color.RED", which would otherwise fail
                # getattr and silently leave a str where an enum is expected).
                # User-supplied argparse values are already bare member names,
                # so the strip is a no-op in that case. Guarding isinstance
                # also makes the list branch safe against already-converted
                # enum members.
                def _to_member(v):
                    return v.split('.')[-1] if isinstance(v, str) else v
                try:
                    # Collection-of-enums: argparse hands back a list, but a
                    # source-parsed default literal like `tuple[E, ...] = (E.A, E.B)`
                    # arrives as a tuple already. Accept either and normalize
                    # to the container type that matches the annotation so the
                    # user function receives exactly what its signature says.
                    if isinstance(value, (list, tuple)):
                        ann_lstrip = annotation.lstrip()
                        if ann_lstrip.startswith("tuple") or ann_lstrip.startswith("Tuple"):
                            container = tuple
                        elif ann_lstrip.startswith("frozenset") or ann_lstrip.startswith("FrozenSet"):
                            container = frozenset
                        elif ann_lstrip.startswith("set") or ann_lstrip.startswith("Set"):
                            container = set
                        else:
                            container = list
                        kwargs[key] = container(
                            getattr(enum_cls, _to_member(v)) for v in value
                        )
                    else:
                        kwargs[key] = getattr(enum_cls, _to_member(value))
                except AttributeError:
                    pass  # Keep original value if conversion fails
                break

    return kwargs


def invoke_function(func, parsed_args, enums=None, pydantic_binds=None):
    """Import module and invoke the function."""
    import inspect  # lazy: only the invoke path needs this (~7ms import)
    module_name = func['module']
    func_name = func['name']

    # Import the module
    module = importlib.import_module(module_name)

    # Check if this is a class method (first param is 'self')
    params = func.get('parameters', [])
    is_method = params and params[0].get('name') == 'self'

    if is_method:
        # Find the class containing this method
        fn = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if inspect.isclass(attr) and hasattr(attr, func_name):
                method = getattr(attr, func_name)
                if callable(method):
                    # Instantiate the class and get the bound method
                    instance = attr()
                    fn = getattr(instance, func_name)
                    break
        if fn is None:
            raise AttributeError(f"Could not find class containing method '{func_name}' in {module_name}")
    else:
        # Regular function
        fn = getattr(module, func_name)

    # Global CLI args to exclude from function call
    global_args = {'cli', 'pdb', 'pip', 'uv', 'pyspy', 'raw', 'notraceback', 'timing', 'version', 'llm_help', 'skip_gen'}

    # Convert parsed args to dict, excluding None values and global CLI args
    kwargs = {k: v for k, v in vars(parsed_args).items() if v is not None and k not in global_args}

    # Lazy-arg defaults: substitute `_LAZY_DEFAULT` sentinel with a FRESH
    # call to the Arg class whose source-level default was recognised at
    # scan time. This is what makes `day: date = DateArg("today")` resolve
    # to today-at-dispatch-time rather than today-at-module-import-time.
    # Any parameter whose value is still the sentinel had no CLI arg
    # supplied AND has a recognised lazy default.
    params_by_name = {p['name']: p for p in func.get('parameters', [])}
    for pname, pval in list(kwargs.items()):
        if pval is _LAZY_DEFAULT:
            spec = params_by_name.get(pname, {}).get('lazy_arg')
            if spec:
                from cliche import types as _nc_types
                cls = getattr(_nc_types, spec['cls'], None)
                if cls is not None:
                    kwargs[pname] = cls(spec['arg'])
                    continue
            # Shouldn't happen — scrub the sentinel so we don't leak it.
            kwargs.pop(pname)

    # Pydantic: pop flattened field args and rebuild model instances. Must
    # happen BEFORE enum conversion so enum-typed fields inside a model are
    # only inspected once, in pydantic's validator path.
    if pydantic_binds:
        for param_name, model_cls, field_names in pydantic_binds:
            field_kwargs = {fn: kwargs.pop(fn) for fn in field_names if fn in kwargs}
            try:
                kwargs[param_name] = model_cls(**field_kwargs)
            except Exception as e:
                print(f"error: failed to construct {model_cls.__name__} for --{param_name}: {e}",
                      file=sys.stderr)
                sys.exit(2)

    # Convert enum string values to actual enum values
    if enums:
        kwargs = convert_enum_args(func, kwargs, enums)

    # Non-enum container coercion: argparse always collects nargs='+'/'*' into
    # a list, but the user's function may be annotated with set / frozenset /
    # tuple. Wrap where needed so the received type matches the signature.
    # (The enum path above already handles enum-typed collections.)
    for param in params:
        pname = param['name']
        if pname not in kwargs:
            continue
        ann = (param.get('type_annotation') or '').lstrip()
        value = kwargs[pname]
        if not isinstance(value, list):
            continue
        if ann.startswith('frozenset[') or ann.startswith('FrozenSet['):
            kwargs[pname] = frozenset(value)
        elif ann.startswith('set[') or ann.startswith('Set['):
            kwargs[pname] = set(value)
        elif ann.startswith('tuple[') or ann.startswith('Tuple['):
            kwargs[pname] = tuple(value)

    # Call the function (handle async functions)
    if inspect.iscoroutinefunction(fn):
        import asyncio
        result = asyncio.run(fn(**kwargs))
    else:
        result = fn(**kwargs)

    # A non-None return value is always auto-printed after the function runs.
    # If the function also called print(...), both outputs are shown — the
    # print usually carries diagnostic context (labels, progress), the return
    # carries the data. Users who literally want only one should either
    # `return None` or remove the print().
    if result is not None:
        if RAW_MODE:
            # Raw: no JSON pretty-print, no wrapping. Good for `| jq`, `| wc`.
            print(result)
        else:
            try:
                print(json.dumps(result, indent=2))
            except (TypeError, ValueError):
                print(result)


def _start_pyspy(duration):
    """Spawn py-spy in a background process to profile the current PID for `duration` seconds.

    When py-spy's duration expires, it SIGKILL's the parent process and prints
    a clear summary for LLM consumption.

    Returns the output file path, or None if py-spy is not available.
    """
    import shutil
    import subprocess
    import tempfile

    if not shutil.which('py-spy'):
        print("warning: py-spy not found, skipping profiling (pip install py-spy)", file=sys.stderr)
        return None

    parent_pid = os.getpid()
    out_file = tempfile.mktemp(suffix='.json', prefix='pyspy_')
    print(f"[pyspy] started: PID={parent_pid} duration={duration}s output={out_file}", file=sys.stderr)

    # Wrapper script: run py-spy, print result, then SIGKILL the parent
    import textwrap
    wrapper = textwrap.dedent(f"""\
        import subprocess, sys, os, signal, time
        subprocess.run(
            ['py-spy', 'record', '-o', '{out_file}', '--pid', '{parent_pid}',
             '--duration', '{duration}', '--native', '--format', 'speedscope'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Print result BEFORE killing the parent
        if os.path.exists('{out_file}'):
            size = os.path.getsize('{out_file}')
            print(f"[pyspy] done: output={out_file} size={{size}} bytes", file=sys.stderr)
            print(f"[pyspy] view: https://www.speedscope.app/ (load {out_file})", file=sys.stderr)
        else:
            print("[pyspy] error: no output file produced", file=sys.stderr)
        sys.stderr.flush()
        time.sleep(0.1)
        # SIGKILL the parent — SIGTERM may be caught/ignored by long-running commands
        try:
            os.kill({parent_pid}, signal.SIGKILL)
        except ProcessLookupError:
            pass
    """)

    proc = subprocess.Popen(
        [sys.executable, '-c', wrapper],
        stdout=subprocess.DEVNULL, stderr=None,  # inherit stderr so prints are visible
    )

    # Register cleanup so py-spy is terminated if the main process exits before duration
    import atexit
    def _cleanup():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if os.path.exists(out_file):
            size = os.path.getsize(out_file)
            print(f"[pyspy] done: output={out_file} size={size} bytes", file=sys.stderr)
            print(f"[pyspy] view: https://www.speedscope.app/ (load {out_file})", file=sys.stderr)
    atexit.register(_cleanup)

    return out_file


def main():
    global CACHE_PATH, SOURCE_DIR
    import time
    t0 = time.time()

    # Get program name for usage messages
    prog_name = os.path.basename(sys.argv[0])

    # Handle --version early: print just the user package's version (or the
    # cliche version as a last resort) and exit. Intentionally terse —
    # designed for `mytool --version | cut ...` and VERSION-file style use.
    if '--version' in sys.argv:
        pkg_ver = _resolve_pkg_version(PKG_NAME, INSTALL_DIR)
        print(pkg_ver or _cliche_version())
        sys.exit(0)

    # Handle --cli early (before loading cache)
    if '--cli' in sys.argv:
        cli_info(CACHE_PATH, pkg_name=PKG_NAME, install_dir=INSTALL_DIR)
        sys.exit(0)

    # Handle --pip early: launch pip belonging to current Python executable
    if '--pip' in sys.argv:
        import subprocess
        python_dir = os.path.dirname(sys.executable)
        pip_path = os.path.join(python_dir, 'pip')
        # Pass everything after --pip as pip arguments
        pip_args = sys.argv[sys.argv.index('--pip') + 1:]
        if os.path.exists(pip_path):
            sys.exit(subprocess.call([pip_path] + pip_args))
        else:
            sys.exit(subprocess.call([sys.executable, '-m', 'pip'] + pip_args))

    # Handle --uv early: launch uv pointed at this CLI's Python environment.
    # Forwards `VIRTUAL_ENV` / `UV_PYTHON` so `uv pip ...` / `uv add ...` /
    # `uv sync` act on the env the CLI is actually running in, not whatever
    # uv would otherwise discover. Falls back to `python -m uv` if the uv
    # binary isn't on PATH.
    if '--uv' in sys.argv:
        import subprocess, shutil
        uv_args = sys.argv[sys.argv.index('--uv') + 1:]
        env = os.environ.copy()
        env.setdefault("UV_PYTHON", sys.executable)
        # If the CLI is running inside a venv, signal it to uv explicitly so
        # `uv pip install ...` lands there instead of the system Python.
        venv_root = os.path.dirname(os.path.dirname(sys.executable))
        if os.path.exists(os.path.join(venv_root, "pyvenv.cfg")):
            env.setdefault("VIRTUAL_ENV", venv_root)
        uv_path = shutil.which("uv")
        if uv_path:
            sys.exit(subprocess.call([uv_path] + uv_args, env=env))
        try:
            sys.exit(subprocess.call([sys.executable, '-m', 'uv'] + uv_args, env=env))
        except FileNotFoundError:
            print("error: uv not found on PATH and not importable as a module.\n"
                  "       install with: pip install uv   (or see https://docs.astral.sh/uv/)",
                  file=sys.stderr)
            sys.exit(127)

    # Accept underscore-style long flags (e.g. --exclude_exchanges) as aliases
    # for the canonical kebab-case (--exclude-exchanges). Rewrite the flag
    # portion only — values after `=` are left untouched.
    for _i, _a in enumerate(sys.argv):
        if _a.startswith('--') and '_' in _a:
            _flag, _sep, _val = _a.partition('=')
            sys.argv[_i] = _flag.replace('_', '-') + _sep + _val

    show_timing = '--timing' in sys.argv
    if show_timing:
        sys.argv.remove('--timing')

    # --raw: disable color and switch result printing to plain `print()`.
    # Consumed by Colors via RAW_MODE and by invoke_function's result printer.
    global RAW_MODE
    if '--raw' in sys.argv:
        sys.argv.remove('--raw')
        RAW_MODE = True

    # --notraceback: suppress the Python traceback on uncaught exceptions. Just
    # print `ExcName: message` and exit 1. Useful for end-user CLIs where the
    # traceback is noise. Plays well with --pdb (pdb sets its own excepthook
    # later, which overrides this — that's the right precedence).
    if '--notraceback' in sys.argv:
        sys.argv.remove('--notraceback')
        def _terse_excepthook(exc_type, exc_value, tb):
            msg = f"{exc_type.__name__}: {exc_value}" if str(exc_value) else exc_type.__name__
            print(msg, file=sys.stderr)
            sys.exit(1)
        sys.excepthook = _terse_excepthook

    # Set up pdb excepthook if --pdb is passed (must be before any imports that might fail).
    # Prefers ipdb (richer REPL) and falls back to stdlib pdb. Hint the user once
    # on fallback so they know the `cliche[debug]` extra would upgrade them.
    if '--pdb' in sys.argv:
        sys.argv.remove('--pdb')
        def _pdb_excepthook(type, value, tb):
            if hasattr(sys, 'ps1') or not sys.stderr.isatty():
                sys.__excepthook__(type, value, tb)
            else:
                import traceback
                traceback.print_exception(type, value, tb)
                try:
                    import ipdb as pdb
                except ImportError:
                    import pdb
                    print("(using stdlib pdb; `pip install ipdb` for a nicer REPL)",
                          file=sys.stderr)
                pdb.post_mortem(tb)
        sys.excepthook = _pdb_excepthook

    # Check for --pyspy flag (spawn py-spy profiler in background)
    pyspy_duration = 0
    if '--pyspy' in sys.argv:
        idx = sys.argv.index('--pyspy')
        # Next arg is duration in seconds
        if idx + 1 < len(sys.argv) and sys.argv[idx + 1].isdigit():
            pyspy_duration = int(sys.argv[idx + 1])
            del sys.argv[idx:idx + 2]
        else:
            del sys.argv[idx]
    if pyspy_duration > 0:
        _start_pyspy(pyspy_duration)

    # Check for --skip-gen flag (skip cache regeneration)
    skip_gen = '--skip-gen' in sys.argv
    if skip_gen:
        sys.argv.remove('--skip-gen')

    # Check for --llm-help flag
    show_llm = '--llm-help' in sys.argv
    if show_llm:
        sys.argv.remove('--llm-help')
        CleanArgumentParser.llm_mode = True

    # Regenerate cache if SOURCE_DIR is set and not skipping
    if SOURCE_DIR and not skip_gen:
        try:
            from cliche.main import scan_directory
        except ImportError:
            from main import scan_directory
        t_gen = time.time()
        scan_directory(Path(SOURCE_DIR), Path(CACHE_PATH))
        if show_timing:
            print(f"timing cache regen: {(time.time() - t_gen)*1000:.1f}ms", file=sys.stderr)

    # Load cache (uses PRELOADED_CACHE if set by install_generator)
    data = load_cache()
    if show_timing:
        cached = " (preloaded)" if PRELOADED_CACHE else ""
        print(f"timing cache_load{cached}: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    t1 = time.time()
    commands, subcommands, enums, pydantic_models = build_index(data)
    if show_timing:
        print(f"timing build_index: {(time.time() - t1)*1000:.1f}ms", file=sys.stderr)

    # Handle argcomplete - build minimal parser for completion
    if '_ARGCOMPLETE' in os.environ:
        import argcomplete

        def add_params_to_parser(cmd_parser, func):
            """Add function parameters to argparse parser for completion."""
            params = func.get('parameters', [])
            short_flags = get_short_flags(params)
            used_short = {'-h'}  # Reserved by argparse for help

            for param in params:
                pname = param['name']
                if pname in ('self', 'cls') or param.get('is_args') or param.get('is_kwargs'):
                    continue
                annotation = param.get('type_annotation')
                default_str = param.get('default')
                has_default = default_str is not None
                param_type = type_from_annotation(annotation)
                short_flag = short_flags.get(pname)
                # Skip short flag if already used
                if short_flag and short_flag in used_short:
                    short_flag = None
                if short_flag:
                    used_short.add(short_flag)
                enum_choices = get_enum_from_annotation(annotation, enums)

                if param_type == bool:
                    if has_default and parse_default(default_str, bool):
                        cmd_parser.add_argument(f'--no-{pname.replace("_", "-")}', dest=pname, action='store_false')
                    else:
                        var_names = build_var_names(pname, short_flag, has_default=True)
                        cmd_parser.add_argument(*var_names, dest=pname, action='store_true')
                elif has_default:
                    var_names = build_var_names(pname, short_flag, has_default=True)
                    kwargs = {'dest': pname}
                    if enum_choices:
                        kwargs['choices'] = enum_choices
                    cmd_parser.add_argument(*var_names, **kwargs)
                else:
                    # Positional argument - use completer to prevent file fallback
                    kwargs = {}
                    if enum_choices:
                        kwargs['choices'] = enum_choices
                        kwargs['metavar'] = pname.upper()
                    else:
                        # No choices = suppress file completion with empty completer
                        kwargs['metavar'] = pname.upper()
                    arg = cmd_parser.add_argument(pname, **kwargs)
                    # Explicitly set completer to prevent file fallback
                    if enum_choices:
                        arg.completer = argcomplete.completers.ChoicesCompleter(enum_choices)
                    else:
                        # Suppress file completion for non-enum positionals
                        arg.completer = lambda **kw: []

        # Parse COMP_LINE to see what we're completing
        comp_line = os.environ.get('COMP_LINE', '')
        comp_words = comp_line.split()

        parser = CleanArgumentParser()
        subparsers = parser.add_subparsers(dest='command')

        # Determine which command is being completed
        target_cmd = comp_words[1] if len(comp_words) > 1 else None
        target_subcmd = comp_words[2] if len(comp_words) > 2 else None

        # Only build the parser for the command being completed
        if target_cmd and target_cmd in commands:
            # Direct command - only build this one
            cmd_parser = subparsers.add_parser(target_cmd)
            add_params_to_parser(cmd_parser, commands[target_cmd])
        elif target_cmd and target_cmd in subcommands:
            # Subcommand group
            group_parser = subparsers.add_parser(target_cmd)
            group_subparsers = group_parser.add_subparsers(dest='subcommand')
            if target_subcmd and target_subcmd in subcommands[target_cmd]:
                # Specific subcommand - only build this one
                cmd_parser = group_subparsers.add_parser(target_subcmd)
                add_params_to_parser(cmd_parser, subcommands[target_cmd][target_subcmd])
            else:
                # Completing subcommand name - add all subcommands (names only)
                for name in subcommands[target_cmd]:
                    group_subparsers.add_parser(name)
        else:
            # Completing command name - add all command names (no params needed)
            for name in commands:
                subparsers.add_parser(name)
            for group in subcommands:
                subparsers.add_parser(group)

        argcomplete.autocomplete(parser)

    # Handle --llm-help help output (only when no args to execute)
    if show_llm and (len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help')):
        print_llm_output(commands, subcommands, enums, prog_name=prog_name,
                         install_dir=INSTALL_DIR, cwd=os.getcwd(),
                         cache_path=CACHE_PATH, pkg_name=PKG_NAME)
        return

    # Parse command from argv
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print_help(commands, subcommands, prog_name=prog_name)
        if show_timing:
            print(f"timing total (help): {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)
        return

    cmd = sys.argv[1].replace('_', '-')

    # Handle --llm-help on a specific command/group (with or without -h). Detailed per-param output.
    if show_llm:
        if cmd in commands:
            print_llm_command_help(commands[cmd], prog_name, cmd)
        elif cmd in subcommands:
            # If a specific subcommand is named, show its detailed help; else list the group.
            if len(sys.argv) >= 3 and sys.argv[2] not in ('-h', '--help'):
                subcmd = sys.argv[2].replace('_', '-')
                if subcmd in subcommands[cmd]:
                    print_llm_command_help(subcommands[cmd][subcmd], prog_name, subcmd, group=cmd)
                else:
                    print(f"error: unknown subcommand '{cmd} {subcmd}'", file=sys.stderr)
                    sys.exit(1)
            else:
                print_llm_output({}, {cmd: subcommands[cmd]}, enums, prog_name=prog_name,
                                 install_dir=INSTALL_DIR, cwd=os.getcwd())
        else:
            print(f"error: unknown command '{cmd}'", file=sys.stderr)
            sys.exit(1)
        return

    # Check if it's a direct command
    if cmd in commands:
        func = commands[cmd]
        sys.argv = sys.argv[1:]  # Shift argv for subparser

        t2 = time.time()
        help_only = any(a in ('-h', '--help') for a in sys.argv[1:])
        parser = build_parser_for_function(func, enums, prog_name=prog_name, help_only=help_only, pydantic_models=pydantic_models)
        if show_timing:
            print(f"timing build_parser: {(time.time() - t2)*1000:.1f}ms", file=sys.stderr)

        parsed_args = parser.parse_args()

        if show_timing:
            print(f"timing before import: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

        t3 = time.time()
        invoke_function(func, parsed_args, enums,
                        pydantic_binds=getattr(parser, '_pydantic_binds', None))
        if show_timing:
            print(f"timing import+invoke: {(time.time() - t3)*1000:.1f}ms", file=sys.stderr)
            print(f"timing total: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)
        return

    # Check if it's a subcommand group
    if cmd in subcommands:
        if len(sys.argv) < 3 or sys.argv[2] in ('-h', '--help'):
            print(f"{Colors.blue(f'usage: {prog_name} {cmd} COMMAND ...')}\n")
            print(f"Commands in '{cmd}':")
            for name in sorted(subcommands[cmd].keys()):
                doc = get_docstring_first_line(subcommands[cmd][name])
                padded_name = f"    {name:24}"
                if doc:
                    print(f"{Colors.blue(padded_name)}{doc[:50]}")
                else:
                    print(Colors.blue(padded_name.rstrip()))
            return

        subcmd = sys.argv[2].replace('_', '-')
        if subcmd in subcommands[cmd]:
            func = subcommands[cmd][subcmd]
            sys.argv = sys.argv[2:]  # Shift argv

            t2 = time.time()
            help_only = any(a in ('-h', '--help') for a in sys.argv[1:])
            parser = build_parser_for_function(func, enums, prog_name=prog_name, help_only=help_only, pydantic_models=pydantic_models)
            if show_timing:
                print(f"timing build parser: {(time.time() - t2)*1000:.1f}ms", file=sys.stderr)

            parsed_args = parser.parse_args()

            if show_timing:
                print(f"timing before import: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

            t3 = time.time()
            invoke_function(func, parsed_args, enums)
            if show_timing:
                print(f"timing import+invoke: {(time.time() - t3)*1000:.1f}ms", file=sys.stderr)
                print(f"timing total: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)
            return
        else:
            print(f"Unknown command: {cmd} {subcmd}", file=sys.stderr)
            sys.exit(1)

    # When the binary name equals the only ungrouped command name (e.g.
    # `csv_stats` binary with a `csv_stats` function), the user will run
    # `csv_stats <pos1> <pos2>` — but `sys.argv[1]` is <pos1>, not the
    # command name.  Detect that single-command case and dispatch directly.
    if len(commands) == 1 and not subcommands:
        single_name, single_func = next(iter(commands.items()))
        if single_name == prog_name.replace('_', '-'):
            func = single_func
            # sys.argv[1:] is the correct arg list for the function.
            # Pass it directly to parse_args() instead of shifting sys.argv
            # (parse_args reads sys.argv[1:] by default, which would eat
            # the first positional arg if we shifted).
            func_argv = sys.argv[1:]

            t2 = time.time()
            help_only = any(a in ('-h', '--help') for a in func_argv)
            parser = build_parser_for_function(func, enums, prog_name=prog_name, help_only=help_only, pydantic_models=pydantic_models)
            if show_timing:
                print(f"timing build_parser: {(time.time() - t2)*1000:.1f}ms", file=sys.stderr)

            parsed_args = parser.parse_args(func_argv)

            if show_timing:
                print(f"timing before import: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

            t3 = time.time()
            invoke_function(func, parsed_args, enums,
                            pydantic_binds=getattr(parser, '_pydantic_binds', None))
            if show_timing:
                print(f"timing import+invoke: {(time.time() - t3)*1000:.1f}ms", file=sys.stderr)
                print(f"timing total: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)
            return

    print(f"Unknown command: {cmd}", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()

