# cliche

Features:

- Least syntax required, and keeps it DRY
- Does not reinvent the wheel, standing on the shoulders of giants (e.g. uses argparse, learnings from others)
- Lightweight: few lines of code
- Uses all information available like *annotations*, *default values* and *docstrings*, yet does not require them.
- Just decorate a function - that is it - it can now be called as CLI but also remains usable by other functions

## Examples

#### Simplest Example

You want to make a calculator. You not only want its functions to be reusable, you also want it to be callable from command line.

```python
# calculator.py
from cliche import cli

@cli
def add(a: int, b: int):
    print(a + b)
```

Now let's see how to use it from the command-line:

    pascal@archbook:~/$ cliche calculator.py add --help

    usage: cliche add [-h] a b

    positional arguments:
      a           |int|
      b           |int|

    optional arguments:
      -h, --help  show this help message and exit

thus:

    cliche calculator.py add 1 10
    11

#### Advanced Example

```python
from cliche import cli


@cli
def sum_or_multiply(a_number: int, b_number: int = 10, sums: bool = False):
    """ Sums or multiplies a and b

    :param a_number: the first one
    :param b_number: This parameter seems to be
    :param sums: Sums when true, otherwise multiply
    """
    if sums:
        print(a_number + b_number)
    else:
        print(a_number * b_number)
```

Calling it:

    pascal@archbook:~/$ cliche calculator.py sum_or_multiply --help

    usage: cliche sum_or_multiply [-h] [--b_number B_NUMBER] [--sums] a_number

    Sums or multiplies a and b

    positional arguments:
      a_number             |int| the first one

    optional arguments:
      -h, --help           show this help message and exit
      --b_number B_NUMBER  |int| Default: 10 | This parameter seems to be
      --sums               |bool| Default: False | Sums when true, otherwise multiply

#### More examples

Check the example files [here](https://github.com/kootenpv//tree/master/examples)

## Similar, and familiar to

  - argparse: you need a lot of code to construct an argparse CLI yet it is powerful
  - click: you need a lot of decorators to construct a CLI, not obvious in usage by default
  - hug (cli): connected to a whole web framework, but gets a lot right
  - python-fire: low set up, but annoying traces all the time, does not show default values nor types
  - cleo: requires too much code/objects to construct
