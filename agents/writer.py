from core import get_time, list_files, read_file, write_file

PROVIDER = "anthropic"
SYSTEM = "You write and edit files in the working directory. Say which files you changed."
TOOLS = [get_time, list_files, read_file, write_file]
