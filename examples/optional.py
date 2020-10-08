from cliche import cli
from typing import Optional, List


@cli
def optional(a: Optional[str] = None):
    print(a)


@cli
def optional_list(a: Optional[List[int]] = None):
    print(a)
