from .file_tools import *
from .tool_schema import *

TOOL_REGISTRY = {
    "read_file": {
        "schema": read_file_schema,
        "func": read_file_impl
    },
    "write_file": {
        "schema": write_file_schema,
        "func": write_file_impl
    },
    "list_directory": {
        "schema": list_directory_schema,
        "func": list_directory_impl
    }
}