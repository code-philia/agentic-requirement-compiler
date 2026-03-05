import os
import aiofiles

async def read_file_impl(path: str) -> str:
    """Real file-reading tool implementation"""
    try:
        if not os.path.exists(path):
            return f"Error: File not found at {path}"
            
        async with aiofiles.open(path, mode='r', encoding='utf-8') as f:
            content = await f.read()
            # In a real scenario, truncate overly large files here to avoid token explosion
            return content
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
