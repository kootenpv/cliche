"""File unrelated to the package, except for convenience in deploying"""

import os
import re
import sys

import sh

commit_count = sh.git("rev-list", ["--all"]).count("\n")

with open("pyproject.toml") as f:
    pyproject_content = f.read()

# Update version in pyproject.toml
major = re.search(r'version = "(\d+)\.(\d+)\.(\d+)"', pyproject_content).groups()[0]
minor = re.search(r'version = "(\d+)\.(\d+)\.(\d+)"', pyproject_content).groups()[1]
version = f"{major}.{minor}.{commit_count}"

pyproject_content = re.sub(r'version = "\d+\.\d+\.\d+"', f'version = "{version}"', pyproject_content)

with open("pyproject.toml", "w") as f:
    f.write(pyproject_content)

name = os.getcwd().split("/")[-1]

with open(f"{name}/__init__.py") as f:
    init = f.read()

with open(f"{name}/__init__.py", "w") as f:
    f.write(re.sub('__version__ = "[0-9.]+"', f'__version__ = "{version}"', init))

os.system("rm -rf dist/")
os.system(f"{sys.executable} -m build")
os.system("twine upload dist/*")
