from cliche import cli


@cli
def exception_example():
    raise ValueError("No panic! This is a known error")
