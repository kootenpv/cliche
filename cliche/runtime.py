#!/usr/bin/env python3
"""
Runtime for pip-installed packages using @cli decorators.

This module is called from the generated _cliche.py entry point in user packages.
It handles dynamic package discovery, scanning, caching, and CLI execution.
"""
import contextlib
import hashlib
import importlib
import json
import os
import re
import sys
import time
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", "venv", "node_modules", ".venv", "env", ".env"}
_PARALLEL_THRESHOLD = 4
_RE_CLI = None  # Lazy compiled regex


def _process_age_ms():
    """Wall-clock ms since this process was spawned, or None if not determinable."""
    if sys.platform.startswith("linux"):
        try:
            with open('/proc/uptime') as f:
                uptime_s = float(f.read().split()[0])
            with open('/proc/self/stat') as f:
                starttime_ticks = int(f.read().split(')')[-1].split()[19])
            clock_ticks = os.sysconf('SC_CLK_TCK')
            return (uptime_s - starttime_ticks / clock_ticks) * 1000
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util
            lib = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib")

            class _ProcBsdInfo(ctypes.Structure):
                _fields_ = [
                    ("pbi_flags", ctypes.c_uint32),
                    ("pbi_status", ctypes.c_uint32),
                    ("pbi_xstatus", ctypes.c_uint32),
                    ("pbi_pid", ctypes.c_uint32),
                    ("pbi_ppid", ctypes.c_uint32),
                    ("pbi_uid", ctypes.c_uint32),
                    ("pbi_gid", ctypes.c_uint32),
                    ("pbi_ruid", ctypes.c_uint32),
                    ("pbi_rgid", ctypes.c_uint32),
                    ("pbi_svuid", ctypes.c_uint32),
                    ("pbi_svgid", ctypes.c_uint32),
                    ("rfu_1", ctypes.c_uint32),
                    ("pbi_comm", ctypes.c_char * 16),
                    ("pbi_name", ctypes.c_char * 32),
                    ("pbi_nfiles", ctypes.c_uint32),
                    ("pbi_pgid", ctypes.c_uint32),
                    ("pbi_pjobc", ctypes.c_uint32),
                    ("e_tdev", ctypes.c_uint32),
                    ("e_tpgid", ctypes.c_uint32),
                    ("pbi_nice", ctypes.c_int32),
                    ("pbi_start_tvsec", ctypes.c_uint64),
                    ("pbi_start_tvusec", ctypes.c_uint64),
                ]

            info = _ProcBsdInfo()
            PROC_PIDTBSDINFO = 3
            ret = lib.proc_pidinfo(os.getpid(), PROC_PIDTBSDINFO, 0,
                                   ctypes.byref(info), ctypes.sizeof(info))
            if ret <= 0:
                return None
            start = info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6
            return (time.time() - start) * 1000
        except Exception:
            return None
    return None


def _get_cache_dir() -> Path:
    """Get cache directory for cliche."""
    # Use XDG_CACHE_HOME if set, otherwise ~/.cache
    cache_home = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    cache_dir = Path(cache_home) / "cliche"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _get_cache_path(package_name: str, pkg_dir: Path) -> Path:
    """Get cache file path for a package, incorporating the path to handle multiple installs."""
    # Include hash of pkg_dir to differentiate editable vs installed, or multiple editable installs
    dir_hash = hashlib.md5(str(pkg_dir).encode()).hexdigest()[:8]
    return _get_cache_dir() / f"{package_name}_{dir_hash}.json"


_RE_ENUM_CLASS = None


