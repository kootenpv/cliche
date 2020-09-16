import time

import sys
from inspect import signature, currentframe, getmro
import traceback
from typing import List, Iterable, Set, Tuple, Union
from cliche.using_underscore import UNDERSCORE_DETECTED
from cliche.types import Choice
from cliche.install import install, uninstall
from cliche.argparser import (
    ColoredHelpOnErrorParser,
    pydantic_models,
    add_arguments_to_command,
    add_command,
)


def get_class(f):
    vals = vars(sys.modules[f.__module__])
    for attr in f.__qualname__.split('.')[:-1]:
        vals = vals[attr]
    if isinstance(vals, dict):
        return None
    return vals


# t1 = time.time()

fn_registry = {}
main_called = []


def warn(x):
    sys.stderr.write("\033[31m" + x + "\033[0m\n")
    sys.stderr.flush()


def cli(fn):
    # print(fn, time.time() - t1) # for debug

    def decorated_fn(*args, **kwargs):
        show_traceback = False
        output_json = False
        if "traceback" in kwargs:
            show_traceback = kwargs.pop("traceback")
        if "raw" in kwargs:
            raw = kwargs.pop("raw")
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
            print(f"Fault while calling {fn.__name__}{signature(fn)} with the above arguments")
            if show_traceback:
                raise
            else:
                warn(traceback.format_exception_only(type(e), e)[-1].strip().split(" ", 1)[1])
                sys.exit(1)

    if UNDERSCORE_DETECTED:
        fn_registry[fn.__name__] = (decorated_fn, fn)
    else:
        fn_registry[fn.__name__.replace("_", "-")] = (decorated_fn, fn)
    return fn


def get_parser():
    frame = currentframe().f_back
    module_doc = frame.f_code.co_consts[0]
    module_doc = module_doc if isinstance(module_doc, str) else None
    parser = ColoredHelpOnErrorParser(description=module_doc)
    subparsers = parser.add_subparsers(dest="command")

    from cliche import fn_registry

    if fn_registry:
        parser.add_argument(
            "--traceback",
            action="store_true",
            default=False,
            help="Whether to enable python tracebacks",
        )
        parser.add_argument(
            "--raw",
            action="store_true",
            default=False,
            help="Whether to prevent attempting to output as json",
        )
        for fn_name, (decorated_fn, fn) in fn_registry.items():
            cmd = add_command(subparsers, fn_name, fn)
            add_arguments_to_command(cmd, fn)
    else:
        installer = subparsers.add_parser("install", help="Create CLI from folder")
        installer.add_argument('name', help='Name of the cli to create')
        fn_registry["install"] = [install, install]
        uninstaller = subparsers.add_parser("uninstall", help="Delete CLI")
        uninstaller.add_argument('name', help='Name of the cli to remove')
        fn_registry["uninstall"] = [uninstall, uninstall]

    return parser


def main(exclude_module_names=None, *parser_args):
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

    import pdb

    pdb.set_trace()
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
