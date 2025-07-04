"""Utilities for handling type annotations in cliche."""

import types
from dataclasses import dataclass
from enum import Enum
from typing import Union, get_args, get_origin

# Container mappings
CONTAINER_MAPPING = {"List": list, "Iterable": list, "Set": set, "Tuple": tuple}
CONTAINER_MAPPING.update({k.lower(): v for k, v in CONTAINER_MAPPING.items()})


def is_union_type(tp):
    """Check if a type is a Union type (including X | Y syntax)."""
    return (
        isinstance(tp, types.UnionType)
        or (hasattr(tp, "__origin__") and tp.__origin__ is Union)
        or (isinstance(tp, str) and (" | " in tp or "None |" in tp or "| None" in tp))
    )


def extract_type_from_union(tp):
    """Extract the non-None type from a Union type or return the type as-is."""
    # Handle string representation of union (e.g., 'X | None')
    if isinstance(tp, str):
        if " | None" in tp or "None | " in tp:
            return str  # Can't resolve string unions to callables
        return tp

    # Handle Python 3.10+ union syntax (X | Y) and typing.Union
    if isinstance(tp, types.UnionType) or (hasattr(tp, "__origin__") and tp.__origin__ is Union):
        args = tp.__args__
        non_none_types = [arg for arg in args if arg is not type(None)]
        return non_none_types[0] if non_none_types else str

    return tp


def is_protobuf_enum(tp):
    """Check if a type is a protobuf enum."""
    return (
        "EnumTypeWrapper" in str(tp)
        or (hasattr(tp, "__class__") and tp.__class__.__name__ == "EnumTypeWrapper")
        or hasattr(tp, "_enum_type")
    )


def parse_container_annotation(annotation_str):
    """Parse a string annotation like 'tuple[Location.V, ...]' to extract container and inner type."""
    annotation_str = annotation_str.strip()
    bracket_pos = annotation_str.find("[")
    if bracket_pos == -1:
        return None, None

    container_name = annotation_str[:bracket_pos].strip()
    inner = annotation_str[bracket_pos + 1 : annotation_str.rfind("]")]
    inner = inner.replace(", ...", "").strip()

    return container_name, inner


@dataclass
class TypeInfo:
    """Information about a resolved type annotation."""

    element_type: type  # The actual type to use for argparse
    type_name: str  # Human-readable type name for help text
    container_type: type | bool  # Container type (list, tuple, etc.) or False


class ActionStrategy:
    """Base class for argparse action strategies."""

    def can_handle(self, element_type) -> bool:
        """Check if this strategy can handle the given type."""
        raise NotImplementedError

    def get_action_name(self):
        """Return the action class to use."""
        raise NotImplementedError


class ProtoEnumActionStrategy(ActionStrategy):
    """Strategy for protobuf enum types."""

    def can_handle(self, element_type) -> bool:
        return is_protobuf_enum(element_type)

    def get_action_name(self):
        from cliche.choice import ProtoEnumAction

        return ProtoEnumAction


class StandardEnumActionStrategy(ActionStrategy):
    """Strategy for standard Python enum types."""

    def can_handle(self, element_type) -> bool:
        try:
            return issubclass(element_type, Enum)
        except TypeError:
            return False

    def get_action_name(self):
        from cliche.choice import EnumAction

        return EnumAction


class DictActionStrategy(ActionStrategy):
    """Strategy for dictionary types."""

    def can_handle(self, element_type) -> bool:
        return isinstance(element_type, tuple)  # Dict types are represented as ((key_type,), (value_type,))

    def get_action_name(self):
        from cliche.choice import DictAction

        return DictAction


class ActionFactory:
    """Factory for determining the appropriate argparse action for a type."""

    def __init__(self) -> None:
        self.strategies = [
            DictActionStrategy(),
            ProtoEnumActionStrategy(),
            StandardEnumActionStrategy(),
        ]

    def get_action_for_type(self, element_type):
        """Get the appropriate action class for the given type, or None for standard argparse."""
        for strategy in self.strategies:
            if strategy.can_handle(element_type):
                return strategy.get_action_name()
        return None


