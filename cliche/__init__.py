import re
import os
import sys
import time
from inspect import signature, currentframe, getmro
import traceback
from typing import List, Iterable, Set, Tuple, Union

try:
    import argcomplete

    ARGCOMPLETE_IMPORTED = True
except ImportError:
    ARGCOMPLETE_IMPORTED = False

from cliche.using_underscore import UNDERSCORE_DETECTED
from cliche.install import install, uninstall
from cliche.choice import Choice, Enum
from cliche.argparser import (
    ColoredHelpOnErrorParser,
    pydantic_models,
    add_arguments_to_command,
    add_command,
    get_var_name_and_default,
    bool_inverted,
)


def get_class(f):
    vals = vars(sys.modules[f.__module__])
    for attr in f.__qualname__.split('.')[:-1]:
        try:
            vals = vals[attr]
        except TypeError:
            return None
    if isinstance(vals, dict):
        return None
    return vals


def get_init(f):
    cl = get_class(f)
    if cl is None:
        return None, None
    for init_class in cl.__mro__:
        init = getattr(init_class, "__init__")
        if init is not None:
            return init_class, init


# t1 = time.time()

fn_registry = {}
fn_class_registry = {}
main_called = []
version = []


def warn(x):
    sys.stderr.write("\033[31m" + x + "\033[0m\n")
    sys.stderr.flush()


def cli(fn):
    # print(fn, time.time() - t1) # for debug

    def decorated_fn(*args, **kwargs):
        no_traceback = False
        raw = False
        if "notraceback" in kwargs:
            no_traceback = kwargs.pop("notraceback")
        if "raw" in kwargs:
            raw = kwargs.pop("raw")
        if "cli" in kwargs:
            kwargs.pop("cli")
        try:
            if not UNDERSCORE_DETECTED:
                kwargs = {k.replace("-", "_"): v for k, v in kwargs.items()}
            if fn in pydantic_models:
                for var_name in pydantic_models[fn]:
                    model, model_args = pydantic_models[fn][var_name]
                    for m in model_args:
                        kwargs.pop(m)
                    kwargs[var_name] = model(**kwargs)
            res = fn(*args, **kwargs)
            if res is not None:
                if raw:
                    print(res)
                else:
                    import json

                    try:
                        print(json.dumps(res, indent=4))
                    except json.JSONDecodeError:
                        print(res)
        except Exception as e:
            fname, sig = fn.__name__, signature(fn)
            print("Fault while calling {}{} with the above arguments".format(fname, sig))
            if no_traceback:
                warn(traceback.format_exception_only(type(e), e)[-1].strip().split(" ", 1)[1])
                sys.exit(1)
            else:
                raise

    if UNDERSCORE_DETECTED:
        fn_registry[fn.__name__] = (decorated_fn, fn)
    else:
        fn_registry[fn.__name__.replace("_", "-")] = (decorated_fn, fn)
    return fn


def highlight(x):
    return "\x1b[1;36m{}\x1b[0m".format(x)


def cli_info(**kwargs):
    """ Outputs CLI and Python version info and exit. """
    sv = sys.version_info
    python_version = "{}.{}.{}".format(sv.major, sv.minor, sv.micro)
    installed = False
    try:
        with open(sys.argv[0]) as f:
            txt = f.read()
            installed = "__import__(function_to_imports[command])" in txt
            file_path = re.findall('file_path = "(.+)"', txt)
    except FileNotFoundError:
        pass
    autocomplete = False
    try:
        name = os.path.basename(sys.argv[0])
        with open(os.path.expanduser("~/.bashrc")) as f:
            autocomplete = f"register-python-argcomplete {name}" in f.read()
    except FileNotFoundError:
        pass
    v = f" (version {version[0]})" if version else ""
    print("Executable:          ", highlight(name + v))
    print("Executable path:     ", highlight(sys.argv[0]))
    print("Installed by cliche: ", highlight(installed))
    if installed:
        print("CLI directory:       ", highlight(file_path[0]))
    print("Autocomplete enabled:", highlight(autocomplete), "(only possible on Linux)")
    print("Python Version:      ", highlight(python_version))
    print("Python Interpreter:  ", highlight(sys.executable))


def add_traceback(parser):
    parser.add_argument(
        "--notraceback",
        action="store_true",
        default=False,
        help="Omit showing Python tracebacks",
    )


def add_raw(parser):
    parser.add_argument(
        "--raw",
        action="store_true",
        default=False,
        help="Prevent function output as JSON",
    )


def add_cli(parser):
    parser.add_argument(
        "--cli",
        action="store_true",
        default=False,
        help=cli_info.__doc__,
    )


