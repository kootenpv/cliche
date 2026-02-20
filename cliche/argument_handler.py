"""Cleaner argument handling utilities for cliche."""

from enum import Enum
from cliche.docstring_to_help import parse_doc_params
from cliche.type_utils import ArgumentBuilder, TypeInfo, TypeResolver
from cliche.using_underscore import UNDERSCORE_DETECTED


class DefaultFormatter:
    """Formats default values for display in help text."""
    
    @staticmethod
    def format(default, tp, container_type):
        """Format a default value for display."""
        if default == "--1":
            return ""
            
        if isinstance(default, Enum):
            return default.name
            
        # Handle protobuf wrapper types
        if "Wrapper" in str(tp):
            if container_type and default:
                formatted = str(container_type([tp.Name(x) for x in default]))
                return formatted.replace("'", "").replace('"', "")
            elif default:
                return tp.Name(default)
                
        return default
    
    @staticmethod
    def get_help_text(default, tp, container_type):
        """Get the help text for a default value."""
        if default == "--1":
            return ""
        formatted = DefaultFormatter.format(default, tp, container_type)
        return f"Default: {formatted} | "


class BooleanArgumentHandler:
    """Handles boolean argument special cases."""
    
    @staticmethod
    def should_invert(tp, default):
        """Check if a boolean argument should be inverted."""
        return tp == bool and default is True
    
    @staticmethod
    def invert_argument(var_name, default):
        """Invert a boolean argument."""
        new_name = "no_" + var_name
        new_default = not default
        return new_name, new_default


class ArgumentProcessor:
    """Processes function arguments for CLI."""
    
    def __init__(self, fn, abbrevs=None):
        self.fn = fn
        self.abbrevs = abbrevs or ["-h"]
        self.doc_params = self._parse_doc_params()
        self.resolver = TypeResolver(fn)
        
    def _parse_doc_params(self):
        """Parse documentation parameters."""
        doc_str = self.fn.__doc__ or ""
        return parse_doc_params(doc_str)
    
    def process_arguments(self, cmd):
        """Process all arguments for a function."""
        from cliche.argparser import (
            get_var_name_and_default, 
            is_pydantic, 
            add_group,
            bool_inverted,
            class_init_lookup
        )
        
        # Update resolver with class_init_lookup
        self.resolver.class_init_lookup = class_init_lookup
        
        for var_name, default in get_var_name_and_default(self.fn):
            # Get type information
            type_info = self._get_type_info(var_name, default)
            
            # Handle pydantic models specially
            if is_pydantic(type_info.element_type):
                add_group(cmd, type_info.element_type, self.fn, var_name, self.abbrevs)
                continue
            
            # Handle boolean inversion
            if BooleanArgumentHandler.should_invert(type_info.element_type, default):
                var_name, _ = BooleanArgumentHandler.invert_argument(var_name, default)
                bool_inverted.add(var_name)
                default = True  # Set back to True for argparse handling
            
            # Build argument description
            arg_desc = self._build_arg_description(var_name, default, type_info)
            
            # Add the argument
            self._add_argument(cmd, type_info, var_name, default, arg_desc)
            
        return self.abbrevs
    
    def _get_type_info(self, var_name, default):
        """Get type information for a parameter."""
        default_type = type(default) if default != "--1" and default is not None else None
        annotation = self.fn.__annotations__.get(var_name, default_type or str)
        return self.resolver.resolve(annotation, default, default_type)
    
    def _build_arg_description(self, var_name, default, type_info):
        """Build the argument description."""
        doc_text = self.doc_params.get(var_name, "")
        default_help = DefaultFormatter.get_help_text(
            default, 
            type_info.element_type, 
            type_info.container_type
        )
        return f"|{type_info.type_name}| {default_help}{doc_text}"
    
    def _add_argument(self, cmd, type_info, var_name, default, arg_desc):
        """Add an argument using the ArgumentBuilder."""
        (ArgumentBuilder(cmd, var_name, self.abbrevs, UNDERSCORE_DETECTED)
         .with_type_info(type_info)
         .with_default(default)
         .with_description(arg_desc)
         .build())


def add_arguments_to_command_clean(cmd, fn, abbrevs=None):
    """Cleaner version of add_arguments_to_command."""
    processor = ArgumentProcessor(fn, abbrevs)
    return processor.process_arguments(cmd)