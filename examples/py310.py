from cliche import cli
from typing import Optional


@cli
def py310_optional(a: int | None = None):
    print(a)
