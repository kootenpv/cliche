# mincli

## Features

- Least syntax required
- Does not reinvent the wheel, standing on the shoulders of giants (argparse)
- Lightweight: very few lines
- Just decorate a function, that is it, it can be called
- Similar and familiar to:
  - argparse: you need a lot of code to construct an argparse CLI but it is powerful
  - click: you need a lot of decorators to construct a CLI, not obvious by default
  - hug (cli): connected to a whole web framework, but gets a lot right
  - python-fire: low set up, but annoying traces all the time, does not show default values nor types
  - cleo: requires too much code/objects to construct

It borrows:
- you can call "python myfile.py" or "mincli myfile.py" like hug
- No need to construct