def _parse_cli_funcs(full_path: str, rel_path: str, package_name: str = ""):
    """Parse a single file for @cli functions OR enum definitions (fast regex scan).

    We keep files with enum definitions even when they contain no @cli
    functions (e.g. a shared `commons/enums.py` module) so their enums make
    it into the global enum cache. Without this, any @cli signature
    referencing those enums would fall through unconverted — the CLI
    string arrives at the user function instead of a real enum member,
    failing on `.name` / `.value` access.
    """
    global _RE_CLI, _RE_ENUM_CLASS
    if _RE_CLI is None:
        _RE_CLI = re.compile(r'^ *@cli(?:\([\'"]([a-zA-Z0-9_]+)[\'"]\))? *\n *(?:async )?def ([^( ]+)', re.M)
    if _RE_ENUM_CLASS is None:
        # Match `class Foo(Enum):`, `class Foo(IntEnum):`, etc. — any
        # stdlib enum base class that extract_python_enums also recognises.
        _RE_ENUM_CLASS = re.compile(
            r'^ *class +[A-Za-z_][A-Za-z0-9_]*\s*\('
            r'[^)]*\b(?:Enum|IntEnum|StrEnum|Flag|IntFlag|ReprEnum)\b',
            re.M,
        )

    try:
        with open(full_path) as f:
            contents = f.read()
    except OSError:
        return None

    funcs = _RE_CLI.findall(contents)
    has_enum_defs = bool(_RE_ENUM_CLASS.search(contents))
    mod_name = rel_path.replace("/", ".").replace(".py", "").replace(".__init__", "")
    # Prepend package name if provided
    if package_name and mod_name:
        mod_name = f"{package_name}.{mod_name}"
    elif package_name:
        mod_name = package_name
    return {
        "mtime": os.stat(full_path).st_mtime,
        "functions": [
            {"name": fn, "module": mod_name, "file_path": full_path, "parameters": [], **({"group": g} if g else {})}
            for g, fn in funcs
        ],
        "has_enum_defs": has_enum_defs,
    }


def _get_all_py_files(directory: Path) -> dict[str, float]:
    """Get all .py files respecting skip dirs."""
    py_files, _ = _walk_tree(directory)
    return py_files


def _walk_tree(directory: Path) -> tuple[dict[str, float], dict[str, float]]:
    """Walk directory once; return (py_files {rel: mtime}, dirs {rel: mtime}).

    Tracking dir mtimes lets us detect file additions/deletions/renames without
    re-walking on steady-state runs: any add/remove bumps the containing dir's
    mtime. Content changes do NOT bump dir mtime — detect those via file mtime.
    """
    py_files = {}
    dir_mtimes = {}
    directory_str = str(directory)
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(root, directory_str)
        with contextlib.suppress(OSError):
            dir_mtimes[rel_dir] = os.stat(root).st_mtime
        for fname in files:
            if fname.endswith(".py"):
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, directory_str)
                with contextlib.suppress(OSError):
                    py_files[rel_path] = os.stat(full_path).st_mtime
    return py_files, dir_mtimes


def _dirs_unchanged(pkg_dir: Path, old_dir_mtimes: dict[str, float]) -> bool:
    """Return True iff every cached dir still exists with the same mtime.

    Fast path: if no directory's mtime has changed, no files have been added,
    removed, or renamed anywhere. We can skip the full os.walk and just stat
    tracked .py files to detect content changes.
    """
    if not old_dir_mtimes:
        return False
    for rel_dir, old_mtime in old_dir_mtimes.items():
        full = pkg_dir if rel_dir == "." else pkg_dir / rel_dir
        try:
            if os.stat(full).st_mtime != old_mtime:
                return False
        except OSError:
            return False
    return True


def _ast_parse_file(args):
    """Parse a single file with full AST (for multiprocessing).
    Returns (rel_path, functions, local_enums) or None.
    """
    rel_path, full_path, base_dir, package_name = args
    try:
        from pathlib import Path

        from cliche.main import extract_cli_functions, extract_python_enums

        content = open(full_path).read()
        functions, tree = extract_cli_functions(content, Path(full_path), Path(base_dir), return_tree=True)

        # Prepend package name to module paths (only if we have any functions).
        if package_name and functions:
            for func in functions:
                mod = func.get("module", "")
                if mod:
                    func["module"] = f"{package_name}.{mod}"
                else:
                    func["module"] = package_name

        # Always extract enums — a file may be enum-only (shared `enums.py`
        # module) with no @cli functions, but still contribute to the global
        # enum cache consumed by @cli signatures in other files.
        local_enums = extract_python_enums(content, tree=tree)
        return (rel_path, functions or [], local_enums)
    except Exception:
        return None


