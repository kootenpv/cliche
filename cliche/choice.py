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


class DictAction(Action):
    """
    Argparse action for handling Protobuf Enums
    """

    def __init__(self, **kwargs):
        self.key_class = kwargs["type"][0][0]
        self.value_class = kwargs["type"][1][0]

        self._tps = kwargs.pop("type", None)

        super(DictAction, self).__init__(**kwargs)

    def enum_lookup(self, key_or_value, value):
        enum = None
        if key_or_value == "key":
            if value in self.key_class.__members__:
                enum = self.key_class[value]
        elif key_or_value == "value":
            if value in self.value_class.__members__:
                enum = self.value_class[value]
        if enum is None:
            try:
                value = int(value)
            except ValueError:
                pass
            enum_class = self.key_class if key_or_value == "key" else self.value_class
            enum = enum_class(value)
        return enum

    def proto_enum_lookup(self, key_or_value, value):
        if key_or_value == "key":
            res = dict(zip(self.key_class.keys(), self.key_class.values()))
            if value in res:
                return res[value]
            try:
                return int(value)
            except:
                raise ValueError(
                    f"{value} not in Protobuf type {self.key_class._enum_type.name}, valid keys: {list(res.keys())}"
                )
        if key_or_value == "value":
            res = dict(zip(self.value_class.keys(), self.value_class.values()))
            if value in res:
                return res[value]
            try:
                return int(value)
            except:
                raise ValueError(
                    f"{value} not in Protobuf type {self.value_class._enum_type.name}, valid values: {list(res.keys())}"
                )

    def key_lookup(self, key):
        if hasattr(self.key_class, "__members__"):
            return self.enum_lookup("key", key)
        if "EnumTypeWrapper" in str(self.key_class):
            return self.proto_enum_lookup("key", key)
        return self.key_class(key)

    def value_lookup(self, value):
        if hasattr(self.key_class, "__members__"):
            return self.enum_lookup("value", value)
        if "EnumTypeWrapper" in str(self.value_class):
            return self.proto_enum_lookup("value", value)
        return self.value_class(value)

    def single_lookup(self, v) -> dict:
        key, value = v.split("=")
        return {self.key_lookup(key): self.value_lookup(value)}

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        if isinstance(values, list):
            result = {self.key_lookup(x.split("=")[0]): self.value_lookup(x.split("=")[1]) for x in values}
        else:
            result = self.single_lookup(values)
        setattr(namespace, self.dest, result)


class ProtoEnumAction(Action):
    """
    Argparse action for handling Protobuf Enums
    """

    def __init__(self, **kwargs):
        # Pop off the type value
        enum = kwargs.pop("type", None)

        # Ensure an Enum subclass is provided
        if enum is None:
            raise ValueError("type must be assigned an Enum when using EnumAction")
        # Generate choices from the Enum
        kwargs.setdefault("choices", tuple(enum.keys()))

        super(ProtoEnumAction, self).__init__(**kwargs)

        self._enum = enum

    def lookup(self, value):
        return self._enum.Value(value)

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        if isinstance(values, list):
            result = [self.lookup(x) for x in values]
        else:
            result = self.lookup(values)
        setattr(namespace, self.dest, result)
