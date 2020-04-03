from cliche import cli

@cli
def add(a: int, b: int):
    print(a + b)


@cli
def sum_or_multiply(a_number: int, b_number: int = 10, sums: bool = False):
    """ Sums or multiplies a and b

    :param a_number: the first one
    :param b_number: This parameter seems to be
    :param sums: Sums when true, otherwise multiply
    """
    if sums:
        print(a_number + b_number)
    else:
        print(a_number * b_number)
