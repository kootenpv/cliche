"""Parse docstrings to extract parameter descriptions.

Supports Sphinx/reST (`:param x:`), Google (`Args:` block), and NumPy
(`Parameters\\n----`) styles. All parsers share an early-out substring check
so this stays cheap on the @cli hot path.
"""
import re

_SPHINX_PARAM_RE = re.compile(
    r':param\s+(\w+)\s*:\s*(.+?)(?=:param|:return|:raises|:type|\Z)', re.DOTALL
)

_GOOGLE_HEADERS = ('Args:', 'Arguments:', 'Parameters:')
_GOOGLE_END_HEADERS = frozenset((
    'Returns:', 'Return:', 'Yields:', 'Yield:', 'Raises:', 'Raise:',
    'Examples:', 'Example:', 'Note:', 'Notes:', 'Warning:', 'Warnings:',
    'See Also:', 'References:', 'Todo:', 'Attributes:', 'Methods:',
))
_GOOGLE_HEADERS_SET = frozenset(_GOOGLE_HEADERS)
_GOOGLE_PARAM_RE = re.compile(r'(\w+)[ \t]*(?:\([^)]*\))?[ \t]*:[ \t]*(.*)$')

_NUMPY_SECTION_RE = re.compile(r'^[ \t]*(?:Parameters|Other Parameters)[ \t]*$')
_NUMPY_DASH_RE = re.compile(r'^[ \t]*-+[ \t]*$')
# Any numpy-style section header (broader than _NUMPY_SECTION_RE), used by
# detect_style to recognize numpydoc even when the Parameters block is absent.
_NUMPY_ANY_HEADER_RE = re.compile(
    r'^[ \t]*(?:Parameters|Other Parameters|Returns|Yields|Raises|Warns|'
    r'Notes|Examples|See Also|References|Attributes|Methods)[ \t]*\n[ \t]*-+[ \t]*$',
    re.MULTILINE,
)
# `name` or `name : type` — nothing else on the line.
_NUMPY_PARAM_RE = re.compile(r'(\w+)(?:[ \t]*:[ \t]*.+)?$')

# One pass to find where structured content begins. Sphinx markers can occur
# anywhere on a line; Google/NumPy section headers are line-leading (allow
# leading whitespace for indented docstrings).
_DESC_END_RE = re.compile(
    r':param|:return|:raises|:type|:rtype'
    r'|\n[ \t]*(?:Args|Arguments|Parameters|Returns|Return|Raises|Yields|'
    r'Examples|Example|Note|Notes|Attributes):'
    r'|\n[ \t]*(?:Parameters|Returns|Raises|Yields|Examples|Notes)[ \t]*\n[ \t]*-'
)


def _normalize(parts: list[str]) -> str:
    return ' '.join(' '.join(p for p in parts if p).split())


def _parse_google(lines: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].strip() not in _GOOGLE_HEADERS_SET:
            i += 1
            continue
        i += 1
        param_indent = None
        current_name = None
        current_parts: list[str] = []
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            if stripped in _GOOGLE_END_HEADERS or stripped in _GOOGLE_HEADERS_SET:
                break
            indent = len(line) - len(line.lstrip())
            if param_indent is None:
                param_indent = indent
            if indent < param_indent:
                break
            if indent == param_indent:
                m = _GOOGLE_PARAM_RE.match(line, indent)
                if not m:
                    break
                if current_name is not None:
                    params[current_name] = _normalize(current_parts)
                current_name = m.group(1)
                current_parts = [m.group(2).strip()]
            elif current_name is not None:
                current_parts.append(stripped)
            i += 1
        if current_name is not None:
            params[current_name] = _normalize(current_parts)
    return params


def _parse_numpy(lines: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    n = len(lines)
    i = 0
    while i < n - 1:
        if not _NUMPY_SECTION_RE.match(lines[i]) or not _NUMPY_DASH_RE.match(lines[i + 1]):
            i += 1
            continue
        i += 2
        param_indent = None
        current_name = None
        current_parts: list[str] = []
        while i < n:
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            # A new section starts when this line is followed by dashes.
            if i + 1 < n and _NUMPY_DASH_RE.match(lines[i + 1]):
                break
            indent = len(line) - len(line.lstrip())
            if param_indent is None:
                param_indent = indent
            if indent < param_indent:
                break
            if indent == param_indent:
                m = _NUMPY_PARAM_RE.match(line, indent)
                if not m:
                    break
                if current_name is not None:
                    params[current_name] = _normalize(current_parts)
                current_name = m.group(1)
                current_parts = []
            elif current_name is not None:
                current_parts.append(stripped)
            i += 1
        if current_name is not None:
            params[current_name] = _normalize(current_parts)
    return params


def parse_param_descriptions(docstring: str | None) -> dict[str, str]:
    """Extract parameter descriptions from a Sphinx, Google, or NumPy docstring.

    Returns a dict mapping parameter names to their descriptions. When multiple
    formats are present, later parsers' values win (Sphinx → Google → NumPy).
    """
    if not docstring:
        return {}

    params: dict[str, str] = {}

    if ':param' in docstring:
        for match in _SPHINX_PARAM_RE.finditer(docstring):
            params[match.group(1)] = ' '.join(match.group(2).split())

    has_google = any(h in docstring for h in _GOOGLE_HEADERS)
    # `---` rather than `\n---` so we still match indented docstrings (raw
    # from AST), where every dash line is preceded by leading whitespace.
    has_numpy = '---' in docstring
    if has_google or has_numpy:
        lines = docstring.splitlines()
        if has_google:
            params.update(_parse_google(lines))
        if has_numpy:
            params.update(_parse_numpy(lines))

    return params


def detect_style(docstring: str | None) -> str:
    """Classify a docstring as 'sphinx', 'google', 'numpy', 'freeform', or 'missing'.

    A docstring with no recognized markers is 'freeform'; an empty/None
    docstring is 'missing'. When multiple style markers coexist, precedence is
    sphinx > google > numpy.
    """
    if not docstring:
        return 'missing'
    if ':param' in docstring:
        return 'sphinx'
    if any(h in docstring for h in _GOOGLE_HEADERS):
        return 'google'
    if '---' in docstring and _NUMPY_ANY_HEADER_RE.search(docstring):
        return 'numpy'
    return 'freeform'


def get_description_without_params(docstring: str | None) -> str:
    """Get the docstring description without param/section blocks.

    Returns the first paragraph before any Sphinx, Google, or NumPy section.
    """
    if not docstring:
        return ''

    m = _DESC_END_RE.search(docstring)
    desc = docstring[:m.start()].strip() if m else docstring.strip()
    if '\n\n' in desc:
        desc = desc.split('\n\n')[0]
    return ' '.join(desc.split())
