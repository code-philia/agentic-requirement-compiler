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