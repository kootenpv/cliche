import argparse
import contextlib
import re
import sys
from enum import Enum
from typing import Union

from cliche.choice import DictAction, EnumAction, ProtoEnumAction
from cliche.docstring_to_help import parse_doc_params
from cliche.using_underscore import UNDERSCORE_DETECTED

pydantic_models = {}
bool_inverted = set()
CONTAINER_MAPPING = {"List": list, "Iterable": list, "Set": set, "Tuple": tuple}
CONTAINER_MAPPING.update({k.lower(): v for k, v in CONTAINER_MAPPING.items()})
container_fn_name_to_type = {}
class_init_lookup = {}  # for class functions

PYTHON_310_OR_HIGHER = sys.version_info >= (3, 10)
IS_VERBOSE = {"verbose", "verbosity"}


class ColoredHelpOnErrorParser(argparse.ArgumentParser):
    # color_dict is a class attribute, here we avoid compatibility
    # issues by attempting to override the __init__ method
    # RED : Error, GREEN : Okay, YELLOW : Warning, Blue: Help/Info
    color_dict = {"RED": "1;31", "GREEN": "1;32", "YELLOW": "1;33", "BLUE": "1;36"}
    # only when called with `cliche`, not `python`
    module_name = False

    def print_help(self, file=None) -> None:
        if file is None:
            file = sys.stdout
        self._print_message(self.format_help(), file, self.color_dict["BLUE"])

    @staticmethod
    def make_subgroups(message):
        ind = message.find("SUBCOMMAND -> ")
        if ind == -1:
            return message
        z = message[:ind].rfind("\n")
        return message[:z] + "\n\nSUBCOMMANDS:" + message[z:].replace("SUBCOMMAND -> ", "")

    def _print_message(self, message, file=None, color=None) -> None:
        if message:
            message = message[0].upper() + message[1:]
            if self.module_name:
                repl = " ".join(["cliche " + self.module_name] + self.prog.split()[1:])
                message = message.replace(self.prog, repl)
            if file is None:
                file = sys.stderr
            # Print messages in bold, colored text if color is given.
            if color is None:
                file.write(message)
            else:
                # \x1b[ is the ANSI Control Sequence Introducer (CSI)
                if hasattr(self, "sub_command"):
                    message = message.replace(self.prog, self.sub_command)
                if color == self.color_dict["BLUE"]:
                    message = message.strip()
                    if len(self.prog.split()) > 1:
                        message = message.replace("positional arguments:", "POSITIONAL ARGUMENTS:")
                    else:
                        # check if first is a positional arg or actual command
                        ms = re.findall("positional arguments:.  {([^}]+)..", message, flags=re.DOTALL)
                        if ms:
                            ms = ms[0]
                            first_start = message.index("positional arguments")
                            start = first_start + message[first_start:].index(ms) + len(ms)
                            end = message.index("options:" if PYTHON_310_OR_HIGHER else "optional ")
                            if all(x in message[start:end] for x in ms.split(",")):
                                # remove the line that shows the possibl commands, like e.g.
                                # {badd, print-item, add}
                                message = re.sub(
                                    "positional arguments:.  {[^ }]+..",
                                    "COMMANDS:\n",
                                    message,
                                    flags=re.DOTALL,
                                )
                        message = message.replace("positional arguments:", "POSITIONAL ARGUMENTS:")

                    message = self.make_subgroups(message)
                    message = message.replace("options" if PYTHON_310_OR_HIGHER else "optional arguments", "OPTIONS:")
                    lines = message.split("\n")
                    inds = 1
                    for i in range(1, len(lines)):
                        if re.search("^[A-Z]", lines[i]):
                            break
                        if re.search(" +([{]|[.][.][.])", lines[i]):
                            lines[i] = None
                        else:
                            inds += 1
                    lines = [
                        "\x1b[" + color + "m" + "\n".join([x for x in lines[:inds] if x is not None]) + "\x1b[0m"
                    ] + lines[inds:]
                    message = "\n".join([x for x in lines if x is not None])
                    message = re.sub(
                        "Default:.[^|]+",
                        "\x1b[" + color + "m" + r"\g<0>" + "\x1b[0m",
                        message,
                        flags=re.DOTALL,
                    )
                    reg = r"(\n *-[a-zA-Z]) (.+, --)( \[[A-Z0-9. ]+\])?"
                    message = re.sub(reg, "\x1b[" + color + "m" + r"\g<1>" + "\x1b[0m, --", message)
                    reg = r", (--[^ ]+)"
                    message = re.sub(reg, ", " + "\x1b[" + color + "m" + r"\g<1> " + "\x1b[0m", message)

                    for reg in [
                        "\n  -h, --help",
                        "\n  {[^}]+}",
                        "\n +--[^ ]+",
                        "\n  {1,6}[a-z0-9A-Z_-]+",
                    ]:
                        message = re.sub(reg, "\x1b[" + color + "m" + r"\g<0>" + "\x1b[0m", message)
                    file.write(message + "\n")
                else:
                    file.write("\x1b[" + color + "m" + message.strip() + "\x1b[0m\n")

    def exit(self, status=0, message=None) -> None:
        if message:
            self._print_message(message, sys.stderr, self.color_dict["RED"])
        sys.exit(status)

    def error(self, message) -> None:
        # otherwise it prints generic help but it should print the specific help of the subcommand
        if "unrecognized arguments" in message:
            multiple_args = message.count(" ") > 2
            option_str = "Unknown option" if PYTHON_310_OR_HIGHER else "Unknown optional argument"

            type_arg_msg = option_str if "-" in message else "Extra positional argument"
            if multiple_args:
                type_arg_msg += "(s)"
            message = message.replace("unrecognized arguments", type_arg_msg)
            with contextlib.suppress(SystemExit):
                self.parse_args(sys.argv[1:-1] + ["--help"])
        else:
            self.print_help(sys.stderr)
        self.exit(2, message)


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


