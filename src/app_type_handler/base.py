import asyncio
import inspect
import os
import shutil
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def _resolve_templates_root() -> str:
    env_root = os.environ.get("ARC_AGENT_TEMPLATES_ROOT", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    return os.path.abspath(os.path.join(BASE_DIR, "..", "templates"))


class AppTypeHandler(ABC):
    name = "web"

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str,
        interface_designer,
        log_cb: LogCallback,
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.interface_designer = interface_designer
        self.log_cb = log_cb

    @classmethod
    def template_dir(cls) -> str:
        return os.path.join(_resolve_templates_root(), cls.name)

    async def initialize_workspace(self) -> bool:
        prereqs_ok = await self.check_prerequisites()
        if not prereqs_ok:
            return False

        copied = await self.copy_template()
        if not copied:
            return False

        setup_ok = await self.post_template_setup()
        if not setup_ok:
            return False

        await self.install_dependencies()
        return True

    async def check_prerequisites(self) -> bool:
        from core.utils import check_prerequisites

        return await check_prerequisites(self.name, self.log_cb)

    async def copy_template(self) -> bool:
        template_dir = self.template_dir()
        if not os.path.exists(template_dir):
            await self._log("System", f"Error: Template directory not found at {template_dir}", "error", None)
            return False

        await self._log("System", f"Using app_type={self.name}, template={template_dir}")
        await self._log("System", f"Copying template from {template_dir} to {self.workspace_path}...")
        try:
            await asyncio.to_thread(
                shutil.copytree,
                template_dir,
                self.workspace_path,
                dirs_exist_ok=True,
            )
            await self._log("System", "Template files copied successfully.")
            return True
        except Exception as exc:
            await self._log("System", f"Error copying template: {str(exc)}", "error", None)
            return False

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self.log_cb(agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

    async def post_template_setup(self) -> bool:
        return True

    async def install_dependencies(self) -> None:
        return None

    @abstractmethod
    async def run_test_file(self, test_type: str, file_path: str) -> str:
        """Run one concrete test file through the system-side test executor."""
        raise NotImplementedError

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        """Run a batch of test files through the system-side test executor.

        App types can override this with a real grouped runner. The default
        implementation preserves compatibility by running files one by one.
        """
        if not file_paths:
            return (
                "Exit Code: 0\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )

        outputs: list[str] = []
        exit_codes: list[int] = []
        for file_path in file_paths:
            output = await self.run_test_file(test_type, file_path)
            outputs.append(f"=== Test File: {file_path} ===\n{output}")
            exit_code = -1
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("Exit Code:"):
                    try:
                        exit_code = int(stripped.split("Exit Code:", 1)[1].strip())
                    except ValueError:
                        exit_code = -1
                    break
            exit_codes.append(exit_code)

        batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
        header = [
            f"Exit Code: {batch_exit_code}",
            f"Batch Test Type: {test_type}",
            "Batch Test Files:",
        ]
        header.extend(f"- {file_path}" for file_path in file_paths)
        return f"{chr(10).join(header)}\n\n" + "\n\n".join(outputs)

    def validate_test_path(self, test_type: str, file_path: str) -> str | None:
        """Return an error message when a generated test path is invalid."""
        return None

    @classmethod
    @abstractmethod
    def build_stack_block(cls) -> str:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def default_stack_summary(cls) -> str:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        raise NotImplementedError

    @classmethod
    def read_stack_summary(cls, project_path: str) -> str:
        return cls.parse_stack_summary(cls.build_stack_block())
