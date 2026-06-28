import re
import os
import asyncio

from typing import Awaitable, Callable

from .base import ARC_STACK_END, ARC_STACK_START, AppTypeHandler


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
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"},
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
    }


def _prepend_test_execution_header(execution: dict[str, str], test_result: str) -> str:
    header = "\n".join(
        [
            f"Runner: {execution['runner']}",
            f"Command: {execution['command']}",
            f"Working Directory: {execution['working_directory']}",
            f"Requested Test File: {execution['requested_test_file']}",
            f"Resolved Test File: {execution['resolved_test_file']}",
        ]
    )
    return f"{header}\n{test_result}"


class WebAppType(AppTypeHandler):
    name = "web"

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
            try:
                backend_process = await asyncio.create_subprocess_shell(
                    "npm run dev",
                    cwd=backend_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                frontend_process = await asyncio.create_subprocess_shell(
                    "npm run dev",
                    cwd=frontend_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                servers_process = (backend_process, frontend_process)
                await asyncio.sleep(5)
            except Exception as exc:
                return f"Failed to start servers for E2E testing: {str(exc)}"

        try:
            result = await _execute_web_test_command(execution["command"], cwd=execution["working_directory"])
            return _prepend_test_execution_header(execution, result)
        finally:
            if servers_process:
                backend_process, frontend_process = servers_process
                for process in (backend_process, frontend_process):
                    try:
                        process.terminate()
                    except Exception:
                        pass

    @classmethod
    def build_stack_block(cls) -> str:
        return (
            f"{ARC_STACK_START}\n"
            "### Main Stack\n"
            "- backend: nodejs\n"
            "- frontend: react\n"
            "- database: sqlite\n"
            "\n"
            "### Frontend\n"
            "* **Framework**: React 18+ (Vite)\n"
            "* **Language**: JavaScript (ES6+)\n"
            "* **Styling**: Tailwind CSS v4\n"
            "* **HTTP**: Axios (Must use Interceptors for global error handling)\n"
            "* **Testing**: None in frontend directory. (Verified via E2E in backend).\n"
            "\n"
            "### Backend\n"
            "* **Runtime**: Node.js (LTS)\n"
            "* **Framework**: Express.js\n"
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)\n"
            "* **Testing**:\n"
            "  * Vitest: Used for Unit and Integration testing.\n"
            "  * Supertest: Used with Vitest for API route testing.\n"
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`.\n"
            f"{ARC_STACK_END}"
        )

    @classmethod
    def default_stack_summary(cls) -> str:
        return "backend=nodejs, frontend=react, database=sqlite"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        backend = re.search(r"-\s*backend:\s*(.+)", metadata_content, re.IGNORECASE)
        frontend = re.search(r"-\s*frontend:\s*(.+)", metadata_content, re.IGNORECASE)
        database = re.search(r"-\s*database:\s*(.+)", metadata_content, re.IGNORECASE)
        return (
            f"backend={backend.group(1).strip() if backend else 'N/A'}, "
            f"frontend={frontend.group(1).strip() if frontend else 'N/A'}, "
            f"database={database.group(1).strip() if database else 'N/A'}"
        )
