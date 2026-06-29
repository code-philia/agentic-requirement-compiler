import asyncio
import os
import re

from utils import build_web_runtime_env, get_abs_path, get_app_type

DEFAULT_EXECUTE_COMMAND_TIMEOUT = 30.0
MAX_EXECUTE_COMMAND_TIMEOUT = 45.0


async def execute_command_impl(command: str, cwd: str = ".", timeout: float = 30.0) -> str:
    """Run a shell command in the project directory."""

    abs_cwd = get_abs_path(cwd)
    process = None
    requested_timeout = timeout
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_EXECUTE_COMMAND_TIMEOUT
    timeout = max(1.0, min(timeout, MAX_EXECUTE_COMMAND_TIMEOUT))

    try:
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8",
        }
        if get_app_type() == "web":
            env.update(build_web_runtime_env())

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=abs_cwd,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")

        result = f"Exit Code: {process.returncode}\n"
        if output:
            result += f"STDOUT:\n{output}\n"
        if error:
            result += f"STDERR:\n{error}\n"
        if timeout != requested_timeout:
            result += (
                f"NOTE: Requested timeout {requested_timeout} seconds was capped to "
                f"{timeout} seconds by the tool runtime.\n"
            )

        if len(result) > 4000:
            result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]
        return result
    except asyncio.TimeoutError:
        if process:
            process.kill()
        return (
            f"Command timed out after {timeout} seconds. "
            "If you started a server, make sure to run it in background or it will block the execution."
        )
    except Exception as exc:
        return f"Execution failed: {str(exc)}"


async def _run_build_web() -> str:
    frontend_result = await execute_command_impl("npm run build", cwd="./frontend")
    backend_result = await execute_command_impl("npm run build --if-present", cwd="./backend")
    return f"=== Frontend Build Result ===\n{frontend_result}\n\n=== Backend Build Result ===\n{backend_result}"


def _gradlew_cmd() -> str:
    if os.name == "nt":
        return "cmd /c gradlew.bat"
    return "./gradlew"


async def _run_build_android() -> str:
    """Run build for Android projects using Gradle."""

    command = f"{_gradlew_cmd()} assembleDebug compileDebugUnitTestJavaWithJavac --info"
    abs_cwd = get_abs_path(".")
    process = None

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=abs_cwd,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        exit_code = process.returncode
    except asyncio.TimeoutError:
        if process:
            process.kill()
        return "=== Android Build Result ===\nCommand timed out after 180 seconds."
    except Exception as exc:
        return f"=== Android Build Result ===\nExecution failed: {str(exc)}"

    raw = output + "\n" + error
    filtered_lines = []
    noise_patterns = [
        "Transforming ", "Compiling XML table", "Compiling file ",
        "Caching disabled", "is not up-to-date", "VCS Checkout Cache",
        "dependencies-accessors", "cleaned up in", "removing files",
        "Watched directory", "Input property ", "Value of input",
        "Merging result: MERGED", "ADDED from", "android:supportsRtl",
        "android:roundIcon", "android:allowBackup", "android:icon",
        "android:label", "android:theme", "android:exported",
        "android:name", "xmlns:android", "intent-filter#",
        "action#", "category#", "See https://developer.android.com",
        "Run with --stacktrace", "Run with --debug", "Run with --scan",
        "Get more help at", "actionable tasks",
    ]
    keep_patterns = [
        "PASSED", "FAILED", "SKIPPED", "BUILD ", "FAILURE:",
        "What went wrong", "Execution failed", "Exception",
        "Caused by", "error:", "Error:", "閿欒:",
        "cannot find symbol", "package does not exist",
        "Merging result: ERROR", "Manifest merger failed",
        "Exit Code", "Test result", "tests,", "no tests found",
        "Task :app:", "at ", "WARNING:",
    ]

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern in line for pattern in keep_patterns):
            filtered_lines.append(line)
            continue
        if any(pattern in line for pattern in noise_patterns):
            continue
        filtered_lines.append(line)

    result = f"=== Android Build Result ===\nExit Code: {exit_code}\n"
    filtered = "\n".join(filtered_lines)
    if len(filtered) > 30000:
        result += filtered[:15000] + "\n...[OUTPUT TRUNCATED]...\n" + filtered[-15000:]
    else:
        result += filtered
    return result


