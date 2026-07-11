import os
import aiofiles

TODO_FILE = "TODO.md"

async def add_todo_impl(task_description: str) -> str:
    """Add a new task to the TODO.md file."""
    try:
        async with aiofiles.open(TODO_FILE, mode='a', encoding='utf-8') as f:
            await f.write(f"- [ ] {task_description}\n")
        return f"Successfully added to {TODO_FILE}"
    except Exception as e:
        return f"Failed to add TODO: {str(e)}"

async def list_todos_impl() -> str:
    """Read and return all tasks with their line indices."""
    if not os.path.exists(TODO_FILE):
        return "TODO list is currently empty (file does not exist)."
    try:
        async with aiofiles.open(TODO_FILE, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
            
        if not lines:
            return "TODO list is empty."
        
        # return a formatted string with line indices
        result = "Current TODOs:\n"
        for i, line in enumerate(lines):
            result += f"[{i}] {line.strip()}\n"
        return result
    except Exception as e:
        return f"Failed to read TODOs: {str(e)}"

async def check_todo_impl(task_index: int) -> str:
    """Mark a specific task as completed based on its index."""
    if not os.path.exists(TODO_FILE):
        return "Failed: TODO list does not exist."
    try:
        async with aiofiles.open(TODO_FILE, mode='r', encoding='utf-8') as f:
            lines = await f.readlines()
        
        if task_index < 0 or task_index >= len(lines):
            return f"Failed: Invalid task index {task_index}."
            
        if "- [ ]" in lines[task_index]:
            lines[task_index] = lines[task_index].replace("- [ ]", "- [x]", 1)
            async with aiofiles.open(TODO_FILE, mode='w', encoding='utf-8') as f:
                await f.writelines(lines)
            return f"Successfully checked off task at index {task_index}."
        elif "- [x]" in lines[task_index]:
            return f"Task at index {task_index} is already completed."
        else:
            return f"Task at index {task_index} does not appear to be a valid TODO item (missing '- [ ]')."
    except Exception as e:
        return f"Failed to check TODO: {str(e)}"

async def clear_todos_impl(clear_all: bool = False) -> str:
    """Clear completed tasks, or clear the entire file if clear_all is True."""
    if not os.path.exists(TODO_FILE):
        return "TODO list is already empty."
    try:
        if clear_all:
            async with aiofiles.open(TODO_FILE, mode='w', encoding='utf-8') as f:
                await f.write("")
            return "Successfully cleared ALL tasks from TODO list."
        else:
            async with aiofiles.open(TODO_FILE, mode='r', encoding='utf-8') as f:
                lines = await f.readlines()
            
            # filter out completed tasks
            remaining_lines = [line for line in lines if "- [x]" not in line]
            
            async with aiofiles.open(TODO_FILE, mode='w', encoding='utf-8') as f:
                await f.writelines(remaining_lines)
            return f"Successfully cleared completed tasks. {len(remaining_lines)} tasks remaining."
    except Exception as e:
        return f"Failed to clear TODOs: {str(e)}"