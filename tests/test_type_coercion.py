"""Tests for date / datetime / dict type coercion added to run.py."""
from datetime import date, datetime
from pathlib import Path

import pytest

from cliche.run import (
    _DictAction,
    _parse_date,
    _parse_datetime,
    _parse_dict_annotation,
    _resolve_callable_type,
    build_parser_for_function,
    parse_default,
    type_from_annotation,
)


# ---------- date / datetime ----------

class TestParseDate:
    def test_valid_iso_date(self):
        assert _parse_date("2026-04-22") == date(2026, 4, 22)

    def test_leap_day(self):
        assert _parse_date("2024-02-29") == date(2024, 2, 29)

    def test_bad_format_rejected(self):
        with pytest.raises(ValueError):
            _parse_date("04/22/2026")

    def test_datetime_string_rejected(self):
        # Strict YYYY-MM-DD only — a datetime string should fail.
        with pytest.raises(ValueError):
            _parse_date("2026-04-22T10:00:00")


class TestParseDatetime:
    def test_iso_with_time(self):
        assert _parse_datetime("2026-04-22T10:30:00") == datetime(2026, 4, 22, 10, 30, 0)

    def test_date_only_is_accepted(self):
        # fromisoformat accepts bare dates, returning midnight.
        assert _parse_datetime("2026-04-22") == datetime(2026, 4, 22, 0, 0, 0)

    def test_bad_format_rejected(self):
        with pytest.raises(ValueError):
            _parse_datetime("not-a-date")


class TestTypeFromAnnotation:
    @pytest.mark.parametrize("annotation, expected", [
        ("date", _parse_date),
        ("datetime.date", _parse_date),
        ("datetime", _parse_datetime),
        ("datetime.datetime", _parse_datetime),
    ])
    def test_date_annotations_resolve_to_parser(self, annotation, expected):
        assert type_from_annotation(annotation) is expected

    def test_primitives_still_work(self):
        assert type_from_annotation("str") is str
        assert type_from_annotation("int") is int
        assert type_from_annotation("float") is float
        assert type_from_annotation("bool") is bool

    def test_unknown_falls_back_to_str(self):
        assert type_from_annotation("SomeCustomType") is str


class TestPathCoercion:
    """pathlib.Path handling — regressions caught via the ralph loop."""

    def test_bare_path_coerces(self):
        assert type_from_annotation("Path") is Path

    def test_qualified_pathlib_path_coerces(self):
        assert type_from_annotation("pathlib.Path") is Path

    def test_optional_path_coerces(self):
        assert type_from_annotation("Optional[Path]") is Path

    def test_pep604_path_or_none_coerces(self):
        # PEP 604 canonical form — AST unparse always produces this shape.
        assert type_from_annotation("Path | None") is Path


class TestPep604UnionGeneral:
    """PEP 604 unions beyond `T | None`. Always picks the most-specific mapped
    type across parts, preferring non-str matches."""

    @pytest.mark.parametrize("annotation, expected", [
        ("Path | None", Path),
        ("None | Path", Path),
        ("Path | str",  Path),      # Path wins over str
        ("str | Path",  Path),      # order-independent
        ("int | None",  int),
        ("int | float", int),       # first mapped wins when neither is str
        ("str | str",   str),
    ])
    def test_union_resolves_to_most_specific(self, annotation, expected):
        assert type_from_annotation(annotation) is expected

    def test_all_unknown_falls_back_to_str(self):
        assert type_from_annotation("Foo | Bar") is str


class TestPathDefaults:
    """Path defaults written as `Path("/tmp")` in source become the literal
    string 'Path("/tmp")' by the time parse_default sees them. We extract
    the inner string arg so argparse can coerce it via `type=Path` into a
    real Path at invoke time."""

    def test_path_call_with_double_quotes(self):
        assert parse_default('Path("/tmp")', Path) == "/tmp"

    def test_path_call_with_single_quotes(self):
        assert parse_default("Path('/tmp')", Path) == "/tmp"

    def test_pathlib_qualified_call(self):
        assert parse_default('pathlib.Path("/etc/passwd")', Path) == "/etc/passwd"

    def test_empty_string_path(self):
        assert parse_default('Path("")', Path) == ""

    def test_none_default_unchanged(self):
        assert parse_default("None", Path) is None

    def test_computed_expression_falls_through_verbatim(self):
        # Documented limitation — use a sentinel + convert inside the function.
        assert parse_default('Path("/tmp") / "x"', Path) == 'Path("/tmp") / "x"'

    def test_non_literal_call_arg_falls_through(self):
        # `Path(HOME)` — variable reference, not a string literal.
        assert parse_default('Path(HOME)', Path) == 'Path(HOME)'


