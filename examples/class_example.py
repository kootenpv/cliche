from cliche import cli, main


@cli
def a(a=1):
    pass


class A:
    def __init__(self, c, d=1):
        self.c = c
        self.d = d

    @cli
    def printer_a(self):
        print(self.c, self.d)


class B(A):
    @cli
    def printer_a(self, a, b=1):
        print(a, b, self.c, self.d)


if __name__ == "__main__":
    main()
