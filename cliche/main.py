#!/usr/bin/env python3
"""
Fast @cli function extractor using AST parsing with mtime-based caching.
"""
import ast
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from proto_enums import parse_pb2_enums
except ImportError:
    def parse_pb2_enums(path):
        return {}


def get_mtime(path: Path) -> float:
    """Get file modification time (stat only, no file read)."""
    return path.stat().st_mtime


def expr_to_string(node: ast.expr) -> str:
    """Convert AST expression to string representation."""
    if node is None:
        return None

    if isinstance(node, ast.Constant):
        if node.value is None:
            return "None"
        elif isinstance(node.value, bool):
            return str(node.value).lower()
        elif isinstance(node.value, str):
            return f'"{node.value}"'
        elif isinstance(node.value, bytes):
            return f"b{node.value!r}"
        elif node.value is ...:
            return "..."
        else:
            return str(node.value)

    elif isinstance(node, ast.Name):
        return node.id

    elif isinstance(node, ast.Attribute):
        return f"{expr_to_string(node.value)}.{node.attr}"

    elif isinstance(node, ast.Subscript):
        return f"{expr_to_string(node.value)}[{expr_to_string(node.slice)}]"

    elif isinstance(node, ast.Tuple):
        parts = [expr_to_string(e) for e in node.elts]
        return f"({', '.join(parts)})"

    elif isinstance(node, ast.List):
        parts = [expr_to_string(e) for e in node.elts]
        return f"[{', '.join(parts)}]"

    elif isinstance(node, ast.Dict):
        parts = []
        for k, v in zip(node.keys, node.values):
            key = expr_to_string(k) if k else "**"
            parts.append(f"{key}: {expr_to_string(v)}")
        return "{" + ", ".join(parts) + "}"

    elif isinstance(node, ast.Call):
        func = expr_to_string(node.func)
        args = [expr_to_string(a) for a in node.args]
        return f"{func}({', '.join(args)})"

    elif isinstance(node, ast.BinOp):
        op_map = {
            ast.BitOr: "|",
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
        }
        op = op_map.get(type(node.op), "?")
        return f"{expr_to_string(node.left)} {op} {expr_to_string(node.right)}"

    elif isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return f"-{expr_to_string(node.operand)}"
        elif isinstance(node.op, ast.Not):
            return f"not {expr_to_string(node.operand)}"
        return expr_to_string(node.operand)

    # Fallback: use ast.unparse if available (Python 3.9+)
    try:
        return ast.unparse(node)
    except:
        return "<complex>"


def is_cli_decorator(decorator: ast.expr) -> tuple[bool, str | None]:
    """Check if decorator is @cli or @cli("group"). Returns (is_cli, group_name)."""
    # @cli
    if isinstance(decorator, ast.Name) and decorator.id == "cli":
        return True, None

    # @something.cli
    if isinstance(decorator, ast.Attribute) and decorator.attr == "cli":
        return True, None

    # @cli("group") or @something.cli("group")
    if isinstance(decorator, ast.Call):
        func = decorator.func
        is_cli = False
        if isinstance(func, ast.Name) and func.id == "cli":
            is_cli = True
        elif isinstance(func, ast.Attribute) and func.attr == "cli":
            is_cli = True

        if is_cli:
            # Extract group name from first argument
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                return True, decorator.args[0].value
            return True, None

    return False, None


def extract_docstring(body: list[ast.stmt]) -> str | None:
    """Extract docstring from function body."""
    if body and isinstance(body[0], ast.Expr):
        if isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            return body[0].value.value
    return None


def extract_python_enums(content: str, tree: ast.Module = None) -> dict[str, list[str]]:
    """Extract Python Enum definitions from source code.

    Args:
        content: Python source code
        tree: Optional pre-parsed AST tree (avoids re-parsing)
    """
    enums = {}

    if tree is None:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return enums

    # All stdlib `enum` base classes users commonly subclass for their CLI
    # parameter types. `IntEnum` / `StrEnum` / `Flag` / `IntFlag` are all
    # subclasses of `Enum`, but AST inspection is textual — so each must be
    # matched by name explicitly. `ReprEnum` was added in 3.11 as the
    # common base of IntEnum/StrEnum; accept it too for forward-compat.
    ENUM_BASE_NAMES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_enum = False
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id in ENUM_BASE_NAMES:
                    is_enum = True
                    break
                if isinstance(base, ast.Attribute) and base.attr in ENUM_BASE_NAMES:
                    is_enum = True
                    break

            if is_enum:
                # Extract enum values (assignments in class body)
                values = []
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                values.append(target.id)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        values.append(item.target.id)

                if values:
                    enums[node.name] = values

    return enums


