import re
import os
import asyncio

from typing import Awaitable, Callable

from .base import ARC_STACK_END, ARC_STACK_START, AppTypeHandler
from utils import build_web_runtime_env, get_web_base_url, get_web_port


async def run_npm_install(target_dir: str, log_cb: Callable[..., Awaitable[None]]):
    try:
        process = await asyncio.create_subprocess_shell(
            "npm install",
            cwd=target_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0:
            await log_cb("System", f"NPM install success in {target_dir}")
        else:
            await log_cb("System", f"NPM install failed in {target_dir}: {stderr.decode()}")
    except Exception as exc:
        await log_cb("System", f"NPM install error: {str(exc)}")


async def _execute_web_test_command(command: str, cwd: str, timeout: float = 60.0) -> str:
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8",
                **build_web_runtime_env(),
            },
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")

        result = f"Exit Code: {process.returncode}\n"
        if output:
            result += f"STDOUT:\n{output}\n"
        if error:
            result += f"STDERR:\n{error}\n"
        if len(result) > 4000:
            result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]
        return result
    except asyncio.TimeoutError:
        if process:
            process.kill()
        return f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return f"Execution failed: {str(exc)}"


def _format_test_result(stdout: str, stderr: str, exit_code: int) -> str:
    result = f"Exit Code: {exit_code}\n"
    if stdout:
        result += f"STDOUT:\n{stdout}\n"
    if stderr:
        result += f"STDERR:\n{stderr}\n"
    if len(result) > 4000:
        result = result[:2000] + "\n...[OUTPUT TRUNCATED]...\n" + result[-2000:]
    return result


