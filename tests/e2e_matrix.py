"""The argv matrix every e2e test draws from.

Kept separate from the test file so conftest can pre-run all cases
concurrently at session start without importing the test module.
"""

E2E_ARGV_MATRIX: dict[str, list[str]] = {
    # date / datetime
    "date_ok":            ["echo-date", "2026-04-22"],
    "date_bad":           ["echo-date", "04/22/2026"],
    "datetime_full":      ["echo-datetime", "2026-04-22T10:30:00"],
    "datetime_date_only": ["echo-datetime", "2026-04-22"],

    # dict
    "dict_empty":         ["echo-dict"],
    "dict_multi":         ["echo-dict", "--tags", "alpha=1", "beta=2", "gamma=3"],
    "dict_int_coerce":    ["echo-dict", "--tags", "x=42"],
    "dict_no_equals":     ["echo-dict", "--tags", "nokeyvalue"],
    "dict_str_values":    ["echo-dict-str", "--meta", "a=hello", "b=world"],

    # enums
    "enum_ok":            ["echo-enum", "RED"],
    "enum_bad":           ["echo-enum", "PURPLE"],
    "enum_default":       ["echo-enum-default"],  # regression: Color.RED qualified default
    "enum_default_set":   ["echo-enum-default", "--color", "BLUE"],

    # Path coercion + container element coercion
    "path_single":        ["echo-path", "/etc/passwd"],
    "path_list":          ["echo-path-list", "/etc/passwd", "/etc/hosts"],
    "int_list_sum":       ["echo-int-list", "1", "2", "3"],

    # pydantic
    "pyd_defaults":       ["serve", "--host", "acme.local"],
    "pyd_all":            ["serve", "--host", "x", "--port", "9000", "--tls"],
    "pyd_missing":        ["serve"],

    # bool inversion
    "bool_T_default":     ["with-cache"],
    "bool_T_flipped":     ["with-cache", "--no-use-cache"],
    "bool_F_default":     ["with-verbose"],
    "bool_F_set":         ["with-verbose", "--verbose"],

    # grouped subcommands
    "group_add":          ["math", "add", "2", "3"],
    "group_mul":          ["math", "mul", "4", "5"],

    # async
    "async_run":          ["run-async", "--n", "7"],

    # --raw / --notraceback
    "raw_mode":           ["--raw", "echo-dict", "--tags", "a=1"],
    "default_pretty":     ["echo-dict", "--tags", "a=1"],
    "err_traceback":      ["raises"],
    "err_terse":          ["--notraceback", "raises"],

    # discovery
    "help":               ["--help"],
    "llm":                ["--llm"],
    "cli_info":           ["--cli"],
}
