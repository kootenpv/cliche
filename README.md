<p align="center">
  <img src="./resources/logo.gif"/>
</p>

# Cliche

Build a simple command-line interface from your functions.

Features:

- Least syntax required: you do not need to learn a "library" to use this
- keeps it DRY (Don't Repeat yourself):
  - it uses all information available like *annotations*, *default values* and *docstrings*... yet does not require them.
- Just decorate a function with `@cli` - that is it - it can now be called as CLI but also remains usable by other functions
- Standing on the shoulders of giants (i.e. it uses argparse and learnings from others) -> lightweight

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

    pascal@archbook:~/$ cliche calculator.py add 1 10
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

Help:

    pascal@archbook:~/$ cliche calculator.py sum_or_multiply --help

    usage: cliche sum_or_multiply [-h] [--b_number B_NUMBER] [--sums] a_number

    Sums or multiplies a and b

    positional arguments:
      a_number             |int| the first one

    optional arguments:
      -h, --help           show this help message and exit
      --b_number B_NUMBER  |int| Default: 10 | This parameter seems to be
      --sums               |bool| Default: False | Sums when true, otherwise multiply

Calling it:

    pascal@archbook:~/$ cliche calculator.py sum_or_multiply 1
    10

    pascal@archbook:~/$ cliche calculator.py sum_or_multiply --sum 1
    11

    pascal@archbook:~/$ cliche calculator.py sum_or_multiply --b_number 3 2
    6

#### More examples

Check the example files [here](https://github.com/kootenpv/cliche/tree/master/examples)

## Comparison with other CLI generators

  - argparse: it is powerful, but you need a lot of code to construct an argparse CLI
  - click: you need a lot of decorators to construct a CLI, and not obvious how to use it
  - hug (cli): connected to a whole web framework, but gets a lot right
  - python-fire: low set up, but annoying traces all the time / ugly design, does not show default values nor types
  - cleo: requires too much code/objects to construct
