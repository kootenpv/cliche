"""End-to-end tests: real binary, real argparse.

Every subprocess call is pre-run concurrently by the `cli_results` session
fixture (see conftest.py). Each test just looks up its case and asserts —
no per-test subprocess latency. Cuts e2e wall-clock from ~3s to ~1s while
keeping one pytest test per behaviour (readable output, `pytest -k` works).

If you add a test here, add the argv it needs to `tests/e2e_matrix.py`.
"""
import json


def _data(cli_results, key):
    """Short helper: look up the CompletedProcess and parse its stdout as JSON."""
    p = cli_results[key]
    assert p.returncode == 0, f"[{key}] rc={p.returncode} stderr={p.stderr!r}"
    return json.loads(p.stdout)


# ---------- date / datetime coercion ----------

def test_echo_date_roundtrip(cli_results):
    assert _data(cli_results, "date_ok") == {"iso": "2026-04-22", "type": "date"}


def test_echo_date_rejects_bad_format(cli_results):
    assert cli_results["date_bad"].returncode != 0


def test_echo_datetime_with_time(cli_results):
    data = _data(cli_results, "datetime_full")
    assert data == {"iso": "2026-04-22T10:30:00", "type": "datetime"}


def test_echo_datetime_date_only_ok(cli_results):
    assert _data(cli_results, "datetime_date_only")["iso"] == "2026-04-22T00:00:00"


# ---------- dict[K, V] ----------

def test_echo_dict_empty_default(cli_results):
    assert _data(cli_results, "dict_empty") == {"tags": {}, "count": 0}


def test_echo_dict_multiple_pairs(cli_results):
    assert _data(cli_results, "dict_multi") == {
        "tags": {"alpha": 1, "beta": 2, "gamma": 3},
        "count": 3,
    }


def test_echo_dict_values_are_ints_not_strings(cli_results):
    data = _data(cli_results, "dict_int_coerce")
    assert data["tags"]["x"] == 42
    assert isinstance(data["tags"]["x"], int)


def test_echo_dict_rejects_missing_equals(cli_results):
    p = cli_results["dict_no_equals"]
    assert p.returncode != 0
    assert "KEY=VALUE" in p.stderr


def test_echo_dict_str_values_stay_strings(cli_results):
    assert _data(cli_results, "dict_str_values") == {"meta": {"a": "hello", "b": "world"}}


# ---------- enums ----------

def test_echo_enum_accepts_valid_member(cli_results):
    assert _data(cli_results, "enum_ok") == {"color": "red"}


def test_echo_enum_rejects_invalid_member(cli_results):
    assert cli_results["enum_bad"].returncode != 0


def test_echo_enum_default_uses_source_literal(cli_results):
    """Regression: `color: Color = Color.RED` stores the default as the
    literal string "Color.RED". Without qualified-form stripping in
    convert_enum_args, the function receives that string and crashes on
    `.value`. Should print `red`."""
    assert _data(cli_results, "enum_default") == {"color": "red"}


def test_echo_enum_default_overridden_by_flag(cli_results):
    assert _data(cli_results, "enum_default_set") == {"color": "blue"}


# ---------- IntEnum ----------

def test_echo_int_enum_accepts_valid_member(cli_results):
    """IntEnum should be handled the same as Enum: lookup by member name,
    and the function receives a real enum instance (also an int)."""
    assert _data(cli_results, "intenum_ok") == {
        "name": "HIGH",
        "value": 10,
        "is_priority": True,
        "is_int": True,
    }


def test_echo_int_enum_rejects_invalid_member(cli_results):
    p = cli_results["intenum_bad"]
    assert p.returncode != 0
    assert "URGENT" in p.stderr


def test_echo_int_enum_default_uses_source_literal(cli_results):
    """`level: Priority = Priority.MEDIUM` — the qualified default literal
    must be stripped and looked up as an IntEnum member."""
    assert _data(cli_results, "intenum_default") == {"name": "MEDIUM", "value": 5}


def test_echo_int_enum_default_overridden_by_flag(cli_results):
    assert _data(cli_results, "intenum_default_set") == {"name": "LOW", "value": 1}


