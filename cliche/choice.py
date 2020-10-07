from enum import Enum
from argparse import Action


def Choice(*args):
    return Enum("Choice", args)


# credits: https://stackoverflow.com/a/60750535/1575066


class EnumAction(Action):
    """
    Argparse action for handling Enums
    """

    def __init__(self, **kwargs):
        # Pop off the type value
        enum = kwargs.pop("type", None)

        # Ensure an Enum subclass is provided
        if enum is None:
            raise ValueError("type must be assigned an Enum when using EnumAction")
        if not issubclass(enum, Enum):
            raise TypeError("type must be an Enum when using EnumAction")
        # Generate choices from the Enum
        kwargs.setdefault("choices", tuple(e.name for e in enum))

        super(EnumAction, self).__init__(**kwargs)

        self._enum = enum

    def lookup(self, value):
        if value in self._enum.__members__:
            enum = self._enum[value]
        else:
            try:
                value = int(value)
            except ValueError:
                pass
            enum = self._enum(value)
        return enum

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        if isinstance(values, list):
            result = [self.lookup(x) for x in values]
        else:
            result = self.lookup(values)
        setattr(namespace, self.dest, result)
