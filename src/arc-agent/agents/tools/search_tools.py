import os
import re
import aiofiles

async def grep_search_impl(pattern: str, dir_path: str = ".") -> str:
    """ search files in the specified directory by regex pattern """
    results = []
    try:
        for root, _, files in os.walk(dir_path):
            for file in files:
                if file.endswith(('.py', '.js', '.ts', '.yaml', '.md')): # TODO: filter by file type
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            for i, line in enumerate(f):
                                if re.search(pattern, line):
                                    results.append(f"{file_path}:{i+1}: {line.strip()}")
                    except Exception:
                        pass
        return "\n".join(results) if results else "No matches found."
    except Exception as e:
        return f"Grep search error: {str(e)}"