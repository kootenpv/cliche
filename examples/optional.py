from cliche import cli
from typing import Optional


@cli
def optional(a: Optional[str] = None):
    print(a)
