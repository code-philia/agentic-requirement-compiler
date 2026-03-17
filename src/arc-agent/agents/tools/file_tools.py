import os
import aiofiles

# Global variable to store the workspace root
WORKSPACE_ROOT = os.getcwd() 

def set_workspace_root(path: str):
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = os.path.abspath(path)

def get_abs_path(rel_path: str) -> str:
    """Convert a relative path to an absolute path within the workspace"""
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(WORKSPACE_ROOT, rel_path)

async def read_file_impl(path: str, start_line: int = None, end_line: int = None) -> str:
    """Real file-reading tool implementation with line range support"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(abs_path, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
            
        total_lines = len(lines)
        
        if start_line is None:
            start_line = 1
        if end_line is None:
            end_line = total_lines
            
        # Validate ranges
        if start_line < 1: start_line = 1
        if end_line > total_lines: end_line = total_lines
        if start_line > end_line:
            return f"Error: start_line ({start_line}) cannot be greater than end_line ({end_line})"
            
        # Adjust for 0-based indexing
        selected_lines = lines[start_line-1 : end_line]
        return "".join(selected_lines)
            
    except Exception as e:
        return f"Error reading file {path}: {str(e)}"
    
async def write_file_impl(path: str, content: str) -> str:
    """Write to a file, automatically creating directories if they do not exist"""
    abs_path = get_abs_path(path)
    try:
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        async with aiofiles.open(abs_path, mode='w', encoding='utf-8') as f:
            await f.write(content)
        return f"Success: File successfully written to {path}"
    except Exception as e:
        return f"Error writing file {path}: {str(e)}"

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

async def insert_lines_impl(path: str, line_number: int, content: str) -> str:
    """Insert content at a specific line number"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(abs_path, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
            
        if line_number < 1:
            line_number = 1
        if line_number > len(lines) + 1:
            line_number = len(lines) + 1
            
        # Ensure content ends with newline if it's multiple lines or meant to be a line
        if not content.endswith('\n'):
            content += '\n'
            
        lines.insert(line_number - 1, content)
        
        async with aiofiles.open(abs_path, mode='w', encoding='utf-8') as f:
            await f.writelines(lines)
            
        return f"Success: Inserted content at line {line_number} in {path}"
    except Exception as e:
        return f"Error inserting lines in {path}: {str(e)}"

async def replace_lines_impl(path: str, start_line: int, end_line: int, content: str) -> str:
    """Replace a range of lines with new content"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(abs_path, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
            
        total_lines = len(lines)
        if start_line < 1: start_line = 1
        if end_line > total_lines: end_line = total_lines
        if start_line > end_line:
             return f"Error: start_line ({start_line}) cannot be greater than end_line ({end_line})"

        # Ensure content ends with newline if needed
        if not content.endswith('\n'):
            content += '\n'
            
        # Replace logic:
        # Keep lines before start_line
        # Insert new content
        # Keep lines after end_line
        
        new_lines = lines[:start_line-1] + [content] + lines[end_line:]
        
        async with aiofiles.open(abs_path, mode='w', encoding='utf-8') as f:
            await f.writelines(new_lines)
            
        return f"Success: Replaced lines {start_line}-{end_line} in {path}"
    except Exception as e:
        return f"Error replacing lines in {path}: {str(e)}"

async def list_directory_impl(path: str) -> str:
    """List all files and folders under the given directory"""
    abs_path = get_abs_path(path)
    try:
        if not os.path.exists(abs_path):
            return f"Error: Directory not found at {path}"
        if not os.path.isdir(abs_path):
            return f"Error: {path} is not a directory"
        
        items = os.listdir(abs_path)
        return f"Contents of {path}:\n" + "\n".join(f"- {item}" for item in items)
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"
