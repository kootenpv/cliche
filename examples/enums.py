from cliche import cli, main, Enum


class Color(Enum):
    BLUE = 1
    RED = 2


@cli
def enums(color: Color):
    print(color)


if __name__ == "__main__":
    main()
