"""Output management and formatting for cliche CLI."""

import contextlib
import re
import sys
import argparse


class ColorManager:
    """Manages color codes and colored output for the CLI."""
    
    COLORS = {
        "RED": "1;31", 
        "GREEN": "1;32", 
        "YELLOW": "1;33", 
        "BLUE": "1;36"
    }
    
    @classmethod
    def colorize(cls, text: str, color: str) -> str:
        """Apply color to text using ANSI codes."""
        if color in cls.COLORS:
            return f"\x1b[{cls.COLORS[color]}m{text}\x1b[0m"
        return text
    
    @classmethod
    def write_colored(cls, text: str, color: str = None, file=None) -> None:
        """Write colored text to file."""
        if file is None:
            file = sys.stderr
        
        if color:
            text = cls.colorize(text.strip(), color)
            file.write(text + "\n")
        else:
            file.write(text)


class MessageFormatter:
    """Handles complex message formatting and regex transformations."""
    
    def __init__(self, prog_name: str, module_name: str = None, python_310_or_higher: bool = True):
        self.prog_name = prog_name
        self.module_name = module_name
        self.python_310_or_higher = python_310_or_higher
    
    def format_message(self, message: str) -> str:
        """Apply all message formatting transformations."""
        if not message:
            return message
            
        # Capitalize first letter
        message = message[0].upper() + message[1:]
        
        # Replace program name if module_name is set
        if self.module_name:
            repl = " ".join(["cliche " + self.module_name] + self.prog_name.split()[1:])
            message = message.replace(self.prog_name, repl)
            
        return message
    
    def format_help_message(self, message: str, sub_command: str = None) -> str:
        """Format help messages with colors and structure."""
        message = message.strip()
        
        if len(self.prog_name.split()) > 1:
            message = message.replace("positional arguments:", "POSITIONAL ARGUMENTS:")
        else:
            message = self._format_positional_arguments(message)
        
        message = self._format_subgroups(message)
        message = self._format_options(message)
        message = self._apply_help_colors(message)
        
        if hasattr(self, 'sub_command') and sub_command:
            message = message.replace(self.prog_name, sub_command)
            
        return message
    
    def _format_positional_arguments(self, message: str) -> str:
        """Format positional arguments section."""
        # Check if first is a positional arg or actual command
        ms = re.findall(r"positional arguments:\s*\{([^}]+)\}", message, flags=re.DOTALL)
        if ms:
            ms_content = ms[0]
            first_start = message.index("positional arguments")
            start = first_start + message[first_start:].index(ms_content) + len(ms_content)
            end_pattern = "options:" if self.python_310_or_higher else "optional "
            
            try:
                end = message.index(end_pattern)
                if all(x in message[start:end] for x in ms_content.split(",")):
                    # Remove the line that shows the possible commands
                    message = re.sub(
                        r"positional arguments:\s*\{[^}]+\}",
                        "COMMANDS:\n",
                        message,
                        flags=re.DOTALL,
                    )
            except ValueError:
                pass  # end_pattern not found
        
        return message.replace("positional arguments:", "POSITIONAL ARGUMENTS:")
    
    def _format_subgroups(self, message: str) -> str:
        """Format subcommand groups."""
        ind = message.find("SUBCOMMAND -> ")
        if ind == -1:
            return message
        z = message[:ind].rfind("\n")
        return message[:z] + "\n\nSUBCOMMANDS:" + message[z:].replace("SUBCOMMAND -> ", "")
    
    def _format_options(self, message: str) -> str:
        """Format options section."""
        options_text = "options" if self.python_310_or_higher else "optional arguments"
        return message.replace(options_text, "OPTIONS:")
    
    def _apply_help_colors(self, message: str) -> str:
        """Apply color formatting to help text."""
        lines = message.split("\n")
        inds = 1
        
        # Find the end of the header section
        for i in range(1, len(lines)):
            if re.search(r"^[A-Z]", lines[i]):
                break
            if re.search(r" +([{]|[.][.][.])", lines[i]):
                lines[i] = None
            else:
                inds += 1
        
        # Color the header section
        header_lines = [x for x in lines[:inds] if x is not None]
        if header_lines:
            colored_header = ColorManager.colorize("\n".join(header_lines), "BLUE")
            lines = [colored_header] + lines[inds:]
        
        # Rejoin message for further processing
        message = "\n".join([x for x in lines if x is not None])
        
        # Color default values
        message = re.sub(
            r"Default:[^|]+",
            lambda m: ColorManager.colorize(m.group(0), "BLUE"),
            message,
        )
        
        # Color short options
        message = re.sub(
            r"(\n *-[a-zA-Z]) (.+, --)( \[[A-Z0-9. ]+\])?",
            lambda m: ColorManager.colorize(m.group(1), "BLUE") + ", --",
            message
        )
        
        # Color long options  
        message = re.sub(
            r", (--[^ ]+)",
            lambda m: ", " + ColorManager.colorize(m.group(1), "BLUE") + " ",
            message
        )
        
        # Color various help elements
        patterns = [
            r"\n  -h, --help",
            r"\n  \{[^}]+\}",
            r"\n +--[^ ]+",
            r"\n  {1,6}[a-z0-9A-Z_-]+",
        ]
        
        for pattern in patterns:
            message = re.sub(
                pattern,
                lambda m: ColorManager.colorize(m.group(0), "BLUE"),
                message
            )
        
        return message
    
    def format_error_message(self, message: str) -> str:
        """Format error messages."""
        if "unrecognized arguments" in message:
            multiple_args = message.count(" ") > 2
            option_str = "Unknown option" if self.python_310_or_higher else "Unknown optional argument"
            
            type_arg_msg = option_str if "-" in message else "Extra positional argument"
            if multiple_args:
                type_arg_msg += "(s)"
            message = message.replace("unrecognized arguments", type_arg_msg)
        
        return message


