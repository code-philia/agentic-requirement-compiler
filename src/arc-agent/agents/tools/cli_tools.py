import asyncio
import os
import re

# Import get_abs_path from utils to ensure we use the same WORKSPACE_ROOT
from utils import get_abs_path, get_app_type

async def execute_command_impl(command: str, cwd: str = ".", timeout: float = 30.0) -> str:
    """run a shell command in the project directory"""

    # Resolve the cwd relative to the WORKSPACE_ROOT
    abs_cwd = get_abs_path(cwd)

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=abs_cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"}
        )

        # Timeout mechanism to prevent hanging commands
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        output = stdout.decode('utf-8', errors='replace')
        error = stderr.decode('utf-8', errors='replace')

        result = f"Exit Code: {process.returncode}\n"
        if output: result += f"STDOUT:\n{output}\n"
        if error: result += f"STDERR:\n{error}\n"

        # Truncate long outputs to prevent token explosion
        if len(result) > 4000:
            result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]

        return result
    except asyncio.TimeoutError:
        process.kill()
        return f"Command timed out after {timeout} seconds. If you started a server, make sure to run it in background or it will block the execution."
    except Exception as e:
        return f"Execution failed: {str(e)}"


# ============================================================
# Web-specific test/build implementations
# ============================================================

async def _run_tests_web(test_type: str, test_file_path: str = "") -> str:
    """Run tests for web projects using Vitest (unit/integration) and Playwright (E2E)."""
    servers_process = None
    if test_type.lower() == "e2e":
        backend_cmd = "npm run dev"
        backend_cwd = get_abs_path("./backend")
        frontend_cmd = "npm run dev"
        frontend_cwd = get_abs_path("./frontend")

        try:
            backend_process = await asyncio.create_subprocess_shell(
                backend_cmd, cwd=backend_cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            frontend_process = await asyncio.create_subprocess_shell(
                frontend_cmd, cwd=frontend_cwd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )

            await asyncio.sleep(5)
            servers_process = (backend_process, frontend_process)
        except Exception as e:
            return f"Failed to start servers for E2E testing: {str(e)}"

    if test_type.lower() in ["unit", "integration"]:
        cmd = f"npx vitest run {test_file_path}" if test_file_path else "npx vitest run"
        cwd = "./backend"
    elif test_type.lower() == "e2e":
        cmd = f"npx playwright test {test_file_path}" if test_file_path else "npx playwright test"
        cwd = "./backend"
    else:
        return "Unknown test type. Must be 'unit', 'integration', or 'e2e'."

    result = await execute_command_impl(cmd, cwd=cwd)

    if servers_process:
        backend_process, frontend_process = servers_process
        try:
            backend_process.terminate()
            frontend_process.terminate()
        except:
            pass

    return result


async def _run_build_web() -> str:
    """Run build for web projects (frontend + backend)."""
    frontend_result = await execute_command_impl("npm run build", cwd="./frontend")
    backend_result = await execute_command_impl("npm run build --if-present", cwd="./backend")
    return f"=== Frontend Build Result ===\n{frontend_result}\n\n=== Backend Build Result ===\n{backend_result}"


# ============================================================
# Android-specific test/build implementations
# ============================================================

def _android_file_to_test_class(file_path: str) -> str:
    """Convert an Android test file path to a fully-qualified class name for Gradle --tests filter.

    Example: 'app/src/test/java/com/example/template/UserServiceTest.java'
             -> 'com.example.template.UserServiceTest'
    """
    parts = file_path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part == "java" and i + 1 < len(parts):
            class_parts = parts[i + 1:]
            if class_parts:
                class_parts[-1] = class_parts[-1].replace(".java", "").replace(".kt", "")
            return ".".join(class_parts)
    return file_path  # Fallback: return raw path


def _gradlew_cmd() -> str:
    """Return the correct Gradle wrapper invocation for the current OS."""
    if os.name == "nt":
        return "cmd /c gradlew.bat"
    return "./gradlew"


async def _run_tests_android(test_type: str, test_file_path: str = "") -> str:
    """Run tests for Android projects using Gradle.

    - Unit/Integration/E2E: ./gradlew testDebugUnitTest (app/src/test/)
    - All tests run on JVM via Robolectric (no device/emulator required).
    """
    gradlew = _gradlew_cmd()

    # All test types run on JVM via testDebugUnitTest
    # --info ensures test execution results are printed to console
    cmd = f"{gradlew} testDebugUnitTest --info"
    if test_file_path:
        test_class = _android_file_to_test_class(test_file_path)
        cmd += f' --tests "{test_class}"'
    timeout = 180.0

    result = await execute_command_impl(cmd, cwd=".", timeout=timeout)

    # Extract only test-relevant lines from --info output to avoid overwhelming context
    lines = result.split('\n')
    relevant = []
    for line in lines:
        # Keep: test results, failures, errors, BUILD, Exit Code, test counts
        if any(kw in line for kw in [
            'PASSED', 'FAILED', 'SKIPPED', 'Test ', 'tests ', 'Build ',
            'BUILD ', 'Exit Code', 'STDOUT', 'STDERR',
            'Exception', 'Error', 'error:', 'Caused by',
            'at ',  # stack trace lines
        ]):
            relevant.append(line)

    if relevant and len(relevant) < len(lines) * 0.5:
        # If we filtered significantly, return the condensed version
        return '\n'.join(relevant)
    return result


async def _run_build_android() -> str:
    """Run build for Android projects using Gradle."""
    gradlew = _gradlew_cmd()
    result = await execute_command_impl(f"{gradlew} assembleDebug", cwd=".", timeout=180.0)
    return f"=== Android Build Result ===\n{result}"


# ============================================================
# Unified dispatchers
# ============================================================

async def run_tests_impl(test_type: str, test_file_path: str = "") -> str:
    """Run the test suite using the project's testing frameworks."""
    app_type = get_app_type()

    if app_type == "android":
        return await _run_tests_android(test_type, test_file_path)
    else:
        return await _run_tests_web(test_type, test_file_path)


async def run_build_impl() -> str:
    """Run the build process for the current project to check for compilation errors."""
    app_type = get_app_type()

    if app_type == "android":
        return await _run_build_android()
    else:
        return await _run_build_web()