def extract_pydantic_models(content: str, tree: ast.Module = None) -> set[str]:
    """Extract names of classes textually declared as pydantic BaseModels.

    Scans `class Foo(BaseModel)` / `class Foo(pydantic.BaseModel)` and — to
    catch user base hierarchies like `class Settings(BaseModel)` followed by
    `class AppSettings(Settings)` — also the transitive closure within the
    file. Returns a set of class names.

    Used so `build_parser_for_function(..., help_only=True)` can tell whether
    an annotation refers to a pydantic model WITHOUT importing the user's
    module. If yes, we still pay the import cost (unavoidable — need field
    list). If no, we stay on the fast path. This preserves the expanded
    --host/--port flags in --help output for functions that actually use
    pydantic, while keeping non-pydantic commands fast.

    Deliberately cheap and textual: misses models defined via
    `create_model(...)` or BaseModel aliased with a non-obvious import.
    Those fall through to the non-expanded view in --help — argparse
    validation still works at invocation time.
    """
    if tree is None:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return set()

    # Direct pydantic bases we recognise by name. `BaseSettings` is the
    # pydantic-settings equivalent and is worth including.
    PYD_BASE_NAMES = {"BaseModel", "BaseSettings"}

    models: set[str] = set()
    # Multiple passes let us resolve transitive subclassing
    # (`class B(A)` where `A(BaseModel)` was declared earlier in the file).
    # Two passes is enough for typical cases; iterate to a fixed point just
    # in case.
    class_bases: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base_names = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)
            class_bases.append((node.name, base_names))

    changed = True
    while changed:
        changed = False
        for name, bases in class_bases:
            if name in models:
                continue
            if any(b in PYD_BASE_NAMES or b in models for b in bases):
                models.add(name)
                changed = True
    return models


_LAZY_ARG_CLASSES = {"DateArg", "DateTimeArg", "DateUtcArg", "DateTimeUtcArg"}


