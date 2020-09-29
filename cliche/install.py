import os
import sys
import platform
from jinja2 import Template


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
        template = Template(f.read())
    with open(bin_name, "w") as f:
        f.write(template.render(cwd=cwd, bin_name=bin_name, first_line=first_line))
    os.system("chmod +x " + bin_name)
    if autocomplete and platform.system() == "Linux":
        os.system(
            f"""echo 'eval "$({bin_path}/register-python-argcomplete {name})"' >> ~/.bashrc"""
        )


def uninstall(name, **kwargs):
    bin_path = os.path.dirname(sys.argv[0])
    bin_name = os.path.join(bin_path, name)
    with open(bin_name) as f:
        txt = f.read()
        if "from cliche" not in txt:
            raise ValueError("This executable does not seem installed by cliche")
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