""" Base tests"""
import pytest
from cliche import cli, main


@cli
def basic():
    pass


def test_basic_int_add():
    expected = 3

    @cli
    def simple1(first: int, second: float):
        assert first + second == expected

    main(None, None, "simple1", "1", "2")


def test_basic_docs():
    expected = 3

    @cli
    def simple2(first: int, second: float):
        """ Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    main(None, None, "simple2", "1", "2")


def test_basic_default():
    expected = 3

    @cli
    def simple3(first: int, second: float = 2):
        """ Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    main(None, None, "simple3", "1")
