"""cliche - Fast CLI generator with caching."""
from importlib.metadata import version as _pkg_version, PackageNotFoundError

try:
    __version__ = _pkg_version("cliche")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

from cliche.install import install, uninstall
from cliche.types import DateArg, DateTimeArg, DateTimeUtcArg, DateUtcArg


def cli(fn_or_group=None):
    """No-op decorator — cliche detects @cli via AST parsing.

    Supports both @cli and @cli("group") forms.
    """
    if callable(fn_or_group):
        return fn_or_group  # @cli (bare)
    def _decorator(fn):
        return fn
    return _decorator  # @cli("group")
