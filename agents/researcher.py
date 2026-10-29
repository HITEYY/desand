from core import list_files, read_file

PROVIDER = "opencode-go"
MODEL = "kimi-k3"
SYSTEM = "You investigate files in the working directory and report findings concisely. Never modify files."
TOOLS = [list_files, read_file]
