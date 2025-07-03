"""Utilities for handling type annotations in cliche."""

import types
from typing import Union


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
            # This is a stringified union type, we can't use it as a callable
            # Default to str for now
            return str
        return tp

    # Handle Python 3.10+ union syntax (X | Y)
    if isinstance(tp, types.UnionType):
        # Find the non-None type in the union
        args = tp.__args__
        non_none_types = [arg for arg in args if arg is not type(None)]
        if non_none_types:
            return non_none_types[0]
        return str  # Default to str if we can't find a proper type

    # Handle typing.Union
    if hasattr(tp, "__origin__") and tp.__origin__ is Union:
        args = tp.__args__
        non_none_types = [arg for arg in args if arg is not type(None)]
        if non_none_types:
            return non_none_types[0]
        return str  # Default to str if we can't find a proper type

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

    # Find the container type
    bracket_pos = annotation_str.find("[")
    if bracket_pos == -1:
        return None, None

    container_name = annotation_str[:bracket_pos].strip()

    # Extract inner content
    inner = annotation_str[bracket_pos + 1 : annotation_str.rfind("]")]

    # Handle ellipsis notation
    inner = inner.replace(", ...", "").strip()

    return container_name, inner