class ArgumentBuilder:
    """Builder for creating argparse arguments with clean separation of concerns."""

    def __init__(self, parser_cmd, var_name, abbrevs, underscore_detected=True) -> None:
        self.parser_cmd = parser_cmd
        self.original_var_name = var_name
        self.var_name = var_name if underscore_detected else var_name.replace("_", "-")
        self.abbrevs = abbrevs
        self.kwargs = {}
        self.var_names = []
        self.action_factory = ActionFactory()

    def with_type_info(self, type_info: "TypeInfo"):
        """Configure the builder with resolved type information."""
        self.type_info = type_info
        return self

    def with_default(self, default):
        """Set the default value."""
        self.default = default
        return self

    def with_description(self, description):
        """Set the help description."""
        self.description = description.replace("%", "%%")
        return self

    def build(self):
        """Build and add the argument to the parser."""
        # Handle boolean arguments specially
        if self.type_info.element_type is bool:
            return self._build_boolean_argument()

        # Set up nargs for containers
        nargs = "*" if self.type_info.container_type else None

        # Handle union type extraction
        element_type = self.type_info.element_type
        if is_union_type(element_type):
            element_type = extract_type_from_union(element_type)

        # Determine action
        action = self.action_factory.get_action_for_type(element_type)
        if action:
            self.kwargs["action"] = action

        # Set up variable names
        if self.default != "--1":
            self.var_name = "--" + self.var_name
        self.var_names = self._get_var_names()

        # Handle container defaults
        if nargs == "*" and self.default == "--1":
            self.default = self.type_info.container_type() if self.type_info.container_type else []

        # Register container type for post-processing
        if self.type_info.container_type:
            fn = self.parser_cmd.prog.split()[-1]
            # Import here to avoid circular import
            from cliche.argparser import container_fn_name_to_type

            container_fn_name_to_type[(fn, self.var_name)] = self.type_info.container_type

        # Add the argument
        if "action" in self.kwargs:
            # Custom actions handle type conversion themselves
            self.kwargs["type"] = element_type
            self.parser_cmd.add_argument(
                *self.var_names, nargs=nargs, default=self.default, help=self.description, **self.kwargs
            )
            return None
        else:
            # Standard argparse type conversion
            self.parser_cmd.add_argument(
                *self.var_names,
                type=element_type,
                nargs=nargs,
                default=self.default,
                help=self.description,
                **self.kwargs,
            )
            return None

    def _build_boolean_argument(self) -> None:
        """Build a boolean argument."""
        action = "store_true" if not self.default else "store_false"
        var_names = self._get_var_names("--" + self.var_name)
        self.parser_cmd.add_argument(*var_names, action=action, help=self.description)

    def _get_var_names(self, var_name=None):
        """Generate variable names with abbreviations."""
        if var_name is None:
            var_name = self.var_name

        # adds shortenings when possible
        if var_name.startswith("--"):
            short = "-" + var_name[2]
            # don't add shortening for inverted bools
            if var_name.startswith(("--no-", "--no_")):
                var_names = [var_name]
            elif short not in self.abbrevs:
                self.abbrevs.append(short)
                var_names = [short, var_name]
            elif short.upper() not in self.abbrevs:
                self.abbrevs.append(short.upper())
                var_names = [short.upper(), var_name]
            else:
                var_names = [var_name]
        else:
            var_names = [var_name]
        return var_names


class LookupResolver:
    """Unified resolver for type lookups."""

    @staticmethod
    def resolve(fn, tp, sans="", class_init_lookup=None):
        """Unified type lookup with fallback strategies."""
        import types as types_module

        # Handle Union types
        if isinstance(tp, str) and "|" in tp:
            return LookupResolver._handle_pipe_union(fn, tp, class_init_lookup)

        if (hasattr(tp, "__name__") and tp.__name__ == "UnionType") or isinstance(tp, types_module.UnionType):
            assert len(tp.__args__) == 2, "Union may at most have 2 types"
            assert type(None) in tp.__args__, "Union must have one None"
            a, b = tp.__args__
            if type(a) == type(None):
                b, a = a, b
            return LookupResolver._base_lookup(fn, a.__name__, a.__name__, class_init_lookup)

        # Handle Optional types
        sans_optional = tp.replace("Optional[", "") if isinstance(tp, str) else str(tp).replace("Optional[", "")
        if sans_optional != (tp if isinstance(tp, str) else str(tp)):  # strip ]
            sans_optional = sans_optional[:-1]
            return LookupResolver._base_lookup(
                fn, tp if isinstance(tp, str) else str(tp), sans_optional, class_init_lookup
            )

        # Default to base lookup
        return LookupResolver._base_lookup(fn, tp if isinstance(tp, str) else str(tp), sans, class_init_lookup)

    @staticmethod
    def _handle_pipe_union(fn, tp, class_init_lookup=None):
        """Handle pipe union types like 'X | None'."""
        if tp.startswith("None | "):
            sans_optional = tp[7:]
        elif tp.endswith("| None"):
            sans_optional = tp[:-7]
        else:
            msg = f"Optional confusion: {fn} {tp}"
            raise Exception(msg)
        return LookupResolver._base_lookup(fn, tp, sans_optional, class_init_lookup)

    @staticmethod
    def _base_lookup(fn, tp, sans, class_init_lookup=None):
        """Base lookup implementation."""
        tp_ending = tuple(tp.split("."))
        sans_ending = tuple(sans.split("."))

        if class_init_lookup and fn.__qualname__ in class_init_lookup:
            fn.lookup = class_init_lookup[fn.__qualname__]

        if tp_ending in fn.lookup:
            tp_name = tp
            tp = fn.lookup[tp_ending]
        elif sans_ending in fn.lookup:
            tp_name = sans
            tp = fn.lookup[sans_ending]
        else:
            tp_name = sans
            tp = __builtins__.get(sans, sans)
        return tp, tp_name