def _lazy_arg_from_default_expr(node: ast.expr):
    """If `node` is `DateArg("today")` / `DateTimeUtcArg("now")` / etc.,
    return {"cls": "DateArg", "arg": "today"}. Otherwise None.

    Matches Call(Name in LAZY_ARG_CLASSES, args=[Constant(str)]) with exactly
    one positional string argument and no kwargs — stricter than the
    runtime class signature on purpose, to avoid guessing.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _LAZY_ARG_CLASSES
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return {"cls": node.func.id, "arg": node.args[0].value}
    return None


def extract_parameters(args: ast.arguments) -> list[dict]:
    """Extract parameter information from function arguments."""
    params = []

    # Calculate defaults offset for positional args
    num_args = len(args.args)
    num_defaults = len(args.defaults)
    defaults_start = num_args - num_defaults

    def _set_default(param: dict, node: ast.expr):
        """Record both a source-text default AND (if applicable) a
        structured lazy-arg marker for dispatch-time evaluation."""
        param["default"] = expr_to_string(node)
        lazy = _lazy_arg_from_default_expr(node)
        if lazy:
            param["lazy_arg"] = lazy

    # Regular positional args
    for i, arg in enumerate(args.args):
        param = {
            "name": arg.arg,
        }
        if arg.annotation:
            param["type_annotation"] = expr_to_string(arg.annotation)
        if i >= defaults_start:
            _set_default(param, args.defaults[i - defaults_start])
        params.append(param)

    # *args
    if args.vararg:
        param = {
            "name": args.vararg.arg,
            "is_args": True,
        }
        if args.vararg.annotation:
            param["type_annotation"] = expr_to_string(args.vararg.annotation)
        params.append(param)

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        param = {
            "name": arg.arg,
        }
        if arg.annotation:
            param["type_annotation"] = expr_to_string(arg.annotation)
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            _set_default(param, args.kw_defaults[i])
        params.append(param)

    # **kwargs
    if args.kwarg:
        param = {
            "name": args.kwarg.arg,
            "is_kwargs": True,
        }
        if args.kwarg.annotation:
            param["type_annotation"] = expr_to_string(args.kwarg.annotation)
        params.append(param)

    return params


def extract_cli_functions(content: str, file_path: Path, base_dir: Path, return_tree: bool = False):
    """Extract @cli decorated functions from Python source.

    Args:
        return_tree: If True, returns (functions, tree) tuple for AST reuse
    """
    functions = []

    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        print(f"Parse error in {file_path}: {e}", file=sys.stderr)
        return (functions, None) if return_tree else functions

    # Compute module name from path
    try:
        relative = file_path.relative_to(base_dir)
    except ValueError:
        relative = file_path
    module_name = str(relative.with_suffix("")).replace("/", ".").replace(".__init__", "")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                is_cli, group = is_cli_decorator(decorator)
                if is_cli:
                    func_info = {
                        "name": node.name,
                        "module": module_name,
                        "file_path": str(file_path),
                        "parameters": extract_parameters(node.args),
                        "byte_offset": node.col_offset,
                    }
                    if group:
                        func_info["group"] = group
                    docstring = extract_docstring(node.body)
                    if docstring:
                        func_info["docstring"] = docstring
                    functions.append(func_info)
                    break  # Only process first @cli decorator

    return (functions, tree) if return_tree else functions


def scan_directory(base_dir: Path, cache_path: Path | None = None) -> dict:
    """Scan directory for @cli functions, using cache when possible."""
    # Load existing cache
    cache = {"version": "1.0", "files": {}}
    if cache_path and cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    old_files = cache.get("files", {})
    new_files = {}

    # Skip directories
    skip_dirs = {".git", "__pycache__", "venv", "node_modules", ".venv", "env", ".env"}

    for root, dirs, files in os.walk(base_dir):
        # Filter out skip directories
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

        for filename in files:
            if not filename.endswith(".py"):
                continue

            file_path = Path(root) / filename
            try:
                relative_path = str(file_path.relative_to(base_dir))
            except ValueError:
                relative_path = str(file_path)

            # Check if file changed using mtime (stat only, no file read)
            try:
                mtime = get_mtime(file_path)
            except OSError:
                continue

            # Use cache if mtime matches
            if relative_path in old_files and old_files[relative_path].get("mtime") == mtime:
                new_files[relative_path] = old_files[relative_path]
                continue

            # Parse file
            try:
                content = file_path.read_text()
            except IOError as e:
                print(f"Failed to read {file_path}: {e}", file=sys.stderr)
                continue

            functions = extract_cli_functions(content, file_path, base_dir)
            py_enums = extract_python_enums(content)

            # Cache both positive and negative results to avoid re-parsing
            new_files[relative_path] = {
                "mtime": mtime,
                "functions": functions,  # may be empty list
                "py_enums": py_enums,  # Python enum definitions in this file
            }

    # Filter to only include files with functions for the output
    result_files = {k: v for k, v in new_files.items() if v["functions"]}

    # Build enum cache from all _pb2.py files (protobuf enums)
    all_enums = {}
    for pb2_file in base_dir.rglob('*_pb2.py'):
        # Skip if in excluded directories
        if any(part.startswith('.') or part in ('__pycache__', 'venv', 'node_modules')
               for part in pb2_file.parts):
            continue
        enums = parse_pb2_enums(pb2_file)
        all_enums.update(enums)

    # Also add Python enums from all parsed files
    for file_info in new_files.values():
        py_enums = file_info.get('py_enums', {})
        all_enums.update(py_enums)

    last_scan = time.time()
    result = {"version": "1.0", "files": result_files, "enums": all_enums, "last_scan": last_scan}

    # But keep full cache for next run (with all files for mtime checking)
    if cache_path:
        full_cache = {"version": "1.0", "files": new_files, "enums": all_enums, "last_scan": last_scan}
        try:
            with open(cache_path, "w") as f:
                json.dump(full_cache, f, indent=2)
        except IOError:
            pass

    return result


def main():
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Fast Python CLI metadata extractor (pure Python version)",
    )
    parser.add_argument(
        "-d", "--dir",
        type=Path,
        default=Path("."),
        help="Directory to scan for Python files",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("cliche_cache.json"),
        help="Output JSON file path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show what would be extracted (don't write)",
    )

    args = parser.parse_args()

    print(f'Scanning "{args.dir}" for @cli decorated functions...', file=sys.stderr)

    # Use output path as cache path for incremental updates
    cache_path = args.output if not args.dry_run else None
    cache = scan_directory(args.dir, cache_path)

    total_functions = sum(len(e["functions"]) for e in cache["files"].values())
    print(f"Found {total_functions} @cli functions in {len(cache['files'])} files", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(cache, indent=2))
    else:
        print(f'Wrote cache to "{args.output}"', file=sys.stderr)


if __name__ == "__main__":
    main()