def _extract_exit_code(command_output: str) -> int | None:
    for line in (command_output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Exit Code:"):
            try:
                return int(stripped.split("Exit Code:", 1)[1].strip())
            except ValueError:
                return None
    return None


def _normalize_backend_test_path(file_path: str) -> str:
    normalized = (file_path or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    if os.path.isabs(file_path):
        return normalized

    normalized = normalized.lstrip("./")
    if normalized.startswith("backend/"):
        normalized = normalized[len("backend/"):]
    return normalized


def _resolve_web_test_target(file_path: str, workspace_path: str) -> tuple[str, str]:
    normalized = (file_path or "").strip().replace("\\", "/")
    backend_path = os.path.join(workspace_path, "backend")
    frontend_path = os.path.join(workspace_path, "frontend")

    if not normalized:
        return backend_path, ""
    if os.path.isabs(file_path):
        return backend_path, normalized

    normalized = normalized.lstrip("./")
    if normalized.startswith("backend/"):
        return backend_path, normalized[len("backend/"):]
    if normalized.startswith("frontend/"):
        return frontend_path, normalized[len("frontend/"):]
    return backend_path, normalized


def _build_web_test_execution(test_type: str, file_path: str, workspace_path: str) -> dict[str, str]:
    normalized_type = (test_type or "").strip().lower()
    working_directory, resolved_file_path = _resolve_web_test_target(file_path, workspace_path)
    web_port = str(get_web_port())
    base_url = get_web_base_url()

    if normalized_type in {"unit", "integration"}:
        runner = "Vitest"
        command = f"npx vitest run {resolved_file_path}" if resolved_file_path else "npx vitest run"
    elif normalized_type == "e2e":
        runner = "Playwright"
        working_directory = os.path.join(workspace_path, "backend")
        resolved_file_path = _normalize_backend_test_path(file_path)
        command = f"npx playwright test {resolved_file_path}" if resolved_file_path else "npx playwright test"
    else:
        raise ValueError("Unknown test type. Must be 'unit', 'integration', or 'e2e'.")

    return {
        "runner": runner,
        "command": command,
        "working_directory": working_directory,
        "requested_test_file": file_path or "",
        "resolved_test_file": resolved_file_path,
        "web_port": web_port,
        "base_url": base_url,
    }


def _build_web_group_execution(test_type: str, file_paths: list[str], workspace_path: str) -> dict[str, str]:
    normalized_type = (test_type or "").strip().lower()
    requested_files = [str(path or "").strip() for path in file_paths if str(path or "").strip()]

    if normalized_type in {"unit", "integration"}:
        backend_targets: list[str] = []
        frontend_targets: list[str] = []
        for file_path in requested_files:
            working_directory, resolved_file_path = _resolve_web_test_target(file_path, workspace_path)
            normalized_resolved = resolved_file_path.replace("\\", "/")
            if working_directory == os.path.join(workspace_path, "frontend"):
                frontend_targets.append(normalized_resolved)
            else:
                backend_targets.append(normalized_resolved)

        return {
            "runner": "Vitest",
            "test_type": test_type,
            "requested_test_files": requested_files,
            "backend_working_directory": os.path.join(workspace_path, "backend"),
            "frontend_working_directory": os.path.join(workspace_path, "frontend"),
            "backend_targets": backend_targets,
            "frontend_targets": frontend_targets,
            "web_port": str(get_web_port()),
            "base_url": get_web_base_url(),
        }

    if normalized_type == "e2e":
        resolved_targets = [_normalize_backend_test_path(file_path) for file_path in requested_files]
        return {
            "runner": "Playwright",
            "test_type": test_type,
            "requested_test_files": requested_files,
            "working_directory": os.path.join(workspace_path, "backend"),
            "resolved_targets": resolved_targets,
            "web_port": str(get_web_port()),
            "base_url": get_web_base_url(),
        }

    raise ValueError("Unknown test type. Must be 'unit', 'integration', or 'e2e'.")


def _prepend_test_execution_header(execution: dict[str, str], test_result: str) -> str:
    header = "\n".join(
        [
            f"Runner: {execution['runner']}",
            f"Command: {execution['command']}",
            f"Working Directory: {execution['working_directory']}",
            f"Requested Test File: {execution['requested_test_file']}",
            f"Resolved Test File: {execution['resolved_test_file']}",
            f"Web Port: {execution['web_port']}",
            f"Base URL: {execution['base_url']}",
        ]
    )
    return f"{header}\n{test_result}"


def _prepend_group_execution_header(execution: dict[str, str], test_result: str) -> str:
    lines = [
        f"Runner: {execution['runner']}",
        f"Batch Test Type: {execution['test_type']}",
        f"Web Port: {execution['web_port']}",
        f"Base URL: {execution['base_url']}",
        "Requested Test Files:",
    ]
    lines.extend(f"- {file_path}" for file_path in execution.get("requested_test_files", []))

    if execution["runner"] == "Vitest":
        lines.append(f"Backend Working Directory: {execution['backend_working_directory']}")
        lines.append(f"Frontend Working Directory: {execution['frontend_working_directory']}")
        if execution.get("backend_targets"):
            lines.append("Backend Targets:")
            lines.extend(f"- {file_path}" for file_path in execution["backend_targets"])
        if execution.get("frontend_targets"):
            lines.append("Frontend Targets:")
            lines.extend(f"- {file_path}" for file_path in execution["frontend_targets"])
    else:
        lines.append(f"Working Directory: {execution['working_directory']}")
        lines.append("Resolved Targets:")
        lines.extend(f"- {file_path}" for file_path in execution.get("resolved_targets", []))

    return f"{chr(10).join(lines)}\n\n{test_result}"


async def _wait_for_tcp_server(host: str, port: int, timeout: float = 20.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            await asyncio.sleep(0.5)

    return False


async def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return

    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except Exception:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass


class WebAppType(AppTypeHandler):
    name = "web"

    async def post_template_setup(self) -> bool:
        replacements = {
            "__ARC_WEB_PORT__": str(get_web_port()),
        }
        target_files = [
            os.path.join(self.workspace_path, "backend", "src", "index.js"),
            os.path.join(self.workspace_path, "backend", "playwright.config.js"),
            os.path.join(self.workspace_path, "frontend", "vite.config.js"),
        ]

        try:
            for file_path in target_files:
                if not os.path.exists(file_path):
                    continue
                with open(file_path, "r", encoding="utf-8") as file:
                    content = file.read()
                for old_value, new_value in replacements.items():
                    content = content.replace(old_value, new_value)
                with open(file_path, "w", encoding="utf-8") as file:
                    file.write(content)
            await self.log_cb(
                "System",
                f"Configured web template for single-port backend hosting on port {get_web_port()}.",
            )
            return True
        except Exception as exc:
            await self.log_cb("System", f"Failed to configure web template: {str(exc)}")
            return False

    async def install_dependencies(self) -> None:
        backend_path = os.path.join(self.workspace_path, "backend")
        if os.path.exists(backend_path):
            await self.log_cb("System", "Installing backend dependencies. This might take a moment...")
            await run_npm_install(backend_path, self.log_cb)

        frontend_path = os.path.join(self.workspace_path, "frontend")
        if os.path.exists(frontend_path):
            await self.log_cb("System", "Installing frontend dependencies. This might take a moment...")
            await run_npm_install(frontend_path, self.log_cb)

    async def run_test_file(self, test_type: str, file_path: str) -> str:
        await self.log_cb("System", f"System test execution ({test_type}): {file_path}")
        normalized_type = test_type.lower()
        backend_path = os.path.join(self.workspace_path, "backend")
        try:
            execution = _build_web_test_execution(test_type, file_path, self.workspace_path)
        except ValueError as exc:
            return str(exc)
        servers_process = None

        if normalized_type == "e2e":
            frontend_path = os.path.join(self.workspace_path, "frontend")
            frontend_build_output = await _execute_web_test_command(
                "npm run build",
                cwd=frontend_path,
                timeout=120.0,
            )
            if _extract_exit_code(frontend_build_output) != 0:
                return _prepend_test_execution_header(
                    execution,
                    "Frontend build failed before E2E startup.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}",
                )

            try:
                backend_process = await asyncio.create_subprocess_shell(
                    "npm run start",
                    cwd=backend_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={
                        **os.environ,
                        **build_web_runtime_env(),
                    },
                )
                servers_process = (backend_process,)
                server_ready = await _wait_for_tcp_server("127.0.0.1", get_web_port(), timeout=20.0)
                if not server_ready:
                    await _terminate_process(backend_process)
                    return _prepend_test_execution_header(
                        execution,
                        "Failed to start backend server for E2E testing within 20 seconds.\n\n"
                        f"=== Frontend Build ===\n{frontend_build_output}",
                    )
            except Exception as exc:
                if servers_process:
                    for process in servers_process:
                        await _terminate_process(process)
                return f"Failed to start servers for E2E testing: {str(exc)}"

        try:
            result = await _execute_web_test_command(execution["command"], cwd=execution["working_directory"])
            if normalized_type == "e2e":
                result = f"=== Frontend Build ===\n{frontend_build_output}\n\n{result}"
            return _prepend_test_execution_header(execution, result)
        finally:
            if servers_process:
                for process in servers_process:
                    await _terminate_process(process)

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        normalized_type = (test_type or "").strip().lower()
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )

        for file_path in file_paths:
            await self.log_cb("System", f"System test execution ({test_type}): {file_path}")

        try:
            execution = _build_web_group_execution(test_type, file_paths, self.workspace_path)
        except ValueError as exc:
            return str(exc)

        if normalized_type in {"unit", "integration"}:
            sections: list[str] = []
            exit_codes: list[int] = []

            if execution.get("backend_targets"):
                backend_command = "npx vitest run " + " ".join(execution["backend_targets"])
                backend_result = await _execute_web_test_command(
                    backend_command,
                    cwd=execution["backend_working_directory"],
                )
                sections.append(f"=== Backend Vitest Batch ===\n{backend_result}")
                exit_codes.append(_extract_exit_code(backend_result) or 1)

            if execution.get("frontend_targets"):
                frontend_command = "npx vitest run " + " ".join(execution["frontend_targets"])
                frontend_result = await _execute_web_test_command(
                    frontend_command,
                    cwd=execution["frontend_working_directory"],
                )
                sections.append(f"=== Frontend Vitest Batch ===\n{frontend_result}")
                exit_codes.append(_extract_exit_code(frontend_result) or 1)

            if not sections:
                return _prepend_group_execution_header(
                    execution,
                    "Exit Code: 1\nSTDERR:\nNo resolvable Vitest targets were found for this batch.\n",
                )

            batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
            body = f"Exit Code: {batch_exit_code}\n\n" + "\n\n".join(sections)
            return _prepend_group_execution_header(execution, body)

        backend_path = os.path.join(self.workspace_path, "backend")
        frontend_path = os.path.join(self.workspace_path, "frontend")
        frontend_build_output = await _execute_web_test_command(
            "npm run build",
            cwd=frontend_path,
            timeout=120.0,
        )
        if _extract_exit_code(frontend_build_output) != 0:
            return _prepend_group_execution_header(
                execution,
                "Frontend build failed before E2E startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}",
            )

        backend_process = None
        try:
            backend_process = await asyncio.create_subprocess_shell(
                "npm run start",
                cwd=backend_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env={
                    **os.environ,
                    **build_web_runtime_env(),
                },
            )
            server_ready = await _wait_for_tcp_server("127.0.0.1", get_web_port(), timeout=20.0)
            if not server_ready:
                await _terminate_process(backend_process)
                return _prepend_group_execution_header(
                    execution,
                    "Failed to start backend server for E2E testing within 20 seconds.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}",
                )

            playwright_command = "npx playwright test"
            if execution.get("resolved_targets"):
                playwright_command += " " + " ".join(execution["resolved_targets"])
            playwright_result = await _execute_web_test_command(
                playwright_command,
                cwd=execution["working_directory"],
                timeout=120.0,
            )
            body = f"=== Frontend Build ===\n{frontend_build_output}\n\n{playwright_result}"
            return _prepend_group_execution_header(execution, body)
        except Exception as exc:
            return f"Failed to start grouped E2E execution: {str(exc)}"
        finally:
            await _terminate_process(backend_process)

    @classmethod
    def build_stack_block(cls) -> str:
        web_port = get_web_port()
        base_url = get_web_base_url()
        return (
            f"{ARC_STACK_START}\n"
            "### Main Stack\n"
            "- backend: nodejs\n"
            "- frontend: react\n"
            "- database: sqlite\n"
            f"- web_port: {web_port}\n"
            "\n"
            "### Runtime And Hosting\n"
            f"* **Single Web Port**: {web_port}\n"
            f"* **Base URL Under Test**: {base_url}\n"
            "* **Hosting Model**: Build the Vite frontend and let the Express backend serve `frontend/dist` on the same origin.\n"
            "* **Deployment Rule**: Do not rely on a separate frontend dev server for deployment or E2E.\n"
            "\n"
            "### Frontend\n"
            "* **Framework**: React 18+ (Vite)\n"
            "* **Language**: JavaScript (ES6+)\n"
            "* **Styling**: Tailwind CSS v4\n"
            "* **HTTP**: Axios (Must use Interceptors for global error handling)\n"
            "* **Testing**: Vitest for frontend unit/integration tests in `frontend/src/...`.\n"
            "* **Frontend Test Infrastructure**: `vitest` + `jsdom` + `@testing-library/react` + `@testing-library/jest-dom` + `@testing-library/user-event` are preinstalled and configured through `frontend/vite.config.js` and `frontend/src/test/setup.js`.\n"
            "\n"
            "### Backend\n"
            "* **Runtime**: Node.js (LTS)\n"
            "* **Framework**: Express.js\n"
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)\n"
            "* **Testing**:\n"
            "  * Vitest: Used for backend Unit and Integration testing.\n"
            "  * Supertest: Used with Vitest for API route testing.\n"
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`, configured by `backend/playwright.config.js`, and expected to use `process.env.PLAYWRIGHT_BASE_URL`.\n"
            f"{ARC_STACK_END}"
        )

    @classmethod
    def default_stack_summary(cls) -> str:
        return f"backend=nodejs, frontend=react, database=sqlite, web_port={get_web_port()}"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        backend = re.search(r"-\s*backend:\s*(.+)", metadata_content, re.IGNORECASE)
        frontend = re.search(r"-\s*frontend:\s*(.+)", metadata_content, re.IGNORECASE)
        database = re.search(r"-\s*database:\s*(.+)", metadata_content, re.IGNORECASE)
        web_port = re.search(r"-\s*web_port:\s*(.+)", metadata_content, re.IGNORECASE)
        return (
            f"backend={backend.group(1).strip() if backend else 'N/A'}, "
            f"frontend={frontend.group(1).strip() if frontend else 'N/A'}, "
            f"database={database.group(1).strip() if database else 'N/A'}, "
            f"web_port={web_port.group(1).strip() if web_port else get_web_port()}"
        )