class HelpFormatter:
    """Manages help text formatting and display."""
    
    def __init__(self, color_manager: ColorManager, message_formatter: MessageFormatter):
        self.color_manager = color_manager
        self.message_formatter = message_formatter
    
    def print_help(self, parser, file=None):
        """Print formatted help text."""
        if file is None:
            file = sys.stdout
        
        help_text = parser.format_help()
        formatted_help = self.message_formatter.format_help_message(help_text)
        # Message is already colored, just write it directly
        file.write(formatted_help + "\n")


class OutputManager:
    """Coordinates all output operations for the CLI."""
    
    def __init__(self, prog_name: str, module_name: str = None):
        self.prog_name = prog_name
        self.module_name = module_name
        self.color_manager = ColorManager()
        self.message_formatter = MessageFormatter(prog_name, module_name)
        self.help_formatter = HelpFormatter(self.color_manager, self.message_formatter)
    
    def print_help(self, parser, file=None):
        """Print help using the help formatter."""
        self.help_formatter.print_help(parser, file)
    
    def print_error(self, message: str, file=None):
        """Print an error message."""
        formatted_message = self.message_formatter.format_error_message(message)
        self.color_manager.write_colored(formatted_message, "RED", file)
    
    def print_message(self, message: str, color: str = None, file=None):
        """Print a general message."""
        formatted_message = self.message_formatter.format_message(message)
        self.color_manager.write_colored(formatted_message, color, file)


class CleanArgumentParser(argparse.ArgumentParser):
    """A clean ArgumentParser that delegates output operations to OutputManager."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_manager = OutputManager(self.prog)
        self.module_name = False
        self.sub_command = None
    
    def print_help(self, file=None) -> None:
        """Print help using the output manager."""
        self.output_manager.print_help(self, file)
    
    def _print_message(self, message, file=None, color=None) -> None:
        """Print a message using the output manager."""
        if message:
            self.output_manager.print_message(message, color, file)
    
    def exit(self, status=0, message=None) -> None:
        """Exit with optional message."""
        if message:
            self.output_manager.print_error(message)
        sys.exit(status)
    
    def error(self, message) -> None:
        """Handle argument parsing errors with better formatting."""
        formatted_message = self.output_manager.message_formatter.format_error_message(message)
        
        if "unrecognized arguments" in message:
            with contextlib.suppress(SystemExit):
                self.parse_args(sys.argv[1:-1] + ["--help"])
        else:
            self.print_help(sys.stderr)
        
        self.exit(2, formatted_message)