from cliche import List, cli


@cli
def a(z=[1, 1]):
    """ Test default list default value """
    print(z)


@cli
def b(z: List[int] = [1, 1]):
    """ Test list typing and list default value """
    print(z)


@cli
def c(z: List[int]):
    """ Test list typing """
    print(z)


@cli
def d(z: List[str]):
    """ Test list typing """
    print(z)


# slightly broken
# (feb2018) pascal@archbook:/home/.../cliche/examples$ cliche list_of_items.py f 1 2
# [1, 2] None


@cli
def e(a: int = 1, z: List[int] = None):
    """ Test list typing with None as default """
    print(a, z)


@cli
def f(a: int, z: List[int] = None):
    """ Test pos argument and list typing with None as default """
    print(a, z)
