
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