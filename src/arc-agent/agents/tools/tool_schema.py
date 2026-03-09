
read_file_schema = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file in the project to gather more context. Returns the raw text of the file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative or absolute path to the file to read. E.g., 'src/main.py'"
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
        "description": "Write content to a file. Automatically creates directories if they don't exist. Use this to save your designs, tests, or code.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The relative or absolute path to the file. E.g., 'src/api/routes.py' or 'docs/design.md'"
                },
                "content": {
                    "type": "string",
                    "description": "The complete text/code content to write into the file."
                }
            },
            "required": ["path", "content"]
        }
    }
}

list_directory_schema = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": "List all files and subdirectories in a given directory path. Helps to understand the project structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path to the directory. E.g., '.' for root, or 'src/'"
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
        "description": "List all current TODOs along with their index numbers. Use this before trying to check off a task.",
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
        "description": "Mark a specific task as completed. You MUST use list_todos first to get the correct task_index.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_index": {
                    "type": "integer", 
                    "description": "The integer index of the task to mark as completed (e.g., 0, 1, 2)."
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
        "description": "Clean up the TODO list by removing completed tasks, or wiping it entirely.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear_all": {
                    "type": "boolean", 
                    "description": "If true, deletes all tasks. If false, only deletes tasks marked as completed [- [x]]."
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
        "description": "Execute a shell command in the project directory. Use this to install npm packages, run linters, or check env vars.",
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
        "description": "Run the test suite using the project's testing frameworks (Vitest for Unit/Integration, Playwright for E2E).",
        "parameters": {
            "type": "object",
            "properties": {
                "test_type": {"type": "string", "enum": ["unit", "integration", "e2e"], "description": "Type of test to run."},
                "test_file_path": {"type": "string", "description": "Optional specific test file to run (e.g., 'tests/api.test.js'). Leave empty to run all tests of that type."}
            },
            "required": ["test_type"]
        }
    }
}