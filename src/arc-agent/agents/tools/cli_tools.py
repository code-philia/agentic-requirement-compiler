import asyncio
import os
import re

# Import get_abs_path from utils to ensure we use the same WORKSPACE_ROOT
from utils import get_abs_path

async def execute_command_impl(command: str, cwd: str = ".") -> str:
    """run a shell command in the project directory"""
    
    # Resolve the cwd relative to the WORKSPACE_ROOT
    abs_cwd = get_abs_path(cwd)
    
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
            cwd=abs_cwd
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
    
    # Pre-start frontend and backend servers if E2E testing
    servers_process = None
    if test_type.lower() == "e2e":
        # E2E tests need both frontend and backend running.
        # We assume there's a script or we can start them in the background.
        # Start backend
        backend_cmd = "npm run dev"
        backend_cwd = get_abs_path("./backend")
        frontend_cmd = "npm run dev"
        frontend_cwd = get_abs_path("./frontend")
        
        # We start them using asyncio subprocess and keep track of them
        try:
            backend_process = await asyncio.create_subprocess_shell(
                backend_cmd, cwd=backend_cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            frontend_process = await asyncio.create_subprocess_shell(
                frontend_cmd, cwd=frontend_cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            
            # Give servers a few seconds to start up
            await asyncio.sleep(5)
            servers_process = (backend_process, frontend_process)
        except Exception as e:
            return f"Failed to start servers for E2E testing: {str(e)}"

    if test_type.lower() in ["unit", "integration"]:
        # If test_file_path contains multiple files (space separated), it will just pass them to vitest
        cmd = f"npx vitest run {test_file_path}" if test_file_path else "npx vitest run"
        cwd = "./backend" 
    elif test_type.lower() == "e2e":
        cmd = f"npx playwright test {test_file_path}" if test_file_path else "npx playwright test"
        cwd = "./backend" # E2E test folder is inside backend per your spec
    else:
        return "Unknown test type. Must be 'unit', 'integration', or 'e2e'."

    result = await execute_command_impl(cmd, cwd=cwd)

    # Cleanup servers if we started them
    if servers_process:
        backend_process, frontend_process = servers_process
        try:
            backend_process.terminate()
            frontend_process.terminate()
        except:
            pass

    return result

async def run_build_impl() -> str:
    """Run build for frontend and check backend compilation."""
    # Build Frontend
    frontend_result = await execute_command_impl("npm run build", cwd="./frontend")
    
    # Check Backend (Node.js doesn't have a build step by default, but we can do a syntax check or npm run build if exists)
    backend_result = await execute_command_impl("npm run build --if-present", cwd="./backend")
    
    return f"=== Frontend Build Result ===\n{frontend_result}\n\n=== Backend Build Result ===\n{backend_result}"