def get_var_names(var_name, abbrevs):
    # adds shortenings when possible
    if var_name.startswith("--"):
        short = "-" + var_name[2]
        # don't add shortening for inverted bools
        if var_name.startswith(("--no-", "--no_")):
            var_names = [var_name]
        elif short not in abbrevs:
            abbrevs.append(short)
            var_names = [short, var_name]
        elif short.upper() not in abbrevs:
            abbrevs.append(short.upper())
            var_names = [short.upper(), var_name]
        else:
            var_names = [var_name]
    else:
        var_names = [var_name]
    return var_names


def protobuf_tp_converter(tp):
    def inner(x):
        return tp.Value(x)

    return inner


def add_argument(parser_cmd, tp, container_type, var_name, default, arg_desc, abbrevs) -> None:
    kwargs = {}
    var_name = var_name if UNDERSCORE_DETECTED else var_name.replace("_", "-")
    arg_desc = arg_desc.replace("%", "%%")
    nargs = None
    if container_type:
        with contextlib.suppress(AttributeError):
            tp = tp.__args__[0]
        nargs = "*"
    if tp is bool:
        action = "store_true" if not default else "store_false"
        var_names = get_var_names("--" + var_name, abbrevs)
        parser_cmd.add_argument(*var_names, action=action, help=arg_desc)
        return
    try:
        if isinstance(tp, tuple):
            kwargs["action"] = DictAction
        elif "EnumTypeWrapper" in str(tp):
            kwargs["action"] = ProtoEnumAction
        elif issubclass(tp, Enum):
            kwargs["action"] = EnumAction
            # txt = "|".join(tp.__members__)
            # if len(txt) > 77:
            #     txt = txt[:77] + "... "
            # kwargs["metavar"] = txt
    except TypeError:
        pass
    if default != "--1":
        var_name = "--" + var_name
    var_names = get_var_names(var_name, abbrevs)
    if nargs == "*" and default == "--1":
        default = container_type()
    if container_type:
        fn = parser_cmd.prog.split()[-1]
        container_fn_name_to_type[(fn, var_name)] = container_type
    parser_cmd.add_argument(*var_names, type=tp, nargs=nargs, default=default, help=arg_desc, **kwargs)


def get_var_name_and_default(fn):
    arg_count = fn.__code__.co_argcount
    defs = fn.__defaults__ or ()
    defaults = (("--1",) * arg_count + defs)[-arg_count:]
    for var_name, default in zip(fn.__code__.co_varnames, defaults, strict=False):
        if var_name in ["self", "cls"]:
            continue
        yield var_name, default


def base_lookup(fn, tp, sans):
    tp_ending = tuple(tp.split("."))
    sans_ending = tuple(sans.split("."))
    if fn.__qualname__ in class_init_lookup:
        fn.lookup = class_init_lookup[fn.__qualname__]
    if tp_ending in fn.lookup:
        tp_name = tp
        tp = fn.lookup[tp_ending]
    elif sans_ending in fn.lookup:
        tp_name = sans
        tp = fn.lookup[sans_ending]
    else:
        tp_name = sans
        tp = __builtins__.get(sans, sans)
    return tp, tp_name


