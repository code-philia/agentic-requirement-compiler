import os
import re
import aiofiles
from utils import get_abs_path

async def grep_search_impl(pattern: str, dir_path: str = ".") -> str:
    """Search for a regex pattern in the contents of files within a directory"""
    abs_dir = get_abs_path(dir_path)
    results = []
    try:
        # compile regex pattern
        regex = re.compile(pattern)
        
        for root, _, files in os.walk(abs_dir):
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