import os
import aiofiles

async def read_file_impl(path: str, start_line: int = None, end_line: int = None) -> str:
    """Real file-reading tool implementation with line range support"""
    try:
        if not os.path.exists(path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
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
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        async with aiofiles.open(path, mode='w', encoding='utf-8') as f:
            await f.write(content)
        return f"Success: File successfully written to {path}"
    except Exception as e:
        return f"Error writing file {path}: {str(e)}"

async def delete_file_impl(path: str) -> str:
    """Delete a file"""
    try:
        if not os.path.exists(path):
            return f"Error: File not found at {path}"
        os.remove(path)
        return f"Success: Deleted file {path}"
    except Exception as e:
        return f"Error deleting file {path}: {str(e)}"

async def insert_lines_impl(path: str, line_number: int, content: str) -> str:
    """Insert content at a specific line number"""
    try:
        if not os.path.exists(path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
            
        if line_number < 1:
            line_number = 1
        if line_number > len(lines) + 1:
            line_number = len(lines) + 1
            
        # Ensure content ends with newline if it's multiple lines or meant to be a line
        if not content.endswith('\n'):
            content += '\n'
            
        lines.insert(line_number - 1, content)
        
        async with aiofiles.open(path, mode='w', encoding='utf-8') as f:
            await f.writelines(lines)
            
        return f"Success: Inserted content at line {line_number} in {path}"
    except Exception as e:
        return f"Error inserting lines in {path}: {str(e)}"

async def replace_lines_impl(path: str, start_line: int, end_line: int, content: str) -> str:
    """Replace a range of lines with new content"""
    try:
        if not os.path.exists(path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
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
        
        async with aiofiles.open(path, mode='w', encoding='utf-8') as f:
            await f.writelines(new_lines)
            
        return f"Success: Replaced lines {start_line}-{end_line} in {path}"
    except Exception as e:
        return f"Error replacing lines in {path}: {str(e)}"

async def list_directory_impl(path: str) -> str:
    """List all files and folders under the given directory"""
    try:
        if not os.path.exists(path):
            return f"Error: Directory not found at {path}"
        if not os.path.isdir(path):
            return f"Error: {path} is not a directory"
        
        items = os.listdir(path)
        return f"Contents of {path}:\n" + "\n".join(f"- {item}" for item in items)
    except Exception as e:
        return f"Error listing directory {path}: {str(e)}"