def optional_pipe_lookup(fn, tp) -> None:
    if tp.startswith("None | "):
        sans_optional = tp[7:]
    elif tp.endswith("| None"):
        sans_optional = tp[:-7]
    else:
        msg = f"Optional confusion: {fn} {tp}"
        raise Exception(msg)
    return base_lookup(fn, tp, sans_optional)


def optional_lookup(fn, tp):
    if isinstance(tp, str) and "|" in tp:
        return optional_pipe_lookup(fn, tp)
    if type(tp).__name__ == "UnionType":
        assert len(tp.__args__) == 2, "Union may at most have 2 types"
        assert type(None) in tp.__args__, "Union must have one None"
        a, b = tp.__args__
        if type(a) == type(None):
            b, a = a, b
        return base_lookup(fn, a.__name__, a.__name__)
    sans_optional = tp.replace("Optional[", "")
    if tp != sans_optional:  # strip ]
        sans_optional = sans_optional[:-1]
    return base_lookup(fn, tp, sans_optional)


def container_lookup(fn, tp, container_name):
    sans_container = tp.replace(f"{container_name}[", "")
    if tp != sans_container:  # strip ]
        sans_container = sans_container[:-1].split(",")[0].strip()
    return base_lookup(fn, tp, sans_container)


def get_fn_info(fn, var_name, default):
    default_type = type(default) if default != "--1" and default is not None else None
    tp = fn.__annotations__.get(var_name, default_type or str)
    # List, Iterable, Set, Tuple
    container_type = False
    found_result = True
    tp_name = "bugggg"
    found_result = default_type in [list, set, tuple, dict]
    if default_type in [list, set, tuple, dict]:
        container_type = default_type
        if "typing" not in str(tp):
            tp_args = ", ".join({type(x).__name__ for x in default}) or "str"
            tp_name = "1 or more of: " + tp_args
        else:
            tp_args = ", ".join(x.__name__ for x in tp.__args__)
            tp_name = "1 or more of: " + tp_args
        if hasattr(tp, "__args__"):
            tp = tp.__args__[0]
        elif len({type(x) for x in default}) > 1:
            tp = None
        elif default:
            if container_type is dict:
                tp = (type(next(iter(default))), type(next(iter(default.values()))))
            elif container_lookup(fn, tp, "tuple")[0] != tp:
                # tp = container_lookup(fn, tp, "tuple")[0]
                found_result = False
            elif container_lookup(fn, tp, "list")[0] != tp:
                # tp = container_lookup(fn, tp, "list")[0]
                found_result = False
            else:
                tp = type(next(iter(default)))
        else:
            found_result = False
    if not found_result:
        try:
            if tp.__origin__ == Union:
                tp = tp.__args__[0]
            container_type = CONTAINER_MAPPING.get(tp._name)
        except AttributeError:
            pass
        if "dict[" in str(tp).lower():
            if str(tp).lower().startswith("optional"):
                tp = tp[9:-1]
            aa, bb = tp[5:-1].split(", ")
            tp = (base_lookup(fn, tp, aa), base_lookup(fn, tp, bb))
            container_type = dict
        else:
            for container_name, container in CONTAINER_MAPPING.items():
                if isinstance(tp, str) and container_name in tp:
                    tp, tp_name = container_lookup(fn, tp, container_name)
                    container_type = container
                    tp_name = "0 or more of: " + tp_name
                    break
            else:
                if container_type:
                    if not hasattr(tp, "__args__"):
                        tp_arg = "str"
                        tp = str
                    else:
                        if tp.__args__ and "Union" in str(tp.__args__[0]):
                            # cannot cast
                            tp_arg = "str"
                        elif tp.__args__:
                            tp_arg = tp.__args__[0].__name__
                        else:
                            tp_arg = "str"
                        tp = tp.__args__[0]
                    tp_name = "0 or more of: " + tp_arg
                elif tp == "str":
                    tp = str
                    tp_name = "str"
                elif isinstance(tp, str) and base_lookup(fn, tp, "")[0]:
                    tp, tp_name = base_lookup(fn, tp, "")
                elif tp.__class__.__name__ == "EnumTypeWrapper":
                    tp_name = tp._enum_type.name
                elif hasattr(tp, "__name__"):
                    tp_name = tp.__name__
                else:
                    tp, tp_name = optional_lookup(fn, tp)
    return tp, tp_name, default, container_type


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
