import re
import os
import json
import asyncio

from typing import Awaitable, Callable

from .base import AppTypeHandler
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


def _is_valid_web_e2e_test_path(file_path: str) -> bool:
    normalized = (file_path or "").strip().replace("\\", "/").lstrip("./")
    return normalized.startswith("backend/test-e2e/") and normalized.endswith((".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx"))


def _is_valid_web_vitest_test_path(file_path: str) -> bool:
    normalized = (file_path or "").strip().replace("\\", "/").lstrip("./")
    valid_prefix = normalized.startswith("frontend/tests/") or normalized.startswith("backend/tests/")
    valid_suffix = normalized.endswith(
        (
            ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
            ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
        )
    )
    return valid_prefix and valid_suffix


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
            "backend_requested_files": [
                file_path for file_path in requested_files if _resolve_web_test_target(file_path, workspace_path)[0] == os.path.join(workspace_path, "backend")
            ],
            "frontend_requested_files": [
                file_path for file_path in requested_files if _resolve_web_test_target(file_path, workspace_path)[0] == os.path.join(workspace_path, "frontend")
            ],
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
            "requested_resolved_pairs": [
                {"requested_file": file_path, "resolved_target": _normalize_backend_test_path(file_path)}
                for file_path in requested_files
            ],
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


async def _wait_for_tcp_server_shutdown(host: str, port: int, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.25)
        except OSError:
            return True

    return False


async def _terminate_process(process: asyncio.subprocess.Process | None, *, port: int | None = None) -> None:
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
    if port is not None:
        await _wait_for_tcp_server_shutdown("127.0.0.1", port, timeout=10.0)


def _read_package_scripts(package_dir: str) -> dict[str, str]:
    package_json_path = os.path.join(package_dir, "package.json")
    if not os.path.exists(package_json_path):
        return {}

    try:
        with open(package_json_path, "r", encoding="utf-8") as package_file:
            package_data = json.load(package_file)
    except Exception:
        return {}

    scripts = package_data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _resolve_backend_start_command(backend_path: str) -> str | None:
    scripts = _read_package_scripts(backend_path)
    if "start" in scripts:
        return "npm run start"
    if "dev" in scripts:
        return "npm run dev"
    return None


async def _build_frontend_dist(workspace_path: str) -> tuple[bool, str]:
    frontend_path = os.path.join(workspace_path, "frontend")
    frontend_build_output = await _execute_web_test_command(
        "npm run build",
        cwd=frontend_path,
        timeout=120.0,
    )
    dist_index_path = os.path.join(frontend_path, "dist", "index.html")
    build_ok = _extract_exit_code(frontend_build_output) == 0 and os.path.exists(dist_index_path)
    if build_ok:
        return True, frontend_build_output

    if os.path.exists(dist_index_path):
        return False, frontend_build_output

    return (
        False,
        frontend_build_output
        + "\nFrontend build did not produce `frontend/dist/index.html`, so backend hosting cannot start.\n",
    )


async def _prepare_e2e_database(workspace_path: str) -> tuple[bool, str]:
    backend_path = os.path.join(workspace_path, "backend")
    prepare_output = await _execute_web_test_command(
        "npm run db:prepare:e2e",
        cwd=backend_path,
        timeout=60.0,
    )
    return _extract_exit_code(prepare_output) == 0, prepare_output


async def _start_backend_runtime(workspace_path: str) -> tuple[asyncio.subprocess.Process | None, str]:
    backend_path = os.path.join(workspace_path, "backend")
    start_command = _resolve_backend_start_command(backend_path)
    if not start_command:
        return None, (
            "Backend package.json must define `start` or `dev` so the backend can host "
            "the built frontend on the single web port."
        )

    try:
        backend_process = await asyncio.create_subprocess_shell(
            start_command,
            cwd=backend_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                **os.environ,
                **build_web_runtime_env(),
            },
        )
    except Exception as exc:
        return None, f"Failed to start backend runtime with `{start_command}`: {str(exc)}"

    server_ready = await _wait_for_tcp_server("127.0.0.1", get_web_port(), timeout=20.0)
    if not server_ready:
        await _terminate_process(backend_process, port=get_web_port())
        return None, (
            f"Failed to start backend runtime with `{start_command}` on port {get_web_port()} "
            "within 20 seconds."
        )

    return backend_process, start_command