class TestCustomCallableTypes:
    """User-defined callables used as annotations flow through to argparse's
    `type=`. Lets users plug in validators like Port / URL / NonEmpty without
    reaching for pydantic."""

    def test_resolves_module_callable(self):
        resolved = _resolve_callable_type('_tc_port', __name__)
        assert resolved is _tc_port

    def test_skips_primitives(self):
        # Primitives are handled by type_from_annotation; this resolver must
        # not shadow them even when a same-named symbol exists in scope.
        for name in ('str', 'int', 'float', 'bool', 'Path'):
            assert _resolve_callable_type(name, __name__) is None

    def test_skips_pydantic_models(self):
        # pydantic BaseModels have a dedicated expansion path.
        assert _resolve_callable_type('_TcPydModel', __name__) is None

    def test_skips_enum_classes(self):
        # Enums have a dedicated choices/convert path.
        assert _resolve_callable_type('_TcEnum', __name__) is None

    def test_missing_name_returns_none(self):
        assert _resolve_callable_type('NoSuchThing', __name__) is None

    def test_non_identifier_returns_none(self):
        # Complex annotations (containers, unions) should fall through to
        # the normal type_from_annotation paths, not this resolver.
        assert _resolve_callable_type('list[Port]', __name__) is None
        assert _resolve_callable_type('Port | None', __name__) is None

    def test_empty_inputs_return_none(self):
        assert _resolve_callable_type('', __name__) is None
        assert _resolve_callable_type('Port', '') is None

    def test_build_parser_applies_custom_type_positional(self):
        func = {
            "name": "serve", "cli_name": "serve",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "port", "type_annotation": "_tc_port"}],
            "docstring": "",
        }
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["8080"])
        assert ns.port == 8080 and isinstance(ns.port, int)

    def test_build_parser_applies_custom_type_flag(self):
        func = {
            "name": "serve", "cli_name": "serve",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "host", "type_annotation": "_tc_nonempty",
                            "default": '"localhost"'}],
            "docstring": "",
        }
        parser = build_parser_for_function(func)
        ns = parser.parse_args([])
        assert ns.host == "localhost"
        ns = parser.parse_args(["--host", "acme.local"])
        assert ns.host == "acme.local"

    def test_build_parser_custom_type_error_exits(self, capsys):
        func = {
            "name": "serve", "cli_name": "serve",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "port", "type_annotation": "_tc_port"}],
            "docstring": "",
        }
        parser = build_parser_for_function(func)
        with pytest.raises(SystemExit):
            parser.parse_args(["70000"])
        err = capsys.readouterr().err
        assert "port out of range" in err


# Module-level helpers for TestCustomCallableTypes. They MUST live at module
# scope because `_resolve_callable_type` looks them up via importlib — nested
# defs inside the test class wouldn't be visible to `getattr(module, name)`.

import argparse as _argparse  # noqa: E402
from enum import Enum as _Enum  # noqa: E402
from pydantic import BaseModel as _BaseModel  # noqa: E402


def _tc_port(s: str) -> int:
    n = int(s)
    if not (1 <= n <= 65535):
        raise _argparse.ArgumentTypeError(f"port out of range: {n}")
    return n


def _tc_nonempty(s: str) -> str:
    if not s:
        raise ValueError("must be non-empty")
    return s


class _TcEnum(_Enum):
    A = "a"
    B = "b"


class _TcPydModel(_BaseModel):
    name: str


class TestBuildParserPathDefault:
    """End-to-end: a Path-typed param with a `Path("/tmp")` source default
    must materialise as a real Path at parse time, not as a string."""

    def _func(self):
        return {
            "name": "f", "cli_name": "f", "module": "", "file_path": "",
            "parameters": [{
                "name": "p",
                "type_annotation": "Path",
                "default": 'Path("/tmp")',
            }],
            "docstring": "",
        }

    def test_default_becomes_real_path(self):
        parser = build_parser_for_function(self._func())
        ns = parser.parse_args([])
        assert isinstance(ns.p, Path)
        assert ns.p == Path("/tmp")

    def test_user_override_still_path(self):
        parser = build_parser_for_function(self._func())
        ns = parser.parse_args(["--p", "/etc/hosts"])
        assert isinstance(ns.p, Path)
        assert ns.p == Path("/etc/hosts")


