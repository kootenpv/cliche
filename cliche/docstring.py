"""Parse docstrings to extract parameter descriptions."""
import re

# Regex to match :param name: description lines
_PARAM_RE = re.compile(r':param\s+(\w+)\s*:\s*(.+?)(?=:param|:return|:raises|:type|\Z)', re.DOTALL)


def parse_param_descriptions(docstring: str) -> dict[str, str]:
    """Extract parameter descriptions from a docstring.

    Parses lines like:
        :param name: Description of the parameter

    Returns:
        Dict mapping parameter names to their descriptions.
    """
    if not docstring:
        return {}

    params = {}
    for match in _PARAM_RE.finditer(docstring):
        name = match.group(1)
        desc = match.group(2).strip()
        # Clean up multiline descriptions - join lines, normalize whitespace
        desc = ' '.join(desc.split())
        params[name] = desc

    return params


def get_description_without_params(docstring: str) -> str:
    """Get the docstring description without :param lines.

    Returns the first paragraph before any :param, :return, etc.
    """
    if not docstring:
        return ''

    # Find where params/returns start
    markers = [':param', ':return', ':raises', ':type', ':rtype']
    first_marker = len(docstring)
    for marker in markers:
        idx = docstring.find(marker)
        if idx != -1 and idx < first_marker:
            first_marker = idx

    desc = docstring[:first_marker].strip()
    # Take first paragraph only
    if '\n\n' in desc:
        desc = desc.split('\n\n')[0]
    return ' '.join(desc.split())
