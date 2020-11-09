""" Base tests"""
import pytest
import cliche
from cliche import cli, main
from typing import List


def mainer(*args):
    cliche.main_called = []
    main(None, None, "simple", *args)


@cli
def basic():
    pass


def test_basic_int_add():
    expected = 3

    @cli
    def simple(first: int, second: float):
        assert first + second == expected

    mainer("1", "2")


def test_basic_docs():
    expected = 3

    @cli
    def simple(first: int, second: float):
        """Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    mainer("1", "2")


def test_kw():
    @cli
    def simple(first: int = 1):
        assert first == 3

    mainer("--first", "3")
    mainer("-f", "3")


def test_basic_default():
    expected = 3

    @cli
    def simple(first: int, second: float = 2):
        """Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    mainer("1")


def test_empty_list():
    @cli
    def simple(first: List[str]):
        """Explanation

        :param first: First
        """
        assert first == []

    mainer()


def test_list_int():
    @cli
    def simple(first: List[int]):
        """Explanation

        :param first: First
        """
        assert first == [1]

    mainer("1")
