"""Tests for cliche.docstring — Sphinx, Google, and NumPy parsing."""
from __future__ import annotations

from cliche.docstring import (
    detect_style,
    get_description_without_params,
    parse_param_descriptions,
)


# --------- Sphinx ---------

class TestSphinx:
    def test_basic(self):
        doc = """Do something.

        :param x: the x value
        :param y: the y value
        :return: nothing
        """
        assert parse_param_descriptions(doc) == {'x': 'the x value', 'y': 'the y value'}

    def test_multiline_description(self):
        doc = """Do it.

        :param x: spans
            multiple
            lines
        """
        assert parse_param_descriptions(doc) == {'x': 'spans multiple lines'}

    def test_detect(self):
        assert detect_style(":param x: foo") == 'sphinx'

    def test_description(self):
        doc = "Short summary.\n\n:param x: details"
        assert get_description_without_params(doc) == 'Short summary.'


# --------- Google ---------

class TestGoogle:
    def test_args_block(self):
        doc = """Do something useful.

        Args:
            msg (str): Human readable string.
            code (int, optional): Error code.
                Continuation of code's description.
            flag: a simple bool

        Returns:
            bool: whether it worked
        """
        assert parse_param_descriptions(doc) == {
            'msg': 'Human readable string.',
            'code': "Error code. Continuation of code's description.",
            'flag': 'a simple bool',
        }

    def test_arguments_header_alias(self):
        doc = """Do it.

        Arguments:
            x: hello
        """
        assert parse_param_descriptions(doc) == {'x': 'hello'}

    def test_parameters_header_alias(self):
        doc = """Do it.

        Parameters:
            x: hello
        """
        assert parse_param_descriptions(doc) == {'x': 'hello'}

    def test_type_with_nested_colons(self):
        # `:obj:` style annotation inside the parens shouldn't break parsing.
        doc = """Do it.

        Args:
            x (:obj:`int`, optional): the x value
        """
        assert parse_param_descriptions(doc) == {'x': 'the x value'}

    def test_block_ends_at_returns(self):
        doc = """Do it.

        Args:
            x: the x

        Returns:
            int: the result
        """
        assert parse_param_descriptions(doc) == {'x': 'the x'}

    def test_detect(self):
        assert detect_style("Args:\n    x: foo") == 'google'

    def test_description(self):
        doc = "Short summary.\n\nArgs:\n    x: details"
        assert get_description_without_params(doc) == 'Short summary.'


# --------- NumPy ---------

class TestNumPy:
    def test_parameters_block(self):
        doc = """Compute a thing.

        Parameters
        ----------
        x : int
            Description of x.
        y : str, optional
            Description of y.
            Can span multiple lines.

        Returns
        -------
        bool
        """
        assert parse_param_descriptions(doc) == {
            'x': 'Description of x.',
            'y': 'Description of y. Can span multiple lines.',
        }

    def test_param_without_type(self):
        doc = """Do it.

        Parameters
        ----------
        x
            just a name
        """
        assert parse_param_descriptions(doc) == {'x': 'just a name'}

    def test_block_ends_at_returns(self):
        doc = """Do it.

        Parameters
        ----------
        x : int
            the x

        Returns
        -------
        bool
        """
        assert parse_param_descriptions(doc) == {'x': 'the x'}

    def test_detect_with_returns_only(self):
        # No Parameters block, but the Returns/dashes pattern is still numpy.
        doc = "Do it.\n\nReturns\n-------\nbool\n"
        assert detect_style(doc) == 'numpy'

    def test_detect_with_params(self):
        doc = "Parameters\n----------\nx : int\n    foo\n"
        assert detect_style(doc) == 'numpy'

    def test_description(self):
        doc = "Short summary.\n\nParameters\n----------\nx : int\n    details\n"
        assert get_description_without_params(doc) == 'Short summary.'


# --------- Freeform / missing ---------

class TestEdgeCases:
    def test_empty(self):
        assert parse_param_descriptions('') == {}
        assert parse_param_descriptions(None) == {}
        assert get_description_without_params('') == ''
        assert get_description_without_params(None) == ''

    def test_freeform_classification(self):
        assert detect_style("Just a sentence.") == 'freeform'

    def test_missing_classification(self):
        assert detect_style('') == 'missing'
        assert detect_style(None) == 'missing'

    def test_dashes_in_freeform_doc(self):
        # `--foo` in body shouldn't trip the numpy detector or parser.
        doc = "Run a thing. Use --foo for the foo option."
        assert parse_param_descriptions(doc) == {}
        assert detect_style(doc) == 'freeform'

    def test_mixed_styles_merge(self):
        doc = """Do it.

        Args:
            a: from google
        :param b: from sphinx
        """
        assert parse_param_descriptions(doc) == {'a': 'from google', 'b': 'from sphinx'}
