import re
import os
import sys
import platform


def install(name, autocomplete=True, **kwargs):
    cliche_path = os.path.dirname(os.path.realpath(__file__))
    with open(sys.argv[0]) as f:
        first_line = f.read().split("\n")[0]
    cwd = os.getcwd()
    bin_path = os.path.dirname(sys.argv[0])
    bin_name = os.path.join(bin_path, name)
    if os.path.exists(bin_name):
        raise FileExistsError(bin_name)
    template_path = os.path.join(cliche_path, "install_generator.py")
    with open(template_path) as f:
        template = f.read()
    with open(bin_name, "w") as f:
        for k, v in {"cwd": cwd, "bin_name": bin_name, "first_line": first_line}.items():
            template = re.sub("{{ *" + re.escape(k) + " *}}", v, template)
        f.write(template)
    os.system("chmod +x " + bin_name)
    if autocomplete and platform.system() == "Linux":
        try:
            import argcomplete
        except ImportError:
            print(
                "Can't import argcomplete. either run with --no_autocomplete or install argcomplete"
            )
            raise
        os.system(
            f"""echo 'eval "$({bin_path}/register-python-argcomplete {name})"' >> ~/.bashrc"""
        )
        print("Note: for autocomplete to work, please reopen a terminal.")


def uninstall(name, **kwargs):
    bin_path = os.path.dirname(sys.argv[0])
    bin_name = os.path.join(bin_path, name)
    with open(bin_name) as f:
        txt = f.read()
        if "from cliche" not in txt:
            raise ValueError(f"The command {name!r} does not seem to have been installed by cliche")
    try:
        os.remove(bin_name)
    except FileNotFoundError:
        pass
    try:
        os.remove(bin_name + ".cache")
    except FileNotFoundError:
        pass
    if platform.system() == "Linux":
        with open(os.path.expanduser("~/.bashrc")) as f:
            inp = f.read()
        autocomplete_line = f'register-python-argcomplete {name})"\n'
        if autocomplete_line in inp:
            inp = "\n".join([x for x in inp.split("\n") if autocomplete_line.strip() not in x])
            with open(os.path.expanduser("~/.bashrc"), "w") as f:
                f.write(inp)