class TestContainerElementCoercion:
    """`list[T]` / `tuple[T, ...]` must pass through T as argparse type=, not str.
    Historical failure: cached annotation wraps the tuple slice in parens
    (`tuple[(int, ...)]`), so naive parsing yielded `"(int"` and fell back to str."""

    @pytest.mark.parametrize("annotation, expected", [
        ("list[int]",               int),
        ("list[float]",             float),
        ("list[Path]",              Path),
        ("list[str]",               str),
        ("List[int]",               int),
        ("tuple[int, ...]",         int),
        ("tuple[Path, ...]",        Path),
        ("tuple[float, ...]",       float),
        # AST-unparse wraps tuple subscript in parens — this is the form
        # actually stored in the runtime cache and must parse correctly.
        ("tuple[(int, ...)]",       int),
        ("tuple[(Path, ...)]",      Path),
        ("tuple[(float, ...)]",     float),
        ("Tuple[(int, ...)]",       int),
    ])
    def test_element_type_extracted(self, annotation, expected):
        assert type_from_annotation(annotation) is expected

    def test_unknown_inner_falls_back_to_str(self):
        assert type_from_annotation("list[SomeEnum]") is str
        assert type_from_annotation("tuple[(CustomType, ...)]") is str


class TestBuildParserContainerCoercion:
    """End-to-end through build_parser_for_function: each token must come back
    as the declared element type, not as a str."""

    def _func(self, annotation: str, name: str = "items") -> dict:
        return {
            "name": "f", "cli_name": "f", "module": "", "file_path": "",
            "parameters": [{"name": name, "type_annotation": annotation}],
            "docstring": "",
        }

    def test_tuple_int_positional_returns_ints(self):
        parser = build_parser_for_function(self._func("tuple[(int, ...)]", "nums"))
        ns = parser.parse_args(["1", "2", "3"])
        assert ns.nums == [1, 2, 3]
        assert all(isinstance(n, int) for n in ns.nums)

    def test_tuple_path_positional_returns_paths(self):
        parser = build_parser_for_function(self._func("tuple[(Path, ...)]", "paths"))
        ns = parser.parse_args(["/etc/passwd", "/etc/hosts"])
        assert ns.paths == [Path("/etc/passwd"), Path("/etc/hosts")]
        assert all(isinstance(p, Path) for p in ns.paths)


# ---------- dict[K, V] ----------

class TestParseDictAnnotation:
    def test_plain_form(self):
        assert _parse_dict_annotation("dict[str, int]") == (str, int)

    def test_capitalized_form(self):
        assert _parse_dict_annotation("Dict[str, float]") == (str, float)

    def test_parenthesised_form_from_ast_unparse(self):
        # The AST → string round-trip wraps the tuple slice in parens.
        assert _parse_dict_annotation("dict[(str, int)]") == (str, int)

    def test_whitespace_tolerant(self):
        assert _parse_dict_annotation("dict[ str , int ]") == (str, int)

    def test_non_dict_returns_none(self):
        assert _parse_dict_annotation("list[int]") is None
        assert _parse_dict_annotation("str") is None
        assert _parse_dict_annotation("") is None
        assert _parse_dict_annotation(None) is None

    def test_bare_dict_without_params_returns_none(self):
        # No K, V info — nothing to coerce.
        assert _parse_dict_annotation("dict") is None

    def test_unknown_types_fall_back_to_str(self):
        assert _parse_dict_annotation("dict[CustomKey, CustomVal]") == (str, str)

    def test_path_value_type(self):
        # `dict[str, Path]` must coerce values to Path, not leave them as str.
        assert _parse_dict_annotation("dict[str, Path]") == (str, Path)

    def test_path_value_type_parenthesised(self):
        # AST-unparse form.
        assert _parse_dict_annotation("dict[(str, Path)]") == (str, Path)


class TestDictAction:
    """Build a real argparse parser and exercise the action end-to-end."""

    def _parser(self, *, key_type=str, value_type=int, **kwargs):
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--tags", action=_DictAction, key_type=key_type, value_type=value_type,
                       nargs="*", **kwargs)
        return p

    def test_single_pair(self):
        p = self._parser()
        ns = p.parse_args(["--tags", "a=1"])
        assert ns.tags == {"a": 1}

    def test_multiple_pairs(self):
        p = self._parser()
        ns = p.parse_args(["--tags", "a=1", "b=2", "c=3"])
        assert ns.tags == {"a": 1, "b": 2, "c": 3}

    def test_int_keys(self):
        p = self._parser(key_type=int, value_type=str)
        ns = p.parse_args(["--tags", "1=alpha", "2=beta"])
        assert ns.tags == {1: "alpha", 2: "beta"}

    def test_value_with_equals_in_it(self):
        # Only the first `=` splits — rest is part of the value.
        p = self._parser(value_type=str)
        ns = p.parse_args(["--tags", "url=https://x.com/?q=1"])
        assert ns.tags == {"url": "https://x.com/?q=1"}

    def test_missing_equals_errors(self, capsys):
        p = self._parser()
        with pytest.raises(SystemExit):
            p.parse_args(["--tags", "just_a_key"])
        err = capsys.readouterr().err
        assert "KEY=VALUE" in err

    def test_bad_value_conversion_errors(self, capsys):
        p = self._parser()  # value_type=int
        with pytest.raises(SystemExit):
            p.parse_args(["--tags", "a=not_an_int"])
        err = capsys.readouterr().err
        assert "bad key/value" in err or "invalid" in err.lower()


