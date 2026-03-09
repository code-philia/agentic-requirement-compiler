import asyncio
import os
import re

async def execute_command_impl(command: str, cwd: str = ".") -> str:
    """run a shell command in the project directory"""
    
    # dangerous command patterns
    # dangerous_patterns = [r'\brm\b\s+-rf', r'\bmv\b.*\/dev\/null', r'\bmkfs\b']
    
    # for pattern in dangerous_patterns:
    #     if re.search(pattern, command):
    #         # TODO: In the future, we should implement a confirmation mechanism
    #         return f"BLOCKED: The command '{command}' is considered dangerous and requires user confirmation. Please use `write_file` to empty files or ask the human user."

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
        
        # Timeout mechanism to prevent hanging commands
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        
        output = stdout.decode()
        error = stderr.decode()
        
        result = f"Exit Code: {process.returncode}\n"
        if output: result += f"STDOUT:\n{output}\n"
        if error: result += f"STDERR:\n{error}\n"
        
        # Truncate long outputs to prevent token explosion
        if len(result) > 4000:
            result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]
            
        return result
    except asyncio.TimeoutError:
        process.kill()
        return "Command timed out after 30 seconds. If you started a server, make sure to run it in background or it will block the execution."
    except Exception as e:
        return f"Execution failed: {str(e)}"

async def run_tests_impl(test_type: str, test_file_path: str = "") -> str:
    """Run tests using Vitest for unit/integration tests, and Playwright for E2E tests."""
    
    if test_type.lower() in ["unit", "integration"]:
        cmd = f"npx vitest run {test_file_path}" if test_file_path else "npx vitest run"
        cwd = "./backend" 
    elif test_type.lower() == "e2e":
        cmd = f"npx playwright test {test_file_path}" if test_file_path else "npx playwright test"
        cwd = "./backend" # E2E test folder is inside backend per your spec
    else:
        return "Unknown test type. Must be 'unit', 'integration', or 'e2e'."

    return await execute_command_impl(cmd, cwd=cwd)