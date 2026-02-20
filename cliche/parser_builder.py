"""Parser building utilities for cliche CLI."""

import sys
from collections import defaultdict
from inspect import currentframe

from cliche.argparser import add_arguments_to_command, add_command, get_desc_str
from cliche.output import CleanArgumentParser


class CommandResolver:
    """Resolves command line arguments to functions."""
    
    def __init__(self, fn_registry):
        self.fn_registry = fn_registry
        self.groups = {group for group, fn_name in fn_registry}
        self.fnames = {fn_name for group, fn_name in fn_registry}
        
    def get_possible_command(self):
        """Extract possible group and command from argv."""
        possible_group = sys.argv[1].replace("-", "_") if len(sys.argv) > 1 else "-"
        possible_cmd = sys.argv[2].replace("-", "_") if len(sys.argv) > 2 else "-"
        return possible_group, possible_cmd
    
    def is_direct_command(self, possible_group, possible_cmd):
        """Check if this is a direct command invocation."""
        return (possible_group, possible_cmd) in self.fn_registry
    
    def is_single_function_mode(self):
        """Check if we're in single function mode (only one @cli decorator)."""
        return (len(self.fn_registry) == 1 and 
                (len(sys.argv) < 2 or sys.argv[1].replace("-", "_") not in self.fnames))
    
    def is_group_command(self, possible_group):
        """Check if this is a group command."""
        return possible_group in self.groups
    
    def get_single_function(self):
        """Get the single registered function."""
        return next(iter(self.fn_registry.values()))[1]
    
    def get_group_functions(self, group):
        """Get all functions in a group."""
        return [(fn_name, fn) for (fn_group, fn_name), (_decorated_fn, fn) 
                in self.fn_registry.items() if fn_group == group]


class ParserBuilder:
    """Builds argument parsers for cliche CLI."""
    
    def __init__(self, fn_registry):
        self.fn_registry = fn_registry
        self.resolver = CommandResolver(fn_registry)
        
    def build(self):
        """Build the appropriate parser based on the command structure."""
        parser = self._create_base_parser()
        
        if not self.fn_registry:
            return parser
            
        from cliche import add_optional_cliche_arguments
        add_optional_cliche_arguments(parser)
        
        possible_group, possible_cmd = self.resolver.get_possible_command()
        
        # Handle direct command (group + command specified)
        if self.resolver.is_direct_command(possible_group, possible_cmd):
            return self._build_direct_command_parser(parser, possible_group, possible_cmd)
        
        # Handle single function mode
        if self.resolver.is_single_function_mode():
            return self._build_single_function_parser(parser)
        
        # Handle subcommands
        return self._build_subcommand_parser(parser, possible_group)
    
    def _create_base_parser(self):
        """Create the base argument parser."""
        frame = currentframe().f_back.f_back  # Go back two frames to get original caller
        module_doc = frame.f_code.co_consts[0]
        module_doc = module_doc if isinstance(module_doc, str) else None
        return CleanArgumentParser(description=module_doc)
    
    def _build_direct_command_parser(self, parser, possible_group, possible_cmd):
        """Build parser for direct command invocation."""
        from cliche import the_group, the_cmd
        
        # Update global state
        the_group = possible_group
        the_cmd = possible_cmd
        
        # Remove group and command from argv
        del sys.argv[1]
        del sys.argv[1]
        
        # Get function and add arguments
        decorated_fn, fn = self.fn_registry[(possible_group, possible_cmd)]
        add_arguments_to_command(parser, fn)
        parser.description = get_desc_str(fn)
        
        return parser
    
    def _build_single_function_parser(self, parser):
        """Build parser for single function mode."""
        fn = self.resolver.get_single_function()
        add_arguments_to_command(parser, fn)
        return parser
    
    def _build_subcommand_parser(self, parser, possible_group):
        """Build parser with subcommands."""
        subparsers = parser.add_subparsers(dest="command")
        
        # Check if this is a known group
        if self.resolver.is_group_command(possible_group):
            self._add_group_commands(parser, subparsers, possible_group)
            del sys.argv[1]  # Remove group from argv
        else:
            self._add_all_commands(subparsers)
            
        return parser
    
    def _add_group_commands(self, parser, subparsers, group):
        """Add all commands from a specific group."""
        parser.sub_command = group
        for fn_name, fn in self.resolver.get_group_functions(group):
            add_command(subparsers, fn_name, fn)
    
    def _add_all_commands(self, subparsers):
        """Add all commands, organized by groups."""
        # First, organize commands by group
        group_fn_names = defaultdict(list)
        for (group, fn_name), (_decorated_fn, fn) in sorted(
            self.fn_registry.items(), key=lambda x: (x[0] == "info", x[0])
        ):
            if group:
                group_fn_names[group].append(fn_name)
            else:
                add_command(subparsers, fn_name, fn)
        
        # Then add grouped commands
        for group, fn_names in group_fn_names.items():
            if fn_names:
                self._add_subgroup(subparsers, group, fn_names)
    
    def _add_subgroup(self, subparsers, group, fn_names):
        """Add a subgroup with its commands."""
        group_parser = subparsers.add_parser(group, help=f"SUBCOMMAND -> {', '.join(sorted(fn_names))}")
        group_subparsers = group_parser.add_subparsers(dest=group + "_command")
        
        for (fn_group, fn_name), (_decorated_fn, fn) in self.fn_registry.items():
            if fn_group == group:
                add_command(group_subparsers, fn_name, fn)