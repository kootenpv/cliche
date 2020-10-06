from cliche import cli, main, Choice


# class Color(Enum):
#     BLUE = 1
#     RED = 2


@cli
def choices(color: Choice("red", "blue") = "red"):
    print(color)


if __name__ == "__main__":
    main()
