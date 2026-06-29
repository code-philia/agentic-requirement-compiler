import asyncio
import os
import shutil
from abc import ABC, abstractmethod
from importlib import resources
from typing import Awaitable, Callable


ARC_STACK_START = "<!-- ARC_TECH_STACK_START -->"
ARC_STACK_END = "<!-- ARC_TECH_STACK_END -->"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_TEMPLATES_PACKAGE = "templates"

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None]]


def _resolve_templates_root() -> str:
    env_root = os.environ.get("ARC_AGENT_TEMPLATES_ROOT", "").strip()
    if env_root:
        return env_root
    return str(resources.files(PACKAGE_TEMPLATES_PACKAGE))


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
        from utils import check_prerequisites

        return await check_prerequisites(self.name, self.log_cb)

    async def copy_template(self) -> bool:
        template_dir = self.template_dir()
        if not os.path.exists(template_dir):
            await self.log_cb("System", f"Error: Template directory not found at {template_dir}")
            return False

        await self.log_cb("System", f"Using app_type={self.name}, template={template_dir}")
        await self.log_cb("System", f"Copying template from {template_dir} to {self.workspace_path}...")
        try:
            await asyncio.to_thread(
                shutil.copytree,
                template_dir,
                self.workspace_path,
                dirs_exist_ok=True,
            )
            await self.log_cb("System", "Template files copied successfully.")
            return True
        except Exception as exc:
            await self.log_cb("System", f"Error copying template: {str(exc)}")
            return False

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
                "Exit Code: 1\n"
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
    def upsert_metadata(cls, project_path: str) -> str:
        arc_dir = os.path.join(project_path, ".arc")
        os.makedirs(arc_dir, exist_ok=True)
        metadata_path = os.path.join(arc_dir, "metadata.md")
        new_block = cls.build_stack_block()

        old_content = ""
        if os.path.exists(metadata_path):
            with open(metadata_path, "r", encoding="utf-8") as file:
                old_content = file.read()

        start = old_content.find(ARC_STACK_START)
        end = old_content.find(ARC_STACK_END)
        if start != -1 and end != -1 and end > start:
            before = old_content[:start].rstrip()
            after = old_content[end + len(ARC_STACK_END):].lstrip()
            merged = ""
            if before:
                merged += before + "\n\n"
            merged += new_block
            if after:
                merged += "\n\n" + after
            content = merged.strip() + "\n"
        elif old_content.strip():
            content = old_content.rstrip() + "\n\n" + new_block + "\n"
        else:
            content = new_block + "\n"

        with open(metadata_path, "w", encoding="utf-8") as file:
            file.write(content)
        return metadata_path

    @classmethod
    def read_stack_summary(cls, project_path: str) -> str:
        metadata_path = os.path.join(project_path, ".arc", "metadata.md")
        if not os.path.exists(metadata_path):
            return cls.default_stack_summary()

        try:
            with open(metadata_path, "r", encoding="utf-8") as file:
                content = file.read()
            return cls.parse_stack_summary(content)
        except Exception as exc:
            return f"Failed to parse metadata.md: {str(exc)}"
