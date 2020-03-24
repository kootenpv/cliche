import re
import sys
import argparse
from inspect import signature, currentframe

fn_registry = {}
pydantic_models = {}


def warn(x):
    sys.stderr.write("\033[31m" + x + "\033[0m\n")
    sys.stderr.flush()


def cli(fn):
    def decorated_fn(*args, **kwargs):
        try:
            if fn in pydantic_models:
                for var_name in pydantic_models[fn]:
                    model, model_args = pydantic_models[fn][var_name]
                    for m in model_args:
                        kwargs.pop(m)
                    kwargs[var_name] = model(**kwargs)
            fn(*args, **kwargs)
        except:
            warn(f"Fault while calling {fn.__name__}{signature(fn)} with the above arguments")
            raise

    fn_registry[fn.__name__] = (decorated_fn, fn)

    return fn


class HelpOnErrorParser(argparse.ArgumentParser):
    def error(self, message):
        # TODO: it actually now prints generic help but it should print the specific help of the subcommand
        # print(sys.modules[cli.__module__].__doc__)
        self.print_help()
        message = message.replace(
            "unrecognized arguments", "unrecognized (too many positional) arguments"
        )
        warn(f"error: {message}")
        sys.exit(2)


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
        elif stack:
            stack["lines"].append(line.strip())
    if stack:
        results[stack["fn"]] = "\n".join(stack["lines"])
    return results


def add_command(subparsers, fn_name, fn):
    doc_str = fn.__doc__ or ""
    desc = re.split("^ *Parameter|^ *Return|^ *Example|:param|\n\n", doc_str)[0].strip()
    cmd = subparsers.add_parser(fn_name, help=desc, description=desc)
    return cmd


def is_pydantic(class_type):
    return "BaseModel" in [x.__name__ for x in class_type.__mro__]


def add_group(parser_cmd, model, fn, var_name):
    kwargs = []
    pydantic_models[fn] = {}
    group = parser_cmd.add_argument_group(model.__name__)
    for field_name, field in model.__fields__.items():
        kwargs.append(field_name)
        default = field.default if field.default is not None else "--1"
        default_help = f"Default: {default} | " if default != "--1" else ""
        tp = field.type_
        if is_pydantic(tp):
            msg = (
                f"Cannot use nested pydantic just yet:"
                + f"property {var_name}.{field_name} of function {fn.__name__}"
            )
            raise ValueError(msg)
        arg_desc = f"|{tp.__name__}| {default_help}"
        add_argument(group, tp, field_name, default, arg_desc)
    pydantic_models[fn][var_name] = (model, kwargs)


def add_argument(parser_cmd, tp, var_name, default, arg_desc):
    action = "store"
    if tp is bool:
        action = "store_true" if not default else "store_false"
        parser_cmd.add_argument("--" + var_name, action=action, help=arg_desc)
        return
    if default != "--1":
        parser_cmd.add_argument("--" + var_name, type=tp, default=default, help=arg_desc)
    else:
        parser_cmd.add_argument(var_name, type=tp, default=default, help=arg_desc)


def add_arguments_to_command(cmd, fn):
    doc_str = fn.__doc__ or ""
    arg_count = fn.__code__.co_argcount
    defs = fn.__defaults__ or tuple()
    defaults = (("--1",) * arg_count + defs)[-arg_count:]
    sphinx_params = parse_sphinx_param_descriptions(doc_str)
    for var_name, default in zip(fn.__code__.co_varnames, defaults):
        tp = fn.__annotations__.get(var_name, str)
        if is_pydantic(tp):
            # msg = f"Cannot use pydantic just yet, argument {var_name!r} (type {tp.__name__}) on cmd {cmd.prog!r}"
            # raise ValueError(msg)
            add_group(cmd, tp, fn, var_name)
            continue
        default_help = f"Default: {default} | " if default != "--1" else ""
        arg_desc = f"|{tp.__name__}| {default_help}" + sphinx_params.get(var_name, "")
        add_argument(cmd, tp, var_name, default, arg_desc)


def get_parser():
    frame = currentframe().f_back
    module_doc = frame.f_code.co_consts[0]
    module_doc = module_doc if isinstance(module_doc, str) else None
    parser = HelpOnErrorParser(description=module_doc)
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
