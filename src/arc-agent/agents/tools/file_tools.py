import os
import aiofiles
from utils import get_abs_path

async def read_file_impl(path: str, offset: int = None, limit: int = None) -> str:
    """
    Read file contents with optional line range.
    Returns content with line numbers in 'cat -n' format (line_number<TAB>content).

    Args:
        path: File path (relative or absolute)
        offset: Starting line number (1-based, optional)
        limit: Number of lines to read (optional)
    """
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"

        async with aiofiles.open(abs_path, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()

        total_lines = len(lines)

        # Default: read entire file
        if offset is None:
            offset = 1
        if limit is None:
            limit = total_lines

        # Validate ranges
        if offset < 1:
            offset = 1
        if offset > total_lines:
            return f"Error: offset ({offset}) exceeds file length ({total_lines} lines)"

        # Calculate end line
        end_line = min(offset + limit - 1, total_lines)

        # Format with line numbers (cat -n style: line_number<TAB>content)
        result_lines = []
        for i in range(offset - 1, end_line):
            line_num = i + 1
            result_lines.append(f"{line_num}\t{lines[i]}")

        return "".join(result_lines)

    except Exception as e:
        return f"Error reading file {path}: {str(e)}"
    
_PROTECTED_PATHS = {"app/build.gradle", "build.gradle", "settings.gradle", "gradle.properties", "gradlew", "gradlew.bat"}

async def write_file_impl(path: str, content: str) -> str:
    """Write to a file, automatically creating directories if they do not exist"""
    # Protect build files — test/TDD agents must not edit them
    norm = path.replace("\\", "/").lstrip("./")
    if norm in _PROTECTED_PATHS or norm.endswith("/build.gradle") and "src" not in norm:
        return (
            f"Error: writing to `{path}` is not allowed. "
            "All required build dependencies are pre-declared in the template. "
            "Fix your import path instead of modifying the build file."
        )
    abs_path = get_abs_path(path)
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        async with aiofiles.open(abs_path, mode='w', encoding='utf-8') as f:
            await f.write(content)
        return f"Success: File successfully written to {path}"
    except Exception as e:
        return f"Error writing file {path}: {str(e)}"

async def edit_file_impl(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """
    Perform exact string replacement in a file.

    Args:
        path: File path
        old_string: Exact string to find (must match exactly, including whitespace)
        new_string: String to replace with
        replace_all: If True, replace all occurrences; if False, require unique match

    Returns:
        Success message or error description
    """
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"

        async with aiofiles.open(abs_path, mode='r', encoding='utf-8') as f:
            content = await f.read()

        # Check if old_string exists
        if old_string not in content:
            return f"Error: old_string not found in {path}. Make sure the string matches exactly, including all whitespace and indentation."

        # Count occurrences
        occurrences = content.count(old_string)

        if not replace_all and occurrences > 1:
            return (
                f"Error: old_string appears {occurrences} times in {path}. "
                "Either provide a larger unique string with more context, or set replace_all=true to replace all occurrences."
            )

        # Perform replacement
        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

        async with aiofiles.open(abs_path, mode='w', encoding='utf-8') as f:
            await f.write(new_content)

        if replace_all:
            return f"Success: Replaced {occurrences} occurrence(s) in {path}"
        else:
            return f"Success: Replaced 1 occurrence in {path}"

    except Exception as e:
        return f"Error editing file {path}: {str(e)}"

async def delete_file_impl(path: str) -> str:
    """Delete a file"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"
        os.remove(abs_path)
        return f"Success: Deleted file {path}"
    except Exception as e:
        return f"Error deleting file {path}: {str(e)}"

async def list_directory_impl(path: str, depth: int = 3) -> str:
    """List all files and folders under the given directory up to a specific depth"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: Directory not found at {path}"
        if not os.path.isdir(abs_path):
            return f"Error: {path} is not a directory"
        
        output_lines = [f"Contents of {path} (max depth: {depth}):"]
        
        def traverse(current_path: str, current_depth: int, rel_prefix: str = ""):
            if current_depth > depth:
                return
            
            try:
                items = sorted(os.listdir(current_path))
            except PermissionError:
                output_lines.append(f"- {rel_prefix}[Permission Denied]")
                return
            except Exception as e:
                output_lines.append(f"- {rel_prefix}[Error: {str(e)}]")
                return
                
            _SKIP_DIRS = {
                ".git", ".arc", ".gradle", "build", ".idea",
                "node_modules", ".venv", "venv", "dist", "out", "coverage",
                "__pycache__", "target", ".next", ".nuxt", ".cache", ".turbo",
                ".parcel-cache", "tmp", "temp",
            }
            _SKIP_FILES = {
                ".gitignore", ".DS_Store", "Thumbs.db", "package-lock.json",
                "yarn.lock", "pnpm-lock.yaml", "composer.lock", "Gemfile.lock",
                ".npmrc", ".yarnrc",
            }

            for item in items:
                if item in _SKIP_DIRS or item in _SKIP_FILES:
                    continue
                item_path = os.path.join(current_path, item)
                item_rel_path = f"{rel_prefix}{item}"
                
                if os.path.isdir(item_path):
                    output_lines.append(f"- {item_rel_path}/")
                    traverse(item_path, current_depth + 1, f"{item_rel_path}/")
                else:
                    output_lines.append(f"- {item_rel_path}")
                    
        traverse(abs_path, 1)
        
        return "\n".join(output_lines)
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"
