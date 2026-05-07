
read_file_schema = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file. Supports reading the whole file or a specific range of lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative or absolute path to the file."
                },
                "start_line": {
                    "type": "integer",
                    "description": "The line number to start reading from (1-based). Optional."
                },
                "end_line": {
                    "type": "integer",
                    "description": "The line number to stop reading at (1-based, inclusive). Optional."
                }
            },
            "required": ["path"]
        }
    }
}

write_file_schema = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file (overwrites entire file). Automatically creates directories.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file."
                },
                "content": {
                    "type": "string",
                    "description": "The complete content to write."
                }
            },
            "required": ["path", "content"]
        }
    }
}

delete_file_schema = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "Delete a file from the filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file to delete."
                }
            },
            "required": ["path"]
        }
    }
}

insert_lines_schema = {
    "type": "function",
    "function": {
        "name": "insert_lines",
        "description": "Insert content into a file at a specific line number.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file."
                },
                "line_number": {
                    "type": "integer",
                    "description": "The line number to insert at (1-based). Content will be inserted BEFORE this line."
                },
                "content": {
                    "type": "string",
                    "description": "The content to insert."
                }
            },
            "required": ["path", "line_number", "content"]
        }
    }
}

replace_lines_schema = {
    "type": "function",
    "function": {
        "name": "replace_lines",
        "description": "Replace a range of lines in a file with new content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file."
                },
                "start_line": {
                    "type": "integer",
                    "description": "The starting line number to replace (1-based)."
                },
                "end_line": {
                    "type": "integer",
                    "description": "The ending line number to replace (1-based, inclusive)."
                },
                "content": {
                    "type": "string",
                    "description": "The new content to replace the specified lines with."
                }
            },
            "required": ["path", "start_line", "end_line", "content"]
        }
    }
}

list_directory_schema = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "List all files and subdirectories in a given directory path up to a specified depth.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the directory."
                },
                "depth": {
                    "type": "integer",
                    "description": "The maximum depth to expand subdirectories. Default is 3. Optional."
                }
            },
            "required": ["path"]
        }
    }
}

grep_search_schema = {
    "type": "function",
    "function": {
        "name": "grep_search",
        "description": "Search for a regex pattern in the contents of files within a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex pattern to search for."},
                "dir_path": {"type": "string", "description": "The directory to search in. Default is current directory '.'"}
            },
            "required": ["pattern"]
        }
    }
}

search_interfaces_by_keyword_schema = {
    "type": "function",
    "function": {
        "name": "search_interfaces_by_keyword",
        "description": "Search for already designed/implemented interfaces by keyword in their name or description. Use this to find existing reusable logic (e.g., 'auth', 'database').",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "The keyword to search for."},
                "limit": {"type": "integer", "description": "Maximum number of results to return. Default is 10."}
            },
            "required": ["keyword"]
        }
    }
}

search_interfaces_by_relation_schema = {
    "type": "function",
    "function": {
        "name": "search_interfaces_by_relation",
        "description": "Find interfaces belonging to related requirement nodes (parent, children, siblings, dependencies). Use this to understand the immediate architectural context.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The ID of the current requirement node."},
                "relation_type": {
                    "type": "string", 
                    "enum": ["parent", "children", "siblings", "dependencies", "all"],
                    "description": "The type of relation to search for. Default is 'all'."
                }
            },
            "required": ["node_id"]
        }
    }
}

find_interface_impacts_schema = {
    "type": "function",
    "function": {
        "name": "find_interface_impacts",
        "description": "Find all other interfaces that call the given interface. Use this before modifying an existing reused interface to understand the blast radius and ensure you update callers if necessary.",
        "parameters": {
            "type": "object",
            "properties": {
                "interface_id": {"type": "string", "description": "The ID of the interface to check."}
            },
            "required": ["interface_id"]
        }
    }
}

get_node_relations_schema = {
    "type": "function",
    "function": {
        "name": "get_node_relations",
        "description": "Get the parent and children nodes for a given requirement node, along with their designed interfaces. Useful to understand the direct upstream/downstream context.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "The ID of the requirement node."}
            },
            "required": ["node_id"]
        }
    }
}

add_todo_schema = {
    "type": "function",
    "function": {
        "name": "add_todo",
        "description": "Add a new pending task to the project's TODO list.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string", 
                    "description": "Clear description of the task to add."
                }
            },
            "required": ["task_description"]
        }
    }
}

list_todos_schema = {
    "type": "function",
    "function": {
        "name": "list_todos",
        "description": "List all current TODOs along with their index numbers.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}

check_todo_schema = {
    "type": "function",
    "function": {
        "name": "check_todo",
        "description": "Mark a specific task as completed.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_index": {
                    "type": "integer", 
                    "description": "The integer index of the task to mark as completed."
                }
            },
            "required": ["task_index"]
        }
    }
}

clear_todos_schema = {
    "type": "function",
    "function": {
        "name": "clear_todos",
        "description": "Clean up the TODO list.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear_all": {
                    "type": "boolean", 
                    "description": "If true, deletes all tasks. If false, only deletes tasks marked as completed."
                }
            },
            "required": ["clear_all"]
        }
    }
}

execute_command_schema = {
    "type": "function",
    "function": {
        "name": "execute_command",
        "description": "Execute a shell command in the project directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."},
                "cwd": {"type": "string", "description": "Working directory. Default is '.'"}
            },
            "required": ["command"]
        }
    }
}

run_tests_schema = {
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": "Run the test suite using the project's testing frameworks. Uses Vitest/Playwright for web projects and Gradle for Android projects.",
        "parameters": {
            "type": "object",
            "properties": {
                "test_type": {"type": "string", "enum": ["unit", "integration", "e2e"], "description": "Type of test to run."},
                "test_file_path": {"type": "string", "description": "Optional specific test file to run."}
            },
            "required": ["test_type"]
        }
    }
}

run_build_schema = {
    "type": "function",
    "function": {
        "name": "run_build",
        "description": "Run the build process for the current project to check for compilation errors. Uses npm for web projects and Gradle for Android projects.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
}
