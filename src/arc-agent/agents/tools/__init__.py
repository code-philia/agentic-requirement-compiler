from .file_tools import *
from .todo_tools import *
from .search_tools import *
from .cli_tools import *

from .tool_schema import *
from utils import set_workspace_root, set_app_type

# Export set_workspace_root for initializing the tools context
__all__ = [
    "TOOL_REGISTRY",
    "set_workspace_root",
    "set_app_type"
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
    "edit_file": {
        "schema": edit_file_schema,
        "func": edit_file_impl
    },
    "delete_file": {
        "schema": delete_file_schema,
        "func": delete_file_impl
    },
    "list_directory": {
        "schema": list_directory_schema,
        "func": list_directory_impl
    },
    "glob": {
        "schema": glob_schema,
        "func": glob_impl
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
    "grep": {
        "schema": grep_schema,
        "func": grep_impl
    },
    "search_interfaces_by_keyword": {
        "schema": search_interfaces_by_keyword_schema,
        "func": search_interfaces_by_keyword_impl
    },
    "search_interfaces_by_relation": {
        "schema": search_interfaces_by_relation_schema,
        "func": search_interfaces_by_relation_impl
    },
    "find_interface_impacts": {
        "schema": find_interface_impacts_schema,
        "func": find_interface_impacts_impl
    },
    "get_node_relations": {
        "schema": get_node_relations_schema,
        "func": get_node_relations_impl
    },
    "run_tests": {
        "schema": run_tests_schema,
        "func": run_tests_signal_impl
    },
    "run_build": {
        "schema": run_build_schema,
        "func": run_build_impl
    },
    "execute_command": {
        "schema": execute_command_schema,
        "func": execute_command_impl
    }
}
