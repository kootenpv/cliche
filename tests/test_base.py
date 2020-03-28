""" Base tests"""
import pytest
from cliche import cli, main


def test_basic_int_add():
    expected = 3

    @cli
    def simple1(first: int, second: float):
        assert first + second == expected

    main(None, "simple1", "1", "2")


def test_basic_docs():
    expected = 3

    @cli
    def simple2(first: int, second: float):
        """ Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    main(None, "simple2", "1", "2")


def test_basic_default():
    expected = 3

    @cli
    def simple3(first: int, second: float = 2):
        """ Explanation

        :param first: First
        :param second: Second
        """
        assert first + second == expected

    main(None, "simple3", "1")


def test_basic_help():
    @cli
    def simple4(first: int, second: float):
        pass

    with pytest.raises(SystemExit) as pytest_wrapped_e:
        main(None, "simple4", "--help")

    assert pytest_wrapped_e.type == SystemExit
    assert pytest_wrapped_e.value.code == 0
