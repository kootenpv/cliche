"""cliche - Fast CLI generator with caching."""

_LAZY = {
    "install": ("cliche.install", "install"),
    "uninstall": ("cliche.install", "uninstall"),
    "DateArg": ("cliche.types", "DateArg"),
    "DateTimeArg": ("cliche.types", "DateTimeArg"),
    "DateTimeUtcArg": ("cliche.types", "DateTimeUtcArg"),
    "DateUtcArg": ("cliche.types", "DateUtcArg"),
}


def __getattr__(name):
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version
        try:
            v = _pkg_version("cliche")
        except PackageNotFoundError:
            v = "0.0.0+unknown"
        globals()["__version__"] = v
        return v
    if name in _LAZY:
        mod_name, attr = _LAZY[name]
        import importlib
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'cliche' has no attribute {name!r}")


def cli(fn_or_group=None):
    """No-op decorator — cliche detects @cli via AST parsing.

    Supports both @cli and @cli("group") forms.
    """
    if callable(fn_or_group):
        return fn_or_group  # @cli (bare)
    def _decorator(fn):
        return fn
    return _decorator  # @cli("group")