async def run_tests_signal_impl() -> str:
    """Signal-only tool implementation.

    Real test execution is owned by the system-side TDD session controller, which intercepts
    this tool call and runs the current test batch externally.
    """

    return "run_tests signal acknowledged. The system should execute the current target test batch."


def parse_test_results(test_output: str) -> dict:
    """Parse test output to identify individual test pass/fail."""

    app_type = get_app_type()
    result = {"passed": [], "failed": [], "exit_code": -1, "sub_batches": []}

    for line in test_output.split("\n"):
        if "Exit Code:" in line:
            try:
                result["exit_code"] = int(line.split("Exit Code:")[1].strip())
            except (ValueError, IndexError):
                pass
            break

    test_file_sections = re.findall(
        r"Test File:\s*(.+?)\r?\nTest Results:\r?\n(.*?)(?=\r?\nTest File: |\Z)",
        test_output or "",
        re.DOTALL,
    )
    if test_file_sections:
        for file_path, raw_section in test_file_sections:
            normalized_file_path = file_path.strip().replace("\\", "/")
            sub_exit_code = -1
            for line in raw_section.splitlines():
                stripped = line.strip()
                if stripped.startswith("Exit Code:"):
                    try:
                        sub_exit_code = int(stripped.split("Exit Code:", 1)[1].strip())
                    except ValueError:
                        sub_exit_code = -1
                    break
            result["sub_batches"].append(
                {
                    "requested_files": [normalized_file_path],
                    "exit_code": sub_exit_code,
                    "raw_output": raw_section.strip(),
                }
            )
    else:
        requested_files = [
            line.split("-", 1)[1].strip().replace("\\", "/")
            for line in (test_output or "").splitlines()
            if line.startswith("- ")
        ]
        backend_match = re.search(
            r"=== Backend Vitest Batch ===\r?\n(.*?)(?=\r?\n=== Frontend Vitest Batch ===|\Z)",
            test_output or "",
            re.DOTALL,
        )
        frontend_match = re.search(
            r"=== Frontend Vitest Batch ===\r?\n(.*?)(?=\Z)",
            test_output or "",
            re.DOTALL,
        )
        if backend_match or frontend_match:
            for scope, match in (("backend", backend_match), ("frontend", frontend_match)):
                if not match:
                    continue
                raw_section = match.group(1).strip()
                sub_exit_code = -1
                for line in raw_section.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Exit Code:"):
                        try:
                            sub_exit_code = int(stripped.split("Exit Code:", 1)[1].strip())
                        except ValueError:
                            sub_exit_code = -1
                        break
                scope_files = [
                    file_path for file_path in requested_files
                    if (scope == "backend" and file_path.startswith("backend/"))
                    or (scope == "frontend" and file_path.startswith("frontend/"))
                ]
                result["sub_batches"].append(
                    {
                        "requested_files": scope_files,
                        "exit_code": sub_exit_code,
                        "raw_output": raw_section,
                    }
                )

    if app_type == "android":
        for line in test_output.split("\n"):
            stripped = line.strip()
            if "PASSED" in stripped:
                test_name = stripped.split("PASSED")[0].strip().rstrip(">").strip()
                if test_name:
                    result["passed"].append(test_name)
            elif "FAILED" in stripped:
                test_name = stripped.split("FAILED")[0].strip().rstrip(">").strip()
                if test_name:
                    result["failed"].append(test_name)
    else:
        current_file = None
        passed_prefixes = ("\u2713", "\u221a", "\u2714")
        failed_prefixes = ("\u2717", "\u00d7", "\u2715")
        for line in test_output.split("\n"):
            stripped = line.strip()
            if stripped.startswith("PASS "):
                current_file = stripped[5:].strip()
            elif stripped.startswith("FAIL "):
                current_file = stripped[5:].strip()
            elif stripped.startswith(passed_prefixes):
                test_name = stripped[1:].strip()
                if current_file:
                    test_name = f"{current_file} > {test_name}"
                result["passed"].append(test_name)
            elif stripped.startswith(failed_prefixes):
                test_name = stripped[1:].strip()
                if current_file:
                    test_name = f"{current_file} > {test_name}"
                result["failed"].append(test_name)

    return result


async def run_build_impl() -> str:
    app_type = get_app_type()
    if app_type == "android":
        return await _run_build_android()
    return await _run_build_web()
