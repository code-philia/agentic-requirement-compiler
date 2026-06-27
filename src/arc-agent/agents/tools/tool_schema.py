
read_file_schema = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file. Returns content with line numbers in 'cat -n' format (line_number<TAB>content). By default reads up to 2000 lines from the beginning. Use offset and limit for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative or absolute path to the file."
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-based). Optional, defaults to 1."
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read. Optional, defaults to entire file."
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
        "description": "Write content to a file (overwrites entire file). Automatically creates directories. Use this for creating new files or complete rewrites. For modifying existing files, prefer edit_file.",
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

edit_file_schema = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Perform exact string replacement in a file. The old_string must match exactly (including whitespace and indentation). When editing text from read_file output, preserve exact indentation AFTER the line number prefix (line_number<TAB>). Never include the line number prefix in old_string or new_string.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the file."
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact string to find and replace. Must match exactly including all whitespace."
                },
                "new_string": {
                    "type": "string",
                    "description": "The string to replace with."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "If true, replace all occurrences. If false (default), require unique match. Use true for renaming variables across the file."
                }
            },
            "required": ["path", "old_string", "new_string"]
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

glob_schema = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "Fast file pattern matching tool. Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\". Returns matching file paths sorted by modification time. Use this when you need to find files by name patterns.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against (e.g., \"**/*.py\", \"src/**/*.java\")."
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. If not specified, the current working directory will be used. Optional."
                }
            },
            "required": ["pattern"]
        }
    }
}

grep_schema = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "A powerful search tool for finding regex patterns in file contents. Supports full regex syntax, multiple output modes, and filtering. ALWAYS use this tool for content search - NEVER use grep/rg as shell commands.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regex pattern to search for (e.g., 'log.*Error', 'function\\s+\\w+')."
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in. Defaults to current working directory. Optional."
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g., '*.js', '**/*.tsx'). Optional."
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output mode: 'content' shows matching lines, 'files_with_matches' shows only file paths (default), 'count' shows match counts per file."
                },
                "context": {
                    "type": "integer",
                    "description": "Number of lines to show before and after each match (content mode only). Optional."
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case insensitive search. Default is false. Optional."
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Limit output to first N results. Defaults to 250. Pass 0 for unlimited (use sparingly). Optional."
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip first N results before applying head_limit. Defaults to 0. Optional."
                },
                "multiline": {
                    "type": "boolean",
                    "description": "Enable multiline mode where . matches newlines and patterns can span lines. Default is false. Optional."
                }
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
        "description": "Signal that the system should execute the current batch of target tests. This tool takes no arguments and does not execute tests directly.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
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