def add_cliche_self_parser(parser):
    subparsers = parser.add_subparsers(dest="command")
    installer = subparsers.add_parser("install", help="Create CLI from folder")
    installer.add_argument('name', help='Name of the cli to create')
    installer.add_argument(
        "-n",
        '--no-autocomplete',
        action="store_false",
        help='Default: False | Whether to add autocomplete support',
    )
    bool_inverted.add("no_autocomplete")
    fn_registry["install"] = [install, install]
    uninstaller = subparsers.add_parser("uninstall", help="Delete CLI")
    uninstaller.add_argument('name', help='Name of the cli to remove')
    fn_registry["uninstall"] = [uninstall, uninstall]


def add_class_arguments(cmd, fn, fn_name):
    abbrevs = None
    init_class, init = get_init(fn)
    if init is not None:
        group_name = "INITIALIZE CLASS: {}()".format(init.__qualname__.split('.')[0])
        group = cmd.add_argument_group(group_name)
        var_names = [x for x in init.__code__.co_varnames if x not in ["self", "cls"]]
        fn_class_registry[fn_name] = (init_class, var_names)
        abbrevs = add_arguments_to_command(group, init, abbrevs)
    return abbrevs


def add_optional_cliche_arguments(cmd):
    group = cmd.add_argument_group("OPTIONAL CLI ARGUMENTS")
    add_traceback(group)
    add_raw(group)


def get_parser():
    frame = currentframe().f_back
    module_doc = frame.f_code.co_consts[0]
    module_doc = module_doc if isinstance(module_doc, str) else None
    parser = ColoredHelpOnErrorParser(description=module_doc)

    from cliche import fn_registry

    if fn_registry:
        add_cli(parser)

        # if only one @cli and the second arg is not a command
        if len(fn_registry) == 1 and (len(sys.argv) < 2 or sys.argv[1] not in fn_registry):
            fn = list(fn_registry.values())[0][1]
            add_arguments_to_command(parser, fn)
        else:
            subparsers = parser.add_subparsers(dest="command")
            for fn_name, (decorated_fn, fn) in sorted(
                fn_registry.items(), key=lambda x: (x[0] == "info", x[0])
            ):
                cmd = add_command(subparsers, fn_name, fn)

                # for methods defined on classes, add those args
                abbrevs = add_class_arguments(cmd, fn, fn_name)

                add_arguments_to_command(cmd, fn, abbrevs)

                add_optional_cliche_arguments(cmd)

    else:
        add_cliche_self_parser(parser)

    return parser


def main(exclude_module_names=None, version_info=None, *parser_args):
    if main_called:
        return
    main_called.append(True)
    # if "cliche" in sys.argv[0] and "cliche/" not in sys.argv[0]:
    #     module_name = sys.argv[1]
    #     sys.argv.remove(module_name)
    #     import importlib.util

    #     spec = importlib.util.spec_from_file_location("pydantic", module_name)
    #     module = importlib.util.module_from_spec(spec)
    #     spec.loader.exec_module(module)
    #     ColoredHelpOnErrorParser.module_name = module_name

    if exclude_module_names is not None:
        # exclude module namespaces
        for x in exclude_module_names:
            for k, v in list(fn_registry.items()):
                _, fn = v
                if x in fn.__module__:
                    fn_registry.pop(k)

    if version_info is not None:
        version.append(version_info)

    if "--cli" in sys.argv:
        cli_info()
        sys.exit(0)

    parser = get_parser()

    if ARGCOMPLETE_IMPORTED:
        argcomplete.autocomplete(parser)

    if parser_args:
        parsed_args = parser.parse_args(parser_args)
    else:
        parsed_args = parser.parse_args()

    cmd = None
    try:
        cmd = parsed_args.command
    except AttributeError:
        if len(fn_registry) == 1:
            cmd = list(fn_registry)[0]
        else:
            warn("No commands have been registered.\n")
            parser.print_help()
            sys.exit(3)
    kwargs = dict(parsed_args._get_kwargs())
    if "command" in kwargs:
        kwargs.pop("command")
    for x in bool_inverted:
        if x in kwargs:
            # stripping "no-" from e.g. "--no-sums"
            kwargs[x[3:]] = kwargs.pop(x)
    if cmd is None:
        parser.print_help()
    else:
        # test.... i think this is never filled, so lets try always with empty
        # starargs = parsed_args._get_args()
        starargs = []
        if cmd in fn_class_registry:
            init_class, init_varnames = fn_class_registry[cmd]
            init_kwargs = {k: kwargs.pop(k) for k in init_varnames if k in kwargs}
            # [k for k in init_varnames if k not in init_kwargs]
            fn_registry[cmd][0](init_class(**init_kwargs), **kwargs)
        else:
            fn_registry[cmd][0](*starargs, **kwargs)
