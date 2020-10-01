{{first_line}}

# the above should be dynamic
import os
import re
import sys
import json
import glob

any_change = False
new_cache = {}

import sys

file_path = "{{cwd}}"
sys.path.insert(0, file_path)

new_cache = {}
# cache filename should be dynamic
try:
    with open("{{bin_name}}.cache") as f:
        cache = json.load(f)
except FileNotFoundError:
    cache = {}

# this path should be dynamic
for x in glob.glob("{{cwd}}/*.py") + glob.glob("{{cwd}}/**/*.py"):
    if any(e in x for e in ["#", "flycheck", "swp"]):
        continue
    mod_date = os.stat(x)[8]
    if x in cache:
        new_cache[x] = cache[x]
        if cache[x]["mod_date"] == mod_date:
            continue
    any_change = True
    with open(x) as f:
        contents = f.read()
        functions = re.findall(r"^ *@cli *\n *def ([^( ]+)+", contents, re.M)
        version = re.findall("""^ *__version__ = ['"]([^'"]+)""", contents)
        cache[x] = {
            "mod_date": mod_date,
            "functions": functions,
            "filename": x,
            "import_name": x.replace(file_path, "").strip("/").replace("/", ".").replace(".py", ""),
        }
        if version:
            cache[x]["version_info"] = version[0]
        new_cache[x] = cache[x]

if any_change:
    cache = new_cache
    with open("{{bin_name}}.cache", "w") as f:
        json.dump(cache, f)

function_to_imports = {}
version_info = None
for cache_value in cache.values():
    import_name = cache_value["import_name"]
    functions = cache_value["functions"]
    version_info = version_info or cache_value.get("version_info")
    if not functions:
        continue
    for function in functions:
        function_to_imports[function] = import_name


def fallback(version_info=None):
    for import_name in sorted(set(function_to_imports.values())):
        __import__(import_name)
    from cliche import main

    main(version_info=version_info)


if len(sys.argv) > 1:
    command = sys.argv[1]
    if command in function_to_imports:
        __import__(function_to_imports[command])
        from cliche import main

        main(version_info=version_info)
    else:
        fallback(version_info=version_info)
else:
    fallback(version_info=version_info)