def _scan_and_cache(pkg_dir: Path, cache_file: Path, package_name: str = "", show_timing: bool = False) -> dict:
    """Scan package directory for @cli functions and update cache."""
    t0 = time.time()

    # Load cache
    try:
        with open(cache_file) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {"version": "2.1", "files": {}, "enums": {}, "py_mtimes": {}}

    if show_timing:
        print(f"cache_load: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    old_files = cache.get("files", {})
    old_py_mtimes = cache.get("py_mtimes", {})
    old_dir_mtimes = cache.get("dir_mtimes", {})

    # Phase 1: Quick check - stat only files that HAD @cli decorators
    changed_files = []
    deleted_files = []

    for rel_path, file_info in old_files.items():
        full_path = os.path.join(pkg_dir, rel_path)
        try:
            current_mtime = os.stat(full_path).st_mtime
            if current_mtime != file_info.get("mtime"):
                changed_files.append(rel_path)
        except OSError:
            deleted_files.append(rel_path)

    if show_timing:
        print(
            f"check_known: {(time.time() - t0)*1000:.1f}ms ({len(old_files)} files, {len(changed_files)} changed)",
            file=sys.stderr,
        )

    # Phase 2: Discover file adds/deletes. Fast path uses dir mtimes: any add,
    # delete, or rename bumps the containing dir's mtime, so if every cached
    # dir's mtime is unchanged, the file set is unchanged — skip os.walk and
    # just stat tracked files for content changes.
    new_py_files = []
    if _dirs_unchanged(pkg_dir, old_dir_mtimes):
        current_py_files = {}
        for rel_path, old_mtime in old_py_mtimes.items():
            full_path = os.path.join(pkg_dir, rel_path)
            try:
                current_mtime = os.stat(full_path).st_mtime
                current_py_files[rel_path] = current_mtime
                if current_mtime != old_mtime and rel_path not in changed_files:
                    changed_files.append(rel_path)
            except OSError:
                if rel_path not in deleted_files:
                    deleted_files.append(rel_path)
        current_dir_mtimes = old_dir_mtimes
        fast_path = True
    else:
        current_py_files, current_dir_mtimes = _walk_tree(pkg_dir)
        for rel_path, current_mtime in current_py_files.items():
            old_mtime = old_py_mtimes.get(rel_path)
            if old_mtime is None:
                new_py_files.append(rel_path)
            elif current_mtime != old_mtime and rel_path not in changed_files:
                changed_files.append(rel_path)
        for rel_path in old_py_mtimes:
            if rel_path not in current_py_files and rel_path not in deleted_files:
                deleted_files.append(rel_path)
        fast_path = False

    if show_timing:
        mode = "fast" if fast_path else "walk"
        print(
            f"check_new: {(time.time() - t0)*1000:.1f}ms ({mode}; {len(new_py_files)} new, {len(changed_files)} changed)",
            file=sys.stderr,
        )

    # Phase 3: Incremental update
    needs_full_ast = False
    new_files = dict(old_files)

    for rel_path in deleted_files:
        new_files.pop(rel_path, None)

    for rel_path in changed_files:
        full_path = os.path.join(pkg_dir, rel_path)
        result = _parse_cli_funcs(full_path, rel_path, package_name)
        if result:
            # Keep the file in new_files if it contributes @cli functions
            # OR defines enums referenced elsewhere. Either requires a full
            # AST parse in Phase 4 to populate the global enum cache.
            if result["functions"] or result.get("has_enum_defs"):
                new_files[rel_path] = result
                needs_full_ast = True
            elif rel_path in new_files:
                del new_files[rel_path]

    for rel_path in new_py_files:
        full_path = os.path.join(pkg_dir, rel_path)
        result = _parse_cli_funcs(full_path, rel_path, package_name)
        if result and (result["functions"] or result.get("has_enum_defs")):
            new_files[rel_path] = result
            needs_full_ast = True

    if show_timing:
        print(f"incremental: {(time.time() - t0)*1000:.1f}ms (ast_needed={needs_full_ast})", file=sys.stderr)

    # Phase 4: Full AST parse only for files with @cli that changed
    all_local_enums = {}

    if needs_full_ast:
        to_parse = []
        for rel_path in list(changed_files) + list(new_py_files):
            if rel_path in new_files:
                full_path = os.path.join(pkg_dir, rel_path)
                to_parse.append((rel_path, full_path, str(pkg_dir), package_name))

        if to_parse:
            if len(to_parse) > _PARALLEL_THRESHOLD:
                from multiprocessing import Pool, cpu_count

                with Pool(min(cpu_count(), len(to_parse))) as pool:
                    results = pool.map(_ast_parse_file, to_parse)
                for result in results:
                    if result:
                        rel_path, functions, local_enums = result
                        new_files[rel_path]["functions"] = functions
                        all_local_enums.update(local_enums)
            else:
                for args in to_parse:
                    result = _ast_parse_file(args)
                    if result:
                        rel_path, functions, local_enums = result
                        new_files[rel_path]["functions"] = functions
                        all_local_enums.update(local_enums)

    if show_timing:
        print(f"ast_parse: {(time.time() - t0)*1000:.1f}ms ({len(all_local_enums)} local enums)", file=sys.stderr)

    # Phase 5: Extract enums
    old_proto_enums = cache.get("proto_enums", {})
    old_py_enums = cache.get("py_enums", {})

    _ENUM_NAME_RE = re.compile(r'\b([A-Z][a-zA-Z0-9_]+)')
    needed_enum_names = set()
    for finfo in new_files.values():
        for func in finfo.get("functions", []):
            for param in func.get("parameters", []):
                annotation = param.get("type_annotation", "")
                if annotation:
                    for match in _ENUM_NAME_RE.findall(annotation):
                        if match not in ("Optional", "List", "Tuple", "Dict", "Set", "Union", "None", "True", "False"):
                            needed_enum_names.add(match)

    files_to_check = set(changed_files) | set(new_py_files) | set(deleted_files)
    pb2_changed = any(f.endswith("_pb2.py") for f in files_to_check)
    if not old_proto_enums or pb2_changed:
        try:
            from cliche.proto_enums import parse_pb2_enums
        except ImportError:
            from proto_enums import parse_pb2_enums

        proto_enums = {}
        for rel_path in current_py_files:
            if rel_path.endswith("_pb2.py"):
                full_path = os.path.join(pkg_dir, rel_path)
                enums = parse_pb2_enums(Path(full_path))
                proto_enums.update(enums)
        cache["proto_enums"] = proto_enums
    else:
        proto_enums = old_proto_enums

    proto_enums_filtered = {k: v for k, v in proto_enums.items() if k in needed_enum_names}

    py_enums = dict(old_py_enums)
    py_enums.update(all_local_enums)
    cache["py_enums"] = py_enums

    py_enums_filtered = {k: v for k, v in py_enums.items() if k in needed_enum_names}

    cache["enums"] = {**proto_enums_filtered, **py_enums_filtered}

    if show_timing:
        print(f"enum_extract: {(time.time() - t0)*1000:.1f}ms ({len(cache['enums'])} enums)", file=sys.stderr)

    # Update cache
    cache["files"] = new_files
    cache["py_mtimes"] = current_py_files
    cache["dir_mtimes"] = current_dir_mtimes
    cache.pop("py_enum_cache", None)
    cache["last_scan"] = time.time()

    # Write cache if anything changed (including first run that just populated
    # dir_mtimes — without persisting, the fast path would never kick in).
    # Atomic: write to a sibling temp file and os.replace() it onto the target
    # so a kill or power-loss mid-write can't leave a truncated JSON behind.
    dirs_newly_tracked = not old_dir_mtimes and bool(current_dir_mtimes)
    if changed_files or deleted_files or new_py_files or pb2_changed or dirs_newly_tracked:
        cache_file_path = Path(cache_file)
        tmp_path = cache_file_path.with_suffix(cache_file_path.suffix + f".tmp.{os.getpid()}")
        try:
            with open(tmp_path, "w") as f:
                json.dump(cache, f)
            os.replace(tmp_path, cache_file_path)
        except Exception:
            # Best-effort cleanup; if we can't write the cache, the next run
            # just rebuilds. Don't let cache failures break CLI invocation.
            try:
                tmp_path.unlink()
            except OSError:
                pass

    if show_timing:
        print(f"cache_write: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    return cache


def run_package_cli(package_name: str, _entry_ts: float = None):
    """
    Entry point for pip-installed packages using @cli.

    Called from the generated _cliche.py in user packages.

    Args:
        package_name: The name of the package to scan for @cli functions
        _entry_ts: Timestamp from the entry point script (before any imports)
    """
    t0 = time.time()
    show_timing = "--timing" in sys.argv

    if show_timing:
        proc_age_ms = _process_age_ms()
        if proc_age_ms is not None and _entry_ts is not None:
            import_ms = (t0 - _entry_ts) * 1000
            interp_ms = proc_age_ms - import_ms
            print(f"python_startup: {proc_age_ms:.1f}ms (interpreter: {interp_ms:.1f}ms, imports: {import_ms:.1f}ms)", file=sys.stderr)
        elif proc_age_ms is not None:
            print(f"python_startup: {proc_age_ms:.1f}ms", file=sys.stderr)
        elif _entry_ts is not None:
            print(f"import_overhead: {(t0 - _entry_ts)*1000:.1f}ms", file=sys.stderr)

    # Discover package location
    try:
        pkg = importlib.import_module(package_name)
    except ImportError as e:
        print(f"Error: Could not import package '{package_name}': {e}", file=sys.stderr)
        sys.exit(1)

    pkg_dir = Path(pkg.__file__).parent

    # Add package parent to sys.path so `import <pkg>` resolves
    pkg_parent = pkg_dir.parent
    if str(pkg_parent) not in sys.path:
        sys.path.insert(0, str(pkg_parent))
    # Also add pkg_dir itself so flat-layout packages can import sibling modules
    # by their top-level name (e.g. `from build_index import ...` where build_index.py
    # lives next to cli.py inside the package directory).
    if str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))

    cache_file = _get_cache_path(package_name, pkg_dir)

    if show_timing:
        print(f"discover: {(time.time() - t0)*1000:.1f}ms (pkg_dir={pkg_dir})", file=sys.stderr)

    # Scan and cache
    cache = _scan_and_cache(pkg_dir, cache_file, package_name, show_timing)

    if show_timing:
        print(f"scan_total: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    # Build command index for lazy imports
    func_to_mod = {}
    commands = {}
    subcommands = {}

    for finfo in cache.get("files", {}).values():
        for func in finfo.get("functions", []):
            name, group, mod = func["name"], func.get("group"), func["module"]
            key = (group or "", name)
            func_to_mod[key] = mod
            if group:
                subcommands.setdefault(group, {})[name] = key
            else:
                commands[name] = key

    if show_timing:
        print(f"index_build: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    # Lazy import - only import the module for the command being run
    if len(sys.argv) > 1 and "--help" not in sys.argv and "-h" not in sys.argv:
        one = sys.argv[1].replace("-", "_")
        two = sys.argv[2].replace("-", "_") if len(sys.argv) > 2 else ""
        key = subcommands.get(one, {}).get(two) or commands.get(one)
        if key and key in func_to_mod:
            __import__(func_to_mod[key])

    if show_timing:
        print(f"total_before_run: {(time.time() - t0)*1000:.1f}ms", file=sys.stderr)

    # Run the CLI
    import cliche.run as runner

    runner.CACHE_PATH = cache_file
    runner.SOURCE_DIR = None
    runner.PRELOADED_CACHE = cache
    runner.INSTALL_DIR = str(pkg_dir)
    runner.PKG_NAME = package_name
    runner.main()
