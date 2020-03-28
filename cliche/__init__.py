import re
import sys
from gettext import gettext
import argparse
from inspect import signature, currentframe
import traceback
from typing import List, Iterable, Set, Tuple

fn_registry = {}
pydantic_models = {}


class Choice:
    def __init__(self, *choices, none_allowed=False, choice_type=None):
        self.choices = choices
        if choice_type is not None:
            self.choices = [choice_type(x) for x in choices]
        if none_allowed and none_allowed not in choice_type:
            self.choices.append(None)

    def __repr__(self):
        inner = ", ".join(f"{x!r}" for x in self.choices)
        return f"Choice({inner})"


class ColoredHelpOnErrorParser(argparse.ArgumentParser):

    # color_dict is a class attribute, here we avoid compatibility
    # issues by attempting to override the __init__ method
    # RED : Error, GREEN : Okay, YELLOW : Warning, Blue: Help/Info
    color_dict = {'RED': '1;31', 'GREEN': '1;32', 'YELLOW': '1;33', 'BLUE': '1;36'}
    # only when called with `cliche`, not `python`
    module_name = False

    def print_usage(self, file=None):
        if file is None:
            file = sys.stdout
        self._print_message(
            self.format_usage()[0].upper() + self.format_usage()[1:],
            file,
            self.color_dict['YELLOW'],
        )

    def print_help(self, file=None):
        if file is None:
            file = sys.stdout
        self._print_message(
            self.format_help()[0].upper() + self.format_help()[1:], file, self.color_dict['BLUE']
        )

    def _print_message(self, message, file=None, color=None):
        if message:
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
                if color == self.color_dict["BLUE"]:
                    message = message.strip()
                    message = message.replace("positional arguments:", "POSITIONAL ARGUMENTS:")
                    message = message.replace("optional arguments:", "OPTIONAL ARGUMENTS:")
                    message = re.sub(
                        "Usage: cliche.+", "\x1b[" + color + "m" + r"\g<0>" + "\x1b[0m", message
                    )
                    message = re.sub(
                        "Default: [^|]+", "\x1b[" + color + "m" + r"\g<0>" + "\x1b[0m", message
                    )

                    for reg in [
                        "\n  -h, --help",
                        "\n +--[^ ]+",
                        "\n  ? ? ? ? ? ?[a-z0-9A-Z_-]+",
                    ]:
                        message = re.sub(reg, "\x1b[" + color + "m" + r"\g<0>" + "\x1b[0m", message)
                    file.write(message + "\n")
                else:
                    file.write('\x1b[' + color + 'm' + message.strip() + '\x1b[0m\n')

    def exit(self, status=0, message=None):
        if message:
            self._print_message(message, sys.stderr, self.color_dict['RED'])
        sys.exit(status)

    def error(self, message):
        message = message.replace(
            "unrecognized arguments", "unrecognized (too many positional) arguments"
        )
        self.print_help(sys.stderr)
        # self.print_usage(sys.stderr)
        args = {'prog': self.prog, 'message': message}
        self.exit(2, gettext('%(prog)s: Error: %(message)s\n') % args)


#     def error(self, message):
#         # TODO: it actually now prints generic help but it should print the specific help of the subcommand
#         # print(sys.modules[cli.__module__].__doc__)

#         message = message.replace(
#             "unrecognized arguments", "unrecognized (too many positional) arguments"
#         )
#         warn(f"error: {message}")
#         sys.exit(2)


def warn(x):
    sys.stderr.write("\033[31m" + x + "\033[0m\n")
    sys.stderr.flush()


def cli(fn):
    def decorated_fn(*args, **kwargs):
        show_traceback = False
        if "traceback" in kwargs:
            show_traceback = kwargs.pop("traceback")
        try:
            kwargs = {k.replace("-", "_"): v for k, v in kwargs.items()}
            if fn in pydantic_models:
                for var_name in pydantic_models[fn]:
                    model, model_args = pydantic_models[fn][var_name]
                    for m in model_args:
                        kwargs.pop(m)
                    kwargs[var_name] = model(**kwargs)
            fn(*args, **kwargs)
        except Exception as e:
            print(f"Fault while calling {fn.__name__}{signature(fn)} with the above arguments")
            if show_traceback:
                raise
            else:
                warn(traceback.format_exception_only(type(e), e)[-1].strip())
                sys.exit(1)

    fn_registry[fn.__name__] = (decorated_fn, fn)

    return fn


GOOGLE_DOC_RE = re.compile(r"^[^ ] +:|^[^ ]+ +\([^\)]+\):")


def parse_google_param_descriptions(doc):
    stack = {}
    results = {}
    args_seen = False
    for line in doc.split("\n"):
        line = line.strip()
        if line == "Returns:":
            break
        if not args_seen:
            if line == "Args:":
                args_seen = True
        elif GOOGLE_DOC_RE.search(line):
            if stack:
                results[stack["fn"]] = "\n".join(stack["lines"])
            fn_name = line.split(":")[0].split()[0]
            stack = {"fn": fn_name, "lines": [GOOGLE_DOC_RE.sub("", line).strip()]}
        elif stack and line.strip():
            stack["lines"].append(line.strip())
    if stack:
        results[stack["fn"]] = "\n".join(stack["lines"])
    return results


def parse_sphinx_param_descriptions(doc):
    stack = {}
    results = {}
    for line in doc.split("\n"):
        line = line.strip()
        if line.startswith(":"):
            if stack:
                results[stack["fn"]] = "\n".join(stack["lines"])
            stack = {}
            if line.startswith(":param"):
                fn_name = line.split(":")[1].split()[-1]
                stack = {"fn": fn_name, "lines": [line.split(":", 2)[2].strip()]}
        elif stack and line.strip():
            stack["lines"].append(line.strip())
    if stack:
        results[stack["fn"]] = "\n".join(stack["lines"])
    return results


