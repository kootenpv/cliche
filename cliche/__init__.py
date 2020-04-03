import sys
from inspect import signature, currentframe
import traceback
from typing import List, Iterable, Set, Tuple, Union
from cliche.types import Choice
from cliche.argparser import (
    ColoredHelpOnErrorParser,
    pydantic_models,
    add_arguments_to_command,
    add_command,
)

fn_registry = {}


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
                warn(traceback.format_exception_only(type(e), e)[-1].strip().split(" ", 1)[1])
                sys.exit(1)

    fn_registry[fn.__name__.replace("_", "-")] = (decorated_fn, fn)

    return fn


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
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
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
