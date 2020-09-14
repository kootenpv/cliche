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
sys.path.append("{{cwd}}")

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
        functions = re.findall(r"@cli\ndef ([^( ]+)+", contents)
        cache[x] = {
            "mod_date": mod_date,
            "functions": functions,
            "filename": x,
            "import_name": x.replace(file_path, "").strip("/").replace("/", ".").replace(".py", ""),
        }
        new_cache[x] = cache[x]

if any_change:
    cache = new_cache
    with open("{{bin_name}}.cache", "w") as f:
        json.dump(cache, f)

function_to_imports = {}
for cache_value in cache.values():
    import_name = cache_value["import_name"]
    functions = cache_value["functions"]
    if not functions:
        continue
    for function in functions:
        function_to_imports[function] = import_name


def fallback():
    for import_name in set(function_to_imports.values()):
        __import__(import_name)
    from cliche import main

    main()


if len(sys.argv) > 1:
    command = sys.argv[1]
    if command in function_to_imports:
        __import__(function_to_imports[command])
        from cliche import main

        main()
    else:
        fallback()
else:
    fallback()