def add_command(subparsers, fn_name, fn):
    doc_str = fn.__doc__ or ""
    desc = re.split("^ *Parameter|^ *Return|^ *Example|:param|\n\n", doc_str)[0].strip()
    cmd = subparsers.add_parser(fn_name.replace("_", "-"), help=desc, description=desc)
    return cmd


def is_pydantic(class_type):
    try:
        return "BaseModel" in [x.__name__ for x in class_type.__mro__]
    except AttributeError:
        return False


def add_group(parser_cmd, model, fn, var_name):
    kwargs = []
    pydantic_models[fn] = {}
    group = parser_cmd.add_argument_group(model.__name__.replace("_", "-"))
    for field_name, field in model.__fields__.items():
        kwargs.append(field_name)
        default = field.default if field.default is not None else "--1"
        default_help = f"Default: {default} | " if default != "--1" else ""
        tp = field.type_
        container_type = tp in [list, set, tuple]
        try:
            container_type = tp._name in ["List", "Iterable", "Set", "Tuple"]
        except AttributeError:
            pass
        if is_pydantic(tp):
            msg = (
                f"Cannot use nested pydantic just yet:"
                + f"property {var_name}.{field_name} of function {fn.__name__}"
            )
            raise ValueError(msg)
        arg_desc = f"|{tp.__name__}| {default_help}"
        add_argument(group, tp, container_type, field_name, default, arg_desc)
    pydantic_models[fn][var_name] = (model, kwargs)


def add_argument(parser_cmd, tp, container_type, var_name, default, arg_desc):
    action = "store"
    var_name = var_name.replace("_", "-")
    if tp is bool:
        action = "store_true" if not default else "store_false"
        parser_cmd.add_argument("--" + var_name, action=action, help=arg_desc)
        return
    nargs = None
    if default != "--1":
        var_name = "--" + var_name
    if container_type:
        try:
            tp = tp.__args__[0]
            nargs = "+"
        except AttributeError:
            pass
    parser_cmd.add_argument(var_name, type=tp, nargs=nargs, default=default, help=arg_desc)


def parse_doc_params(doc_str):
    doc_params = parse_sphinx_param_descriptions(doc_str)
    doc_params.update(parse_google_param_descriptions(doc_str))
    return doc_params


def add_arguments_to_command(cmd, fn):
    doc_str = fn.__doc__ or ""
    arg_count = fn.__code__.co_argcount
    defs = fn.__defaults__ or tuple()
    defaults = (("--1",) * arg_count + defs)[-arg_count:]
    doc_params = parse_doc_params(doc_str)
    for var_name, default in zip(fn.__code__.co_varnames, defaults):
        default_help = f"Default: {default} | " if default != "--1" else ""
        default_type = type(default) if default != "--1" and default is not None else None
        tp = fn.__annotations__.get(var_name, default_type or str)
        # List, Iterable, Set, Tuple
        container_type = False
        if default_type in [list, set, tuple]:
            for value in default:
                break
            else:
                value = ""
            tp = type(value)
            container_type = default_type
            tp_args = ", ".join(set(type(x).__name__ for x in default))
            tp_name = "1 or more of: " + tp_args
        else:
            try:
                container_type = tp._name in ["List", "Iterable", "Set", "Tuple"]
            except AttributeError:
                pass
            if container_type:
                tp_args = ", ".join(x.__name__ for x in tp.__args__)
                tp_name = "1 or more of: " + tp_args
            else:
                tp_name = tp.__name__
        if is_pydantic(tp):
            # msg = f"Cannot use pydantic just yet, argument {var_name!r} (type {tp.__name__}) on cmd {cmd.prog!r}"
            # raise ValueError(msg)
            add_group(cmd, tp, fn, var_name)
            continue
        arg_desc = f"|{tp_name}| {default_help}" + doc_params.get(var_name, "")
        add_argument(cmd, tp, container_type, var_name, default, arg_desc)


def get_parser():
    frame = currentframe().f_back
    module_doc = frame.f_code.co_consts[0]
    module_doc = module_doc if isinstance(module_doc, str) else None
    parser = ColoredHelpOnErrorParser(description=module_doc)
    parser.add_argument(
        "--traceback",
        action="store_true",
        default=False,
        help="Whether to enable python tracebacks",
    )

    from cliche import fn_registry

    if fn_registry:
        subparsers = parser.add_subparsers(dest="command")
        for fn_name, (decorated_fn, fn) in fn_registry.items():
            cmd = add_command(subparsers, fn_name, fn)
            add_arguments_to_command(cmd, fn)

    return parser


def main(exclude_module_names=None, *parser_args):
    if "cliche" in sys.argv[0]:
        module_name = sys.argv[1]
        sys.argv.remove(module_name)
        import importlib.util

        spec = importlib.util.spec_from_file_location("pydantic", module_name)
        foo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(foo)
        ColoredHelpOnErrorParser.module_name = module_name

    if exclude_module_names is not None:
        # exclude module namespaces
        for x in exclude_module_names:
            for k, v in list(fn_registry.items()):
                _, fn = v
                if x in fn.__module__:
                    fn_registry.pop(k)
    parser = get_parser()

    if parser_args:
        arguments = parser.parse_args(parser_args)
    else:
        arguments = parser.parse_args()
    cmd = None
    try:
        cmd = arguments.command
    except AttributeError:
        warn("No commands have been registered.\n")
        parser.print_help()
        sys.exit(3)
    kwargs = dict(arguments._get_kwargs())
    kwargs.pop("command")
    if cmd is None:
        parser.print_help()
    else:
        from cliche import fn_registry

        fn_registry[cmd][0](*arguments._get_args(), **kwargs)
