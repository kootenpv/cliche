from enum import Enum
from argparse import Action


class Choice(list):
    def __init__(self, *args, **kwargs):
        args = args if isinstance(args[0], (str, int)) else args[0]
        super(Choice, self).__init__(args)

    def __repr__(self):
        return f"{self.__class__.__name__}({super().__repr__()})"

    def __call__(self, *args, **kwargs):
        import pdb

        pdb.set_trace()


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

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        if values in self._enum.__members__:
            enum = self._enum[values]
        else:
            try:
                values = int(values)
            except ValueError:
                pass
            enum = self._enum(values)
        setattr(namespace, self.dest, enum)
