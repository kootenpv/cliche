"""Tests for pydantic BaseModel expansion in run.py."""
import pytest
from pydantic import BaseModel

from cliche.run import (
    _is_pydantic_model,
    _pydantic_fields,
    _resolve_annotation_class,
    build_parser_for_function,
    invoke_function,
)


# Module-level models — referenced by annotation strings in test func_info dicts.
# _resolve_annotation_class imports the module and does getattr, so they must
# live at module scope (not inside a test function).

class SimpleModel(BaseModel):
    host: str
    port: int = 8080
    tls: bool = False


class ModelWithRequiredBool(BaseModel):
    enabled: bool
    name: str = "anon"


# Module-level functions referenced by invoke_function tests below. They run
# via the cached-lookup path (module importlib import, getattr by name), so
# they must be top-level names in this module.

def _handler(cfg: SimpleModel) -> dict:
    return cfg.model_dump()


def _bool_handler(cfg: ModelWithRequiredBool) -> dict:
    return cfg.model_dump()


class TestResolveAnnotationClass:
    def test_resolves_bare_name(self):
        assert _resolve_annotation_class("SimpleModel", __name__) is SimpleModel

    def test_strips_union_none(self):
        assert _resolve_annotation_class("SimpleModel | None", __name__) is SimpleModel

    def test_strips_optional(self):
        assert _resolve_annotation_class("Optional[SimpleModel]", __name__) is SimpleModel

    def test_unknown_name_returns_none(self):
        assert _resolve_annotation_class("NotAThing", __name__) is None

    def test_empty_returns_none(self):
        assert _resolve_annotation_class("", __name__) is None
        assert _resolve_annotation_class(None, __name__) is None

    def test_bad_module_returns_none(self):
        assert _resolve_annotation_class("SimpleModel", "no.such.module") is None


class TestIsPydanticModel:
    def test_detects_basemodel_subclass(self):
        assert _is_pydantic_model(SimpleModel) is True

    def test_rejects_plain_class(self):
        class NotAModel:
            pass
        assert _is_pydantic_model(NotAModel) is False

    def test_rejects_none(self):
        assert _is_pydantic_model(None) is False

    def test_rejects_non_class(self):
        assert _is_pydantic_model("SimpleModel") is False


class TestPydanticFields:
    def test_extracts_required_and_optional(self):
        fields = _pydantic_fields(SimpleModel)
        by_name = {name: (typ, default, req) for name, typ, default, req in fields}
        assert by_name["host"] == (str, None, True)
        assert by_name["port"] == (int, 8080, False)
        assert by_name["tls"] == (bool, False, False)


class TestBuildParserWithPydantic:
    def _func(self):
        return {
            "name": "serve", "cli_name": "serve",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "cfg", "type_annotation": "SimpleModel"}],
            "docstring": "serve",
        }

    def test_fields_become_flags(self):
        parser = build_parser_for_function(self._func())
        ns = parser.parse_args(["--host", "localhost", "--port", "9000", "--tls"])
        assert ns.host == "localhost"
        assert ns.port == 9000
        assert ns.tls is True

    def test_required_field_missing_errors(self):
        parser = build_parser_for_function(self._func())
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_optional_defaults_applied(self):
        parser = build_parser_for_function(self._func())
        ns = parser.parse_args(["--host", "x"])
        assert ns.port == 8080
        assert ns.tls is False

    def test_binding_recorded_for_invoke(self):
        parser = build_parser_for_function(self._func())
        binds = getattr(parser, "_pydantic_binds", None)
        assert binds == [("cfg", SimpleModel, ["host", "port", "tls"])]


class TestHelpOnlyPydanticExpansion:
    """help_only=True must still expand pydantic fields when the AST scanner
    named the model. Otherwise we regress help output for pydantic users."""

    def _func(self):
        return {
            "name": "serve", "cli_name": "serve",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "cfg", "type_annotation": "SimpleModel"}],
            "docstring": "serve",
        }

    def test_expands_when_model_in_set(self):
        parser = build_parser_for_function(
            self._func(), help_only=True, pydantic_models={"SimpleModel"},
        )
        # Expanded flags parse exactly like the non-help path.
        ns = parser.parse_args(["--host", "x", "--port", "9", "--tls"])
        assert (ns.host, ns.port, ns.tls) == ("x", 9, True)
        binds = getattr(parser, "_pydantic_binds", None)
        assert binds == [("cfg", SimpleModel, ["host", "port", "tls"])]

    def test_skipped_when_model_not_in_set(self):
        # AST scanner didn't see this class → we must NOT import the user
        # module to check. The param falls through to the non-expanded
        # path (single --cfg flag rather than --host/--port/--tls).
        parser = build_parser_for_function(
            self._func(), help_only=True, pydantic_models=set(),
        )
        # No per-field flags were registered.
        with pytest.raises(SystemExit):
            parser.parse_args(["--host", "x"])
        binds = getattr(parser, "_pydantic_binds", None)
        assert not binds

    def test_strips_optional_wrapper(self):
        # `Optional[SimpleModel]` and `SimpleModel | None` should both
        # resolve to the bare class name when checking the set.
        func = self._func()
        func["parameters"][0]["type_annotation"] = "Optional[SimpleModel]"
        parser = build_parser_for_function(
            func, help_only=True, pydantic_models={"SimpleModel"},
        )
        ns = parser.parse_args(["--host", "x"])
        assert ns.host == "x"

    def test_extract_pydantic_models_ast(self):
        """End-to-end: the AST extractor flags SimpleModel so the
        `help_only` path above receives a populated set from the cache."""
        from cliche.main import extract_pydantic_models
        src = (
            "from pydantic import BaseModel\n"
            "class Inner(BaseModel):\n    x: int\n"
            "class Outer(Inner):\n    y: int\n"
            "class Unrelated:\n    pass\n"
        )
        assert extract_pydantic_models(src) == {"Inner", "Outer"}


class TestInvokeFunctionWithPydantic:
    def test_model_reconstructed_at_invoke(self):
        """End-to-end: parse → invoke → function receives real BaseModel instance."""
        func = {
            "name": "_handler", "cli_name": "_handler",
            "module": __name__, "file_path": "",
            "parameters": [{"name": "cfg", "type_annotation": "SimpleModel"}],
            "docstring": "",
        }
        parser = build_parser_for_function(func)
        ns = parser.parse_args(["--host", "h", "--port", "1", "--tls"])

        # The handler returns its cfg's dump — if reconstruction didn't happen
        # the call would blow up (dict has no .model_dump()) or pass wrong type.
        # Capture via invoke_function. Redirect stdout — invoke_function prints.
        from io import StringIO
        import sys
        old = sys.stdout
        sys.stdout = StringIO()
        try:
            invoke_function(func, ns, pydantic_binds=parser._pydantic_binds)
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old
        import json
        data = json.loads(output)
        assert data == {"host": "h", "port": 1, "tls": True}
