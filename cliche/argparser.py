import argparse
import contextlib
import re
import sys
from enum import Enum

from cliche.docstring_to_help import parse_doc_params
from cliche.output import CleanArgumentParser
from cliche.type_utils import CONTAINER_MAPPING
from cliche.using_underscore import UNDERSCORE_DETECTED

pydantic_models = {}
bool_inverted = set()
container_fn_name_to_type = {}
class_init_lookup = {}  # for class functions

PYTHON_310_OR_HIGHER = sys.version_info >= (3, 10)
IS_VERBOSE = {"verbose", "verbosity"}


def get_desc_str(fn):
    doc_str = fn.__doc__ or ""
    desc = re.split("^ *Parameter|^ *Return|^ *Example|:param|\n\n", doc_str)[0].strip()
    desc = desc.replace("%", "%%")
    return desc[:1].upper() + desc[1:]


def add_command(subparsers, fn_name, fn):
    desc = get_desc_str(fn)
    name = fn_name if UNDERSCORE_DETECTED else fn_name.replace("_", "-")
    return subparsers.add_parser(name, help=desc, description=desc)


def is_pydantic(class_type):
    try:
        return "BaseModel" in [x.__name__ for x in class_type.__mro__]
    except AttributeError:
        return False


def add_group(parser_cmd, model, fn, var_name, abbrevs) -> None:
    kwargs = []
    pydantic_models[fn] = {}
    name = model.__name__ if UNDERSCORE_DETECTED else model.__name__.replace("_", "-")
    group = parser_cmd.add_argument_group(name)
    for field_name, field in model.__fields__.items():
        kwargs.append(field_name)
        default = field.default if field.default is not None else "--1"
        default_help = f"Default: {default} | " if default != "--1" else ""
        tp = field.type_
        container_type = tp in [list, set, tuple]
        with contextlib.suppress(AttributeError):
            container_type = CONTAINER_MAPPING.get(tp._name)
        if is_pydantic(tp):
            msg = "Cannot use nested pydantic just yet:" + f"property {var_name}.{field_name} of function {fn.__name__}"
            raise ValueError(msg)
        arg_desc = f"|{tp.__name__}| {default_help}"
        add_argument(group, tp, container_type, field_name, default, arg_desc, abbrevs)
    pydantic_models[fn][var_name] = (model, kwargs)


def add_argument(parser_cmd, tp, container_type, var_name, default, arg_desc, abbrevs) -> None:
    """Add an argument to the parser using the modern ArgumentBuilder approach."""
    from cliche.type_utils import ArgumentBuilder, TypeInfo

    # Create TypeInfo from the resolved type information
    type_info = TypeInfo(element_type=tp, type_name="", container_type=container_type)

    # Use ArgumentBuilder for clean argument construction
    (
        ArgumentBuilder(parser_cmd, var_name, abbrevs, UNDERSCORE_DETECTED)
        .with_type_info(type_info)
        .with_default(default)
        .with_description(arg_desc)
        .build()
    )


def get_var_name_and_default(fn):
    arg_count = fn.__code__.co_argcount
    defs = fn.__defaults__ or ()
    defaults = (("--1",) * arg_count + defs)[-arg_count:]
    for var_name, default in zip(fn.__code__.co_varnames, defaults, strict=False):
        if var_name in ["self", "cls"]:
            continue
        yield var_name, default


def get_fn_info(fn, var_name, default):
    """Extract type information for a function parameter."""
    from cliche.type_utils import TypeResolver

    default_type = type(default) if default != "--1" and default is not None else None
    annotation = fn.__annotations__.get(var_name, default_type or str)

    # Use the centralized TypeResolver
    resolver = TypeResolver(fn, class_init_lookup=class_init_lookup)
    type_info = resolver.resolve(annotation, default, default_type)

    return type_info.element_type, type_info.type_name, default, type_info.container_type


def add_arguments_to_command(cmd, fn, abbrevs=None):
    doc_str = fn.__doc__ or ""
    doc_params = parse_doc_params(doc_str)
    abbrevs = abbrevs or ["-h"]
    for var_name, default in get_var_name_and_default(fn):
        tp, tp_name, default, container_type = get_fn_info(fn, var_name, default)
        if is_pydantic(tp):
            # msg = f"Cannot use pydantic just yet, argument {var_name!r} (type {tp.__name__}) on cmd {cmd.prog!r}"
            # raise ValueError(msg)
            add_group(cmd, tp, fn, var_name, abbrevs)
            continue
        doc_text = doc_params.get(var_name, "")
        # changing the name to "no_X" in case the default is True for X, since we should set a flag to invert it
        # e.g. --sums becomes --no-sums
        if tp == bool and default is True:
            var_name = "no_" + var_name
            bool_inverted.add(var_name)
            default = False
            default_help = f"Default: {default} | " if default != "--1" else ""
            default = True
        else:
            if isinstance(default, Enum):
                default_fmt = default.name
            elif default == "--1":
                default_fmt = ""
            elif container_type and "Wrapper" in str(tp) and default:
                default_fmt = str(container_type([tp.Name(x) for x in default])).replace("'", "").replace('"', "")
            elif "Wrapper" in str(tp) and default:
                default_fmt = tp.Name(default)
            else:
                default_fmt = default
            default_help = f"Default: {default_fmt} | " if default != "--1" else ""
        arg_desc = f"|{tp_name}| {default_help}" + doc_text
        add_argument(cmd, tp, container_type, var_name, default, arg_desc, abbrevs)
    return abbrevs
