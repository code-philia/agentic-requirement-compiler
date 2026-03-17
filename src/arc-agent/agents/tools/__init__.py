from .file_tools import *
from .todo_tools import *
from .search_tools import *
from .cli_tools import *

from .tool_schema import *

# Export set_workspace_root for initializing the tools context
__all__ = [
    "TOOL_REGISTRY",
    "set_workspace_root"
]

TOOL_REGISTRY = {
    "read_file": {
        "schema": read_file_schema,
        "func": read_file_impl
    },
    "write_file": {
        "schema": write_file_schema,
        "func": write_file_impl
    },
    "delete_file": {
        "schema": delete_file_schema,
        "func": delete_file_impl
    },
    "insert_lines": {
        "schema": insert_lines_schema,
        "func": insert_lines_impl
    },
    "replace_lines": {
        "schema": replace_lines_schema,
        "func": replace_lines_impl
    },
    "list_directory": {
        "schema": list_directory_schema,
        "func": list_directory_impl
    },
    "add_todo": {
        "schema": add_todo_schema,
        "func": add_todo_impl
    },
    "list_todos": {
        "schema": list_todos_schema,
        "func": list_todos_impl
    },
    "check_todo": {
        "schema": check_todo_schema,
        "func": check_todo_impl
    },
    "clear_todos": {
        "schema": clear_todos_schema,
        "func": clear_todos_impl
    },
    "grep_search": {
        "schema": grep_search_schema,
        "func": grep_search_impl
    },
    "run_tests": {
        "schema": run_tests_schema,
        "func": run_tests_impl
    },
    "execute_command": {
        "schema": execute_command_schema,
        "func": execute_command_impl
    }
}
