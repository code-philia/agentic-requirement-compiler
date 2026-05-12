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

    IMPORTANT: Runs subprocess directly (not via execute_command_impl) to get
    FULL output before filtering. execute_command_impl truncates at 4000 chars,
    which destroys critical error info in Gradle's verbose --info output.
    """
    gradlew = _gradlew_cmd()

    # All test types run on JVM via testDebugUnitTest
    # --info ensures test execution results are printed to console
    cmd = f"{gradlew} testDebugUnitTest --info"
    if test_file_path:
        test_class = _android_file_to_test_class(test_file_path)
        cmd += f' --tests "{test_class}"'
    timeout = 180.0

    # Run subprocess directly to get FULL output (no 4000-char truncation)
    abs_cwd = get_abs_path(".")
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=abs_cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"}
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        output = stdout.decode('utf-8', errors='replace')
        error = stderr.decode('utf-8', errors='replace')
        exit_code = process.returncode
    except asyncio.TimeoutError:
        process.kill()
        return f"Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Execution failed: {str(e)}"

    # Parse and filter the --info output to keep only test-relevant information
    # Process stdout and stderr separately for clarity
    all_lines = output.split('\n') + error.split('\n')
    relevant = []
    has_test_result = False  # Track whether we saw any test execution results

    for line in all_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Test execution results (Gradle --info format)
        # e.g., "Gradle Test Run :app:testDebugUnitTest > org.pkg.TestClass > testMethodName PASSED"
        if any(kw in line for kw in ['PASSED', 'FAILED', 'SKIPPED']):
            has_test_result = True
            relevant.append(line)
            continue

        # Test summary lines (e.g., "Test result: 3 tests, 3 passed, 0 failed")
        if any(kw in line.lower() for kw in ['test result:', 'tests,', 'test execution', 'tests found', 'no tests found']):
            has_test_result = True
            relevant.append(line)
            continue

        # Build result
        if 'BUILD ' in line and not stripped.startswith('>'):
            relevant.append(line)
            continue

        # Failure/exception details
        if any(kw in line for kw in ['FAILURE:', 'What went wrong:', 'Execution failed', 'Exception', 'Caused by']):
            relevant.append(line)
            continue

        # Key Android/Robolectric errors that must be visible to TDD Agent
        if any(kw in line for kw in [
            'No instrumentation registered', 'InstrumentationRegistry',
            'IllegalStateException', 'StrictMode', 'not mocked',
            'AndroidRuntimeException', 'RuntimeException'
        ]):
            relevant.append(line)
            has_test_result = True  # These ARE test failures
            continue

        # Stack trace lines (at com.example.Class.method(File.java:123))
        if stripped.startswith('at ') and '.java:' in line:
            relevant.append(line)
            continue

        # Compilation errors (javac output)
        # e.g., "error: cannot find symbol" or "错误: 找不到符号"
        if any(kw in line.lower() for kw in ['error:', '错误:', 'cannot find symbol', '找不到符号', 'package does not exist', 'does not exist']):
            relevant.append(line)
            continue

        # Gradle task failures
        if 'FAILED' in line and 'Task' in line:
            relevant.append(line)
            continue

    # If no test results were found, add a clear warning
    if not has_test_result:
        if exit_code == 0:
            relevant.append("")
            relevant.append("WARNING: BUILD SUCCESSFUL but NO test results were found!")
            relevant.append("This likely means 0 tests were executed. Possible causes:")
            relevant.append("  1. Test classes have compilation errors and were silently excluded")
            relevant.append("  2. JUnit5 test discovery failed (missing android-junit5 plugin)")
            relevant.append("  3. Test class name does not match the --tests filter")
            relevant.append("Run `run_build` to check for compilation errors in test source code.")
        else:
            relevant.append("")
            relevant.append("WARNING: BUILD FAILED and no test results were captured.")
            relevant.append("Check the compilation errors above — tests may not compile at all.")

    # Build final result with exit code header
    result = f"Exit Code: {exit_code}\n"
    if relevant:
        result += '\n'.join(relevant)

    # Truncate AFTER filtering (not before) to prevent token explosion
    # Allow more room since we've already filtered out noise
    if len(result) > 8000:
        result = result[:4000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-4000:]

    return result


async def _run_build_android() -> str:
    """Run build for Android projects using Gradle.
    Compiles both main source and test source to catch test compilation errors early.

    Runs subprocess directly to get FULL output before filtering, same as _run_tests_android.
    """
    gradlew = _gradlew_cmd()
    cmd = f"{gradlew} assembleDebug compileDebugUnitTestJavaWithJavac"
    abs_cwd = get_abs_path(".")
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=abs_cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"}
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
        output = stdout.decode('utf-8', errors='replace')
        error = stderr.decode('utf-8', errors='replace')
        exit_code = process.returncode
    except asyncio.TimeoutError:
        process.kill()
        return "=== Android Build Result ===\nCommand timed out after 180 seconds."
    except Exception as e:
        return f"=== Android Build Result ===\nExecution failed: {str(e)}"

    # Filter build output: keep only errors, warnings, and BUILD result
    all_lines = output.split('\n') + error.split('\n')
    relevant = []
    for line in all_lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Build result
        if 'BUILD ' in line and not stripped.startswith('>'):
            relevant.append(line)
            continue
        # Failure/exception details
        if any(kw in line for kw in ['FAILURE:', 'What went wrong:', 'Execution failed', 'Exception', 'Caused by']):
            relevant.append(line)
            continue
        # Compilation errors
        if any(kw in line.lower() for kw in ['error:', '错误:', 'cannot find symbol', '找不到符号', 'package does not exist', 'does not exist', 'warning:']):
            relevant.append(line)
            continue
        # Stack trace lines
        if stripped.startswith('at ') and '.java:' in line:
            relevant.append(line)
            continue
        # Gradle task failures
        if 'FAILED' in line and 'Task' in line:
            relevant.append(line)
            continue

    result = f"=== Android Build Result ===\nExit Code: {exit_code}\n"
    if relevant:
        result += '\n'.join(relevant)
    else:
        # If nothing relevant was filtered, include a summary
        if exit_code == 0:
            result += "BUILD SUCCESSFUL"
        else:
            # Fallback: include last 2000 chars of raw output
            raw = output + error
            result += (raw[-2000:] if len(raw) > 2000 else raw)

    if len(result) > 8000:
        result = result[:4000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-4000:]

    return result


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
