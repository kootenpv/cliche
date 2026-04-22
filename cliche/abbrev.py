"""
Abbreviation logic for CLI argument short flags.
Matches the behavior in cliche/type_utils.py ArgumentBuilder._get_var_names
"""


def get_short_flags(parameters: list[dict]) -> dict[str, str | None]:
    """
    Generate short flags for parameters based on cliche's algorithm.

    Args:
        parameters: List of parameter dicts with 'name' and optional 'default' keys

    Returns:
        Dict mapping parameter name to short flag (e.g., {'base': '-b', 'channel': '-c'})
        Value is None if no short flag available.
    """
    # Reserve -h for --help
    used_abbrevs = {'-h', '-H'}
    result = {}

    for param in parameters:
        name = param['name']
        has_default = 'default' in param

        # Skip positional arguments (no default) - they don't get short flags
        if not has_default:
            result[name] = None
            continue

        # Skip special parameters
        if name in ('self', 'cls') or param.get('is_args') or param.get('is_kwargs'):
            result[name] = None
            continue

        # Get first letter
        first_letter = name[0]
        short_lower = f"-{first_letter}"
        short_upper = f"-{first_letter.upper()}"

        if short_lower not in used_abbrevs:
            used_abbrevs.add(short_lower)
            result[name] = short_lower
        elif short_upper not in used_abbrevs:
            used_abbrevs.add(short_upper)
            result[name] = short_upper
        else:
            result[name] = None

    return result


def build_var_names(param_name: str, short_flag: str | None, has_default: bool) -> list[str]:
    """
    Build the list of argument names for argparse.

    Args:
        param_name: The parameter name (e.g., 'base')
        short_flag: The short flag (e.g., '-b') or None
        has_default: Whether the parameter has a default value

    Returns:
        List of argument names for argparse (e.g., ['-b', '--base'] or ['base'])
    """
    # Replace underscores with hyphens for CLI
    cli_name = param_name.replace('_', '-')

    if has_default:
        # Optional argument with --
        long_flag = f"--{cli_name}"
        if short_flag:
            return [short_flag, long_flag]
        return [long_flag]
    else:
        # Positional argument
        return [param_name]
