"""Every feature we want to smoke-test via subprocess, one @cli per behaviour.

Each function returns a JSON-serialisable dict so tests can
`json.loads(stdout)` and assert on the structure. Keep the functions
small — this is fixture code, not example code.
"""
from datetime import date, datetime
from enum import Enum, IntEnum
from pathlib import Path

from cliche import cli
from pydantic import BaseModel


class Color(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class Priority(IntEnum):
    LOW = 1
    MEDIUM = 5
    HIGH = 10


class Config(BaseModel):
    host: str
    port: int = 8080
    tls: bool = False


# ---------- type coercion ----------

@cli
def echo_date(d: date):
    """Echo an ISO date back as JSON."""
    return {"iso": d.isoformat(), "type": type(d).__name__}


@cli
def echo_datetime(t: datetime):
    """Echo a datetime back as JSON."""
    return {"iso": t.isoformat(), "type": type(t).__name__}


@cli
def echo_dict(tags: dict[str, int] = {}):
    """Echo a dict[str, int] back."""
    return {"tags": tags, "count": len(tags)}


@cli
def echo_dict_str(meta: dict[str, str] = {}):
    """Echo a dict[str, str] back — values shouldn't be int-coerced."""
    return {"meta": meta}


# ---------- Path coercion (bugs caught via ralph) ----------

@cli
def echo_path(p: Path):
    """Path-typed positional — must arrive as a real pathlib.Path."""
    return {"name": p.name, "is_path": isinstance(p, Path)}


@cli
def echo_path_list(paths: tuple[Path, ...]):
    """`tuple[Path, ...]` — each element must be a Path, not a str."""
    return {"names": [p.name for p in paths], "count": len(paths)}


@cli
def echo_int_list(nums: tuple[int, ...]):
    """`tuple[int, ...]` — elements must be ints so `sum(nums)` works
    without any manual int() conversion inside the function body."""
    return {"total": sum(nums), "first_type": type(nums[0]).__name__}


# ---------- enums ----------

@cli
def echo_enum(color: Color):
    """Positional enum param."""
    return {"color": color.value}


@cli
def echo_enum_default(color: Color = Color.RED):
    """Source-parsed Enum default stored as string literal `Color.RED` —
    must be converted to the real enum member before the function runs."""
    return {"color": color.value}


@cli
def echo_int_enum(level: Priority):
    """Positional IntEnum param — `Priority` inherits from `IntEnum`,
    so `level` should arrive as the real enum member (which is also an int)."""
    return {
        "name": level.name,
        "value": int(level),
        "is_priority": isinstance(level, Priority),
        "is_int": isinstance(level, int),
    }


@cli
def echo_int_enum_default(level: Priority = Priority.MEDIUM):
    """IntEnum default written in qualified form — must be converted to
    the real enum member before the function runs (same path as Enum)."""
    return {"name": level.name, "value": int(level)}


# ---------- pydantic ----------

@cli
def serve(cfg: Config):
    """Pydantic model expanded to --host/--port/--tls flags."""
    return cfg.model_dump()


# ---------- bool inversion ----------

@cli
def with_cache(use_cache: bool = True, name: str = "x"):
    """Default-True bool -> --no-use-cache."""
    return {"use_cache": use_cache, "name": name}


@cli
def with_verbose(verbose: bool = False):
    """Default-False bool -> --verbose."""
    return {"verbose": verbose}


# ---------- async ----------

@cli
async def run_async(n: int = 1):
    """Async fn — invoke_function should wrap with asyncio.run."""
    return {"n": n * 2}


# ---------- exceptions (for --notraceback / default paths) ----------

@cli
def raises():
    """Always raises — exercises error-printing paths."""
    raise ValueError("intentional error from fixture")


# ---------- grouped subcommand ----------

@cli("math")
def add(a: int, b: int):
    """Grouped subcommand: `nc-test-bin math add 2 3`."""
    return {"sum": a + b}


@cli("math")
def mul(a: int, b: int):
    """Grouped subcommand: `nc-test-bin math mul 4 5`."""
    return {"product": a * b}