class TypeResolver:
    """Centralized type resolution for cliche annotations."""

    def __init__(self, fn, base_lookup_fn=None, class_init_lookup=None) -> None:
        self.fn = fn
        self.class_init_lookup = class_init_lookup
        if base_lookup_fn:
            self.base_lookup = base_lookup_fn
        else:
            # Use partial application to pass class_init_lookup
            from functools import partial

            self.base_lookup = partial(LookupResolver.resolve, class_init_lookup=class_init_lookup)

    def resolve(self, annotation, default_value, default_type) -> TypeInfo:
        """Main entry point for type resolution."""
        # Step 1: Handle union types first
        if is_union_type(annotation):
            annotation = extract_type_from_union(annotation)

        # Step 2: Detect container type
        container_type = self._detect_container_type(annotation, default_type)

        # Step 3: Resolve element type
        if container_type:
            element_type, type_name = self._resolve_container_element_type(annotation, container_type, default_value)
        else:
            element_type, type_name = self._resolve_simple_type(annotation)

        return TypeInfo(element_type, type_name, container_type)

    def _detect_container_type(self, annotation, default_type):
        """Detect if this is a container type and return the container class."""
        # Check default type first
        if default_type in [list, set, tuple, dict]:
            return default_type

        # Check typing system
        origin = get_origin(annotation)
        if origin and origin != Union:
            return origin

        # Check string annotations
        if isinstance(annotation, str):
            for container_name, container_class in CONTAINER_MAPPING.items():
                if container_name.lower() + "[" in annotation.lower():
                    return container_class

        return False

    def _resolve_container_element_type(self, annotation, container_type, default_value):
        """Resolve the element type for container annotations."""
        if container_type is dict:
            return self._resolve_dict_type(annotation, default_value)

        # Try typing system first
        tp_args = get_args(annotation)
        if tp_args:
            element_type = tp_args[0]
            if is_protobuf_enum(element_type):
                return element_type, f"1 or more of: {element_type._enum_type.name}"
            elif hasattr(element_type, "__name__"):
                return element_type, f"1 or more of: {element_type.__name__}"

        # Fall back to string parsing
        if isinstance(annotation, str):
            return self._resolve_string_container_element(annotation)

        # Fall back to default analysis
        if default_value:
            element_type = type(next(iter(default_value)))
            element_name = element_type.__name__ if hasattr(element_type, "__name__") else "str"
            return element_type, f"1 or more of: {element_name}"

        return str, "1 or more of: str"

    def _resolve_dict_type(self, annotation, default_value):
        """Resolve dict type annotations."""
        tp_args = get_args(annotation)
        if len(tp_args) >= 2:
            key_type, value_type = tp_args[0], tp_args[1]
            return ((key_type,), (value_type,)), f"dict[{key_type.__name__}, {value_type.__name__}]"

        if default_value:
            key_type = type(next(iter(default_value)))
            value_type = type(next(iter(default_value.values())))
            return ((key_type,), (value_type,)), f"dict[{key_type.__name__}, {value_type.__name__}]"

        return ((str,), (str,)), "dict[str, str]"

    def _resolve_string_container_element(self, annotation):
        """Resolve element type from string container annotations like 'tuple[Location.V, ...]'."""
        container_name, inner_type = parse_container_annotation(annotation)
        if not (container_name and inner_type):
            return str, "1 or more of: str"

        if "." in inner_type:
            # Handle protobuf enum references like Location.V -> Location
            enum_name = inner_type.split(".")[0]

            # Try direct lookup first
            if (enum_name,) in self.fn.lookup:
                enum_type = self.fn.lookup[(enum_name,)]
                return enum_type, f"1 or more of: {enum_name}"
            elif (enum_name, "V") in self.fn.lookup:
                enum_type = self.fn.lookup[(enum_name, "V")]
                return enum_type, f"1 or more of: {enum_name}"
            else:
                # Fall back to base_lookup
                resolved_type, resolved_name = self.base_lookup(self.fn, inner_type, enum_name)
                if resolved_type == inner_type:
                    resolved_type, resolved_name = self.base_lookup(self.fn, enum_name, enum_name)
                return resolved_type, f"1 or more of: {resolved_name}"
        else:
            # Simple type
            resolved_type, resolved_name = self.base_lookup(self.fn, inner_type, inner_type)
            return resolved_type, f"1 or more of: {resolved_name}"

    def _resolve_simple_type(self, annotation):
        """Resolve non-container type annotations."""
        if is_protobuf_enum(annotation):
            return annotation, annotation._enum_type.name
        elif hasattr(annotation, "__name__"):
            return annotation, annotation.__name__
        elif isinstance(annotation, str):
            resolved_type, resolved_name = self.base_lookup(self.fn, annotation, annotation)
            return resolved_type, resolved_name
        else:
            return annotation, str(annotation)
