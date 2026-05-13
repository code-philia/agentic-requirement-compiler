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


async def _run_tests_android(test_type: str, test_file_path: str = "", test_class_pattern: str = "") -> str:
    """Run tests for Android projects using Gradle.

    - Unit/Integration/E2E: ./gradlew testDebugUnitTest (app/src/test/)
    - All tests run on JVM via Robolectric (no device/emulator required).
    - test_class_pattern: Gradle --tests class name pattern to filter specific test classes
      e.g., "*IntegrationTest" to run only integration tests, "*E2ETest" for E2E

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
    elif test_class_pattern:
        cmd += f' --tests "{test_class_pattern}"'
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

    # Filter Gradle noise lines before returning to LLM
    # Keep: test results, errors, failures, build result, task failures, stack traces
    # Remove: Transforming, Compiling resources, Caching, VCS cleanup, etc.
    raw = output + "\n" + error
    filtered_lines = []
    noise_patterns = [
        'Transforming ', 'Compiling XML table', 'Compiling file ',
        'Caching disabled', 'is not up-to-date', 'VCS Checkout Cache',
        'dependencies-accessors', 'cleaned up in', 'removing files',
        'Watched directory', 'Input property ', 'Value of input',
        'Merging result: MERGED', 'ADDED from', 'android:supportsRtl',
        'android:roundIcon', 'android:allowBackup', 'android:icon',
        'android:label', 'android:theme', 'android:exported',
        'android:name', 'xmlns:android', 'intent-filter#',
        'action#', 'category#', 'See https://developer.android.com',
        'Run with --stacktrace', 'Run with --debug', 'Run with --scan',
        'Get more help at', 'actionable tasks',
    ]
    for line in raw.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        # Keep lines that matter
        if any(kw in line for kw in [
            'PASSED', 'FAILED', 'SKIPPED', 'BUILD ', 'FAILURE:',
            'What went wrong', 'Execution failed', 'Exception',
            'Caused by', 'error:', 'Error:', '错误:',
            'cannot find symbol', 'package does not exist',
            'Merging result: ERROR', 'Manifest merger failed',
            'Exit Code', 'Test result', 'tests,', 'no tests found',
            'Task :app:', 'at ', 'WARNING:',
        ]):
            filtered_lines.append(line)
            continue
        # Skip noise
        if any(kw in line for kw in noise_patterns):
            continue
        # Keep unknown lines (might be important)
        filtered_lines.append(line)

    result = f"Exit Code: {exit_code}\n"
    filtered = '\n'.join(filtered_lines)
    if len(filtered) > 30000:
        result += filtered[:15000] + "\n...[OUTPUT TRUNCATED]...\n" + filtered[-15000:]
    else:
        result += filtered
    return result


async def _run_build_android() -> str:
    """Run build for Android projects using Gradle.
    Compiles both main source and test source to catch test compilation errors early.

    Runs subprocess directly to get FULL output before filtering, same as _run_tests_android.
    """
    gradlew = _gradlew_cmd()
    cmd = f"{gradlew} assembleDebug compileDebugUnitTestJavaWithJavac --info"
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

    # Filter Gradle noise lines before returning to LLM (same as _run_tests_android)
    raw = output + "\n" + error
    filtered_lines = []
    noise_patterns = [
        'Transforming ', 'Compiling XML table', 'Compiling file ',
        'Caching disabled', 'is not up-to-date', 'VCS Checkout Cache',
        'dependencies-accessors', 'cleaned up in', 'removing files',
        'Watched directory', 'Input property ', 'Value of input',
        'Merging result: MERGED', 'ADDED from', 'android:supportsRtl',
        'android:roundIcon', 'android:allowBackup', 'android:icon',
        'android:label', 'android:theme', 'android:exported',
        'android:name', 'xmlns:android', 'intent-filter#',
        'action#', 'category#', 'See https://developer.android.com',
        'Run with --stacktrace', 'Run with --debug', 'Run with --scan',
        'Get more help at', 'actionable tasks',
    ]
    for line in raw.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        # Keep lines that matter
        if any(kw in line for kw in [
            'PASSED', 'FAILED', 'SKIPPED', 'BUILD ', 'FAILURE:',
            'What went wrong', 'Execution failed', 'Exception',
            'Caused by', 'error:', 'Error:', '错误:',
            'cannot find symbol', 'package does not exist',
            'Merging result: ERROR', 'Manifest merger failed',
            'Exit Code', 'Test result', 'tests,', 'no tests found',
            'Task :app:', 'at ', 'WARNING:',
        ]):
            filtered_lines.append(line)
            continue
        # Skip noise
        if any(kw in line for kw in noise_patterns):
            continue
        # Keep unknown lines (might be important)
        filtered_lines.append(line)

    result = f"=== Android Build Result ===\nExit Code: {exit_code}\n"
    filtered = '\n'.join(filtered_lines)
    if len(filtered) > 30000:
        result += filtered[:15000] + "\n...[OUTPUT TRUNCATED]...\n" + filtered[-15000:]
    else:
        result += filtered
    return result


# ============================================================
# Unified dispatchers
# ============================================================

async def run_tests_impl(test_type: str, test_file_path: str = "", test_class_pattern: str = "") -> str:
    """Run the test suite using the project's testing frameworks.
    test_class_pattern: Gradle --tests pattern for Android (e.g., "com.example.app.unit.*").
    If not provided, auto-derived from test_type using package-based subdirectory filtering.
    """
    app_type = get_app_type()

    if app_type == "android":
        # Auto-derive package-based class pattern from test_type if not explicitly provided
        if not test_class_pattern and not test_file_path:
            from utils import get_android_package
            pkg = get_android_package()
            tt = test_type.lower()
            if tt == "unit":
                test_class_pattern = f"{pkg}.unit.*"
            elif tt == "integration":
                test_class_pattern = f"{pkg}.integration.*"
            elif tt == "e2e":
                test_class_pattern = f"{pkg}.e2e.*"
        return await _run_tests_android(test_type, test_file_path, test_class_pattern)
    else:
        return await _run_tests_web(test_type, test_file_path)


def parse_test_results(test_output: str) -> dict:
    """Parse test output to identify individual test pass/fail.
    Returns {"passed": [test_name, ...], "failed": [test_name, ...], "exit_code": int}
    """
    app_type = get_app_type()
    result = {"passed": [], "failed": [], "exit_code": -1}

    # Extract exit code
    for line in test_output.split('\n'):
        if "Exit Code:" in line:
            try:
                result["exit_code"] = int(line.split("Exit Code:")[1].strip())
            except (ValueError, IndexError):
                pass

    if app_type == "android":
        # Gradle --info format:
        # "Gradle Test Run ... > org.pkg.TestClass > testMethodName PASSED"
        # "org.pkg.TestClass > testMethodName() FAILED"
        for line in test_output.split('\n'):
            stripped = line.strip()
            if 'PASSED' in stripped:
                # Extract test identifier before "PASSED"
                test_name = stripped.split('PASSED')[0].strip().rstrip('>').strip()
                if test_name:
                    result["passed"].append(test_name)
            elif 'FAILED' in stripped:
                test_name = stripped.split('FAILED')[0].strip().rstrip('>').strip()
                if test_name:
                    result["failed"].append(test_name)
    else:
        # Web (Jest/Vitest) format:
        # "PASS src/__tests__/foo.test.js" / "FAIL src/__tests__/foo.test.js"
        # "  ✓ testBar (5ms)" / "  ✕ testBaz (3ms)"
        current_file = None
        for line in test_output.split('\n'):
            stripped = line.strip()
            if stripped.startswith('PASS '):
                current_file = stripped[5:].strip()
            elif stripped.startswith('FAIL '):
                current_file = stripped[5:].strip()
            elif stripped.startswith('✓') or stripped.startswith('√'):
                test_name = stripped.lstrip('✓√').strip()
                if current_file:
                    test_name = f"{current_file} > {test_name}"
                result["passed"].append(test_name)
            elif stripped.startswith('✕') or stripped.startswith('×') or stripped.startswith('FAIL'):
                if stripped.startswith('✕') or stripped.startswith('×'):
                    test_name = stripped.lstrip('✕×').strip()
                    if current_file:
                        test_name = f"{current_file} > {test_name}"
                    result["failed"].append(test_name)

    return result


async def run_build_impl() -> str:
    """Run the build process for the current project to check for compilation errors."""
    app_type = get_app_type()

    if app_type == "android":
        return await _run_build_android()
    else:
        return await _run_build_web()