# ---------- Path coercion ----------

def test_echo_path_is_real_path_instance(cli_results):
    assert _data(cli_results, "path_single") == {"name": "passwd", "is_path": True}


def test_tuple_of_paths_each_coerced(cli_results):
    """Regression: list/tuple element types previously fell through to str,
    so `tuple[Path, ...]` yielded strings and `.name` raised AttributeError."""
    assert _data(cli_results, "path_list") == {
        "names": ["passwd", "hosts"],
        "count": 2,
    }


def test_tuple_of_ints_each_coerced(cli_results):
    """Regression: `tuple[int, ...]` used to deliver strings; `sum()` would
    raise TypeError. Now the argparse `type=` does the coercion."""
    data = _data(cli_results, "int_list_sum")
    assert data["total"] == 6
    assert data["first_type"] == "int"


# ---------- pydantic ----------

def test_serve_pydantic_defaults(cli_results):
    assert _data(cli_results, "pyd_defaults") == {"host": "acme.local", "port": 8080, "tls": False}


def test_serve_pydantic_all_fields(cli_results):
    assert _data(cli_results, "pyd_all") == {"host": "x", "port": 9000, "tls": True}


def test_serve_pydantic_missing_required_errors(cli_results):
    p = cli_results["pyd_missing"]
    assert p.returncode != 0
    assert "host" in p.stderr.lower()


# ---------- bool inversion ----------

def test_bool_default_true_no_flag_keeps_default(cli_results):
    assert _data(cli_results, "bool_T_default")["use_cache"] is True


def test_bool_default_true_flipped_by_no_prefix(cli_results):
    assert _data(cli_results, "bool_T_flipped")["use_cache"] is False


def test_bool_default_false_no_flag_stays_false(cli_results):
    assert _data(cli_results, "bool_F_default")["verbose"] is False


def test_bool_default_false_enabled_by_flag(cli_results):
    assert _data(cli_results, "bool_F_set")["verbose"] is True


# ---------- grouped subcommands ----------

def test_grouped_subcommand_add(cli_results):
    assert _data(cli_results, "group_add") == {"sum": 5}


def test_grouped_subcommand_mul(cli_results):
    assert _data(cli_results, "group_mul") == {"product": 20}


# ---------- async ----------

def test_async_function_wrapped_with_asyncio_run(cli_results):
    assert _data(cli_results, "async_run") == {"n": 14}


# ---------- global flags: --raw ----------

def test_raw_mode_plain_print(cli_results):
    p = cli_results["raw_mode"]
    assert p.returncode == 0, p.stderr
    # Plain print of a dict uses Python repr with single quotes.
    assert "'tags':" in p.stdout or "{'tags'" in p.stdout
    assert '"tags": {' not in p.stdout


def test_default_mode_is_pretty_json(cli_results):
    p = cli_results["default_pretty"]
    assert '"tags":' in p.stdout
    assert "\n" in p.stdout  # indented


# ---------- global flags: --notraceback ----------

def test_default_error_shows_traceback(cli_results):
    p = cli_results["err_traceback"]
    assert p.returncode != 0
    assert "Traceback" in p.stderr or "ValueError" in p.stderr


def test_notraceback_shows_terse_message_only(cli_results):
    p = cli_results["err_terse"]
    assert p.returncode != 0
    assert p.stderr.strip() == "ValueError: intentional error from fixture"
    assert "Traceback" not in p.stderr


# ---------- discovery flags ----------

def test_help_shows_all_commands(cli_results):
    p = cli_results["help"]
    assert p.returncode == 0
    for cmd in ("echo-date", "echo-dict", "serve", "with-cache", "math"):
        assert cmd in p.stdout, f"missing {cmd} in --help output"


def test_llm_output_lists_commands(cli_results):
    p = cli_results["llm"]
    assert p.returncode == 0
    assert "echo_date" in p.stdout or "echo-date" in p.stdout


def test_cli_info(cli_results):
    p = cli_results["cli_info"]
    assert p.returncode == 0
    assert "Python Version" in p.stdout