# ---------- end-to-end via build_parser_for_function ----------

def _func_info(name: str, module: str, parameters: list, docstring: str = "") -> dict:
    """Build the dict build_parser_for_function expects."""
    return {
        "name": name,
        "cli_name": name.replace("_", "-"),
        "module": module,
        "file_path": "",
        "parameters": parameters,
        "docstring": docstring,
    }


class TestBuildParserDateAndDict:
    def test_date_positional_coerces(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "d", "type_annotation": "date"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["2026-04-22"])
        assert ns.d == date(2026, 4, 22)

    def test_datetime_positional_coerces(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "t", "type_annotation": "datetime"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["2026-04-22T10:30:00"])
        assert ns.t == datetime(2026, 4, 22, 10, 30, 0)

    def test_dict_optional_with_default(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "tags", "type_annotation": "dict[str, int]", "default": "{}"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["--tags", "a=1", "b=2"])
        assert ns.tags == {"a": 1, "b": 2}

    def test_dict_optional_default_empty_when_omitted(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "tags", "type_annotation": "dict[str, int]", "default": "{}"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args([])
        assert ns.tags == {}

    def test_dict_ast_unparse_form(self):
        # Simulates what the cache actually stores after AST round-trip.
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "tags", "type_annotation": "dict[(str, int)]", "default": "{}"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["--tags", "x=42"])
        assert ns.tags == {"x": 42}


class TestBuildParserDictKVMatrix:
    """End-to-end coverage of the dict[K, V] combinations: keys and values must
    arrive as the declared types, not strings. Catches regressions where a
    future refactor drops V-coercion (or K-coercion) for specific types."""

    @pytest.mark.parametrize("annotation, argv, expected, v_type", [
        ("dict[str, int]",   ["--m", "a=1", "b=2"],    {"a": 1, "b": 2},           int),
        ("dict[str, float]", ["--m", "x=1.5", "y=2.0"], {"x": 1.5, "y": 2.0},      float),
        ("dict[str, str]",   ["--m", "k=v", "a=hello"], {"k": "v", "a": "hello"},  str),
        ("dict[int, str]",   ["--m", "1=a", "2=b"],    {1: "a", 2: "b"},           str),
        ("dict[str, Path]",  ["--m", "cfg=/etc/hosts"], {"cfg": Path("/etc/hosts")}, Path),
        # AST-unparse parenthesised form — must produce the same result.
        ("dict[(str, Path)]", ["--m", "cfg=/etc/hosts"], {"cfg": Path("/etc/hosts")}, Path),
        ("dict[(int, float)]", ["--m", "3=3.14"],       {3: 3.14},                 float),
    ])
    def test_kv_types_coerced(self, annotation, argv, expected, v_type):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "m", "type_annotation": annotation, "default": "{}"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(argv)
        assert ns.m == expected
        # Check element types, not just equality (True == 1, Path("x") == "x" via __eq__).
        for v in ns.m.values():
            assert isinstance(v, v_type), f"value {v!r} not {v_type.__name__}"


class TestBuildParserPathUnions:
    """PEP 604 `Path | None` and qualified `pathlib.Path` must coerce end-to-end
    through build_parser_for_function, not just through type_from_annotation."""

    def test_pep604_path_or_none_with_default(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "p", "type_annotation": "Path | None", "default": "None"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["--p", "/etc/hosts"])
        assert isinstance(ns.p, Path)
        assert ns.p == Path("/etc/hosts")

    def test_pep604_path_or_none_omitted_is_none(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "p", "type_annotation": "Path | None", "default": "None"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args([])
        assert ns.p is None

    def test_qualified_pathlib_path_positional(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "p", "type_annotation": "pathlib.Path"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["/etc/hosts"])
        assert isinstance(ns.p, Path)
        assert ns.p == Path("/etc/hosts")

    def test_optional_path_old_form_coerces(self):
        func = _func_info("f", "tests.test_type_coercion", [
            {"name": "p", "type_annotation": "Optional[Path]", "default": "None"},
        ])
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["--p", "/tmp/x"])
        assert isinstance(ns.p, Path)
        assert ns.p == Path("/tmp/x")
