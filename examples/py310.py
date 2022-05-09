from cliche import cli


@cli
def py310_optional(a: int | None):
    print(a)
