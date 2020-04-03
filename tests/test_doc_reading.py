import pytest
from cliche.docstring_to_help import parse_doc_params


def test_google_docstr():
    inp = """
      Args:
          msg (str): Human readable string describing the exception.
          code (:obj:`int`, optional): Error code.
      """
    assert parse_doc_params(inp) == {
        'msg': 'Human readable string describing the exception.',
        'code': 'Error code.',
    }


def test_sphinx_docstr():
    inp = """ Explanation

        :param first: First
        :param second: Second
        """
    assert parse_doc_params(inp) == {
        'first': 'First',
        'second': 'Second',
    }