class WebAppType(AppTypeHandler):
    name = "web"

    def validate_test_path(self, test_type: str, file_path: str) -> str | None:
        normalized_type = (test_type or "").strip().lower()
        if normalized_type in {"unit", "integration"} and not _is_valid_web_vitest_test_path(file_path):
            return (
                "Web Unit and Integration tests must live under `frontend/tests/...` or `backend/tests/...` "
                "and use a Vitest test/spec filename. "
                f"Received: {file_path}"
            )
        if normalized_type == "e2e" and not _is_valid_web_e2e_test_path(file_path):
            return (
                "Web E2E tests must live under `backend/test-e2e/...` and use a Playwright spec filename. "
                f"Received: {file_path}"
            )
        return None

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
        validation_error = self.validate_test_path(test_type, file_path)
        if validation_error:
            return f"Exit Code: 1\nSTDERR:\n{validation_error}\n"
        try:
            execution = _build_web_test_execution(test_type, file_path, self.workspace_path)
        except ValueError as exc:
            return str(exc)
        backend_process = None
        frontend_build_output = ""
        database_prepare_output = ""
        backend_start_command = ""

        if normalized_type == "e2e":
            build_ok, frontend_build_output = await _build_frontend_dist(self.workspace_path)
            if not build_ok:
                return _prepend_test_execution_header(
                    execution,
                    "Frontend build failed before E2E startup.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}",
                )

            database_ready, database_prepare_output = await _prepare_e2e_database(self.workspace_path)
            if not database_ready:
                return _prepend_test_execution_header(
                    execution,
                    "E2E database preparation failed before backend startup.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}",
                )

            backend_process, backend_start_command = await _start_backend_runtime(self.workspace_path)
            if backend_process is None:
                return _prepend_test_execution_header(
                    execution,
                    "Failed to start backend server for E2E testing.\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== Backend Runtime Error ===\n{backend_start_command}\n",
                )

        try:
            result = await _execute_web_test_command(execution["command"], cwd=execution["working_directory"])
            if normalized_type == "e2e":
                result = (
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"=== Backend Runtime ===\nCommand: {backend_start_command}\n"
                    f"Port: {get_web_port()}\n\n{result}"
                )
            return _prepend_test_execution_header(execution, result)
        finally:
            await _terminate_process(backend_process, port=get_web_port())

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

        invalid_paths = [file_path for file_path in file_paths if self.validate_test_path(test_type, file_path)]
        if invalid_paths:
            error_lines = ["Exit Code: 1", "STDERR:"]
            for file_path in invalid_paths:
                error_lines.append(self.validate_test_path(test_type, file_path) or "")
            return "\n".join(error_lines) + "\n"

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
                backend_exit_code = _extract_exit_code(backend_result)
                if backend_exit_code is None:
                    backend_exit_code = 1
                exit_codes.append(backend_exit_code)

            if execution.get("frontend_targets"):
                frontend_command = "npx vitest run " + " ".join(execution["frontend_targets"])
                frontend_result = await _execute_web_test_command(
                    frontend_command,
                    cwd=execution["frontend_working_directory"],
                )
                sections.append(f"=== Frontend Vitest Batch ===\n{frontend_result}")
                frontend_exit_code = _extract_exit_code(frontend_result)
                if frontend_exit_code is None:
                    frontend_exit_code = 1
                exit_codes.append(frontend_exit_code)

            if not sections:
                return _prepend_group_execution_header(
                    execution,
                    "Exit Code: 1\nSTDERR:\nNo resolvable Vitest targets were found for this batch.\n",
                )

            batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
            body = f"Exit Code: {batch_exit_code}\n\n" + "\n\n".join(sections)
            return _prepend_group_execution_header(execution, body)

        build_ok, frontend_build_output = await _build_frontend_dist(self.workspace_path)
        if not build_ok:
            return _prepend_group_execution_header(
                execution,
                "Frontend build failed before E2E startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}",
            )

        database_ready, database_prepare_output = await _prepare_e2e_database(self.workspace_path)
        if not database_ready:
            return _prepend_group_execution_header(
                execution,
                "E2E database preparation failed before backend startup.\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                f"=== Database Prepare ===\n{database_prepare_output}",
            )

        backend_process = None
        backend_start_command = ""
        try:
            backend_process, backend_start_command = await _start_backend_runtime(self.workspace_path)
            if backend_process is None:
                return _prepend_group_execution_header(
                    execution,
                    "Exit Code: 1\n\n"
                    f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                    f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                    f"STDERR:\n{backend_start_command}\n",
                )

            playwright_command = "npx playwright test"
            if execution.get("resolved_targets"):
                playwright_command += " " + " ".join(execution["resolved_targets"])
            playwright_result = await _execute_web_test_command(
                playwright_command,
                cwd=execution["working_directory"],
                timeout=120.0,
            )
            playwright_exit_code = _extract_exit_code(playwright_result)
            if playwright_exit_code is None:
                playwright_exit_code = 1
            body = (
                f"Exit Code: {playwright_exit_code}\n\n"
                f"=== Frontend Build ===\n{frontend_build_output}\n\n"
                f"=== Database Prepare ===\n{database_prepare_output}\n\n"
                f"=== Backend Runtime ===\nCommand: {backend_start_command}\n"
                f"Port: {get_web_port()}\n\n"
                f"{playwright_result}"
            )
            return _prepend_group_execution_header(execution, body)
        except Exception as exc:
            return f"Failed to start grouped E2E execution: {str(exc)}"
        finally:
            await _terminate_process(backend_process, port=get_web_port())

    @classmethod
    def build_stack_block(cls) -> str:
        web_port = get_web_port()
        base_url = get_web_base_url()
        return (
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
            "* **Language**: TypeScript + TSX (preferred default for frontend source files)\n"
            "* **Styling**: Tailwind CSS v4 via utility classes in component markup, not only bare CSS imports\n"
            "* **HTTP**: Axios (Must use Interceptors for global error handling)\n"
            "* **Testing**: Vitest for frontend unit/integration tests in `frontend/tests/...`.\n"
            "* **Frontend Test Infrastructure**: `vitest` + `jsdom` + `@testing-library/react` + `@testing-library/jest-dom` + `@testing-library/user-event` are preinstalled and configured through `frontend/vite.config.js` and `frontend/src/test/setup.ts`.\n"
            "\n"
            "### Backend\n"
            "* **Runtime**: Node.js (LTS)\n"
            "* **Framework**: Express.js\n"
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)\n"
            "* **Database Scaffold**:\n"
            "  * Runtime bootstrap and schema lifecycle: `backend/src/database/init_db.js`\n"
            "  * Shared query helpers: `backend/src/database/db_runtime.js`\n"
            "  * Shared seed entrypoint: `backend/src/database/seed_db.js`\n"
            "  * Shared test DB harness: `backend/src/database/test_harness.js`\n"
            "  * Barrel export for reuse: `backend/src/database/index.js`\n"
            "  * Extend these scaffold files instead of creating one-off DB connection/reset helpers in feature folders.\n"
            "* **Testing**:\n"
            "  * Vitest: Used for backend Unit and Integration testing.\n"
            "  * Supertest: Used with Vitest for API route testing.\n"
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`, configured by `backend/playwright.config.js`, and expected to use `process.env.PLAYWRIGHT_BASE_URL`.\n"
            "  * If a test uses the database, it must create an isolated test DB via the scaffold, prepare test data through the scaffold, and clean the test DB up after the suite finishes.\n"
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
