import re

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
                # e.g. :param name (str):
                if re.search(r":param +[^ ]+ +[(]", line):
                    fn_name = line.split(":")[1].split()[-2]
                # e.g. :param name:
                else:
                    fn_name = line.split(":")[1].split()[-1]
                stack = {"fn": fn_name, "lines": [line.split(":", 2)[2].strip()]}
        elif stack and line.strip():
            stack["lines"].append(line.strip())
    if stack:
        results[stack["fn"]] = "\n".join(stack["lines"])
    return results


def parse_doc_params(doc_str):
    doc_params = parse_sphinx_param_descriptions(doc_str)
    doc_params.update(parse_google_param_descriptions(doc_str))
    return doc_params
