import argparse
import asyncio
import os
import re
from typing import Optional, Dict, Any

ARC_STACK_START = "<!-- ARC_TECH_STACK_START -->"
ARC_STACK_END = "<!-- ARC_TECH_STACK_END -->"

def _detect_requirement_path(project_path: str, requirement_path: Optional[str]) -> str:
    if requirement_path:
        if os.path.isabs(requirement_path):
            return requirement_path
        return os.path.abspath(os.path.join(project_path, requirement_path))

    candidates = [
        os.path.join(project_path, "requirements", "requirements.yaml"),
        os.path.join(project_path, "requirements", "requirents.yaml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "Could not find requirements file. Tried: requirements/requirements.yaml, requirements/requirents.yaml"
    )


def _read_stack_summary(project_path: str) -> str:
    metadata_path = os.path.join(project_path, ".arc", "metadata.md")
    if not os.path.exists(metadata_path):
        return "No .arc/metadata.md found. Stack defaults will be inferred by templates/tools."

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            content = f.read()
        backend = re.search(r"-\s*backend:\s*(.+)", content, re.IGNORECASE)
        frontend = re.search(r"-\s*frontend:\s*(.+)", content, re.IGNORECASE)
        database = re.search(r"-\s*database:\s*(.+)", content, re.IGNORECASE)
        platform = re.search(r"\*\*\s*Platform\s*\*\*\s*:\s*(.+)", content, re.IGNORECASE)
        if platform:
            return f"platform={platform.group(1).strip()}"
        return (
            f"backend={backend.group(1).strip() if backend else 'N/A'}, "
            f"frontend={frontend.group(1).strip() if frontend else 'N/A'}, "
            f"database={database.group(1).strip() if database else 'N/A'}"
        )
    except Exception as e:
        return f"Failed to parse metadata.md: {str(e)}"


def _format_log(message: Dict[str, Any]) -> str:
    msg_type = message.get("type", "log")
    agent = message.get("agent", "System")

    if msg_type == "node_update":
        node_id = message.get("nodeId", "UNKNOWN")
        status = message.get("status", "unknown")
        return f"[Node:{node_id}] status -> {status}"

    if msg_type == "error-event":
        return f"[ERROR][{agent}] {message.get('message', '')}"

    if msg_type == "db_update":
        data = message.get("data", {})
        return f"[DB][{agent}] table={data.get('table', 'unknown')} items={data.get('items', '?')}"

    if msg_type == "clear-logs":
        return "================ ARC LOGS CLEARED ================"

    return f"[{agent}] {message.get('message', '')}"


def _build_stack_block(app_type: str) -> str:
    if app_type == "android":
        return "\n".join(
            [
                ARC_STACK_START,
                "* **Platform** : Android Native App (Single-module `app` template)",
                "* **Build System** : Gradle Wrapper + Android Gradle Plugin `7.1.2`",
                "* **Language** : Java 8 (`sourceCompatibility` / `targetCompatibility` = 1.8)",
                "* **UI Stack** : XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout",
                "* **SDK Target** : `compileSdk 31` / `minSdk 31` / `targetSdk 31`",
                "* **Runtime Entry** : `MainActivity` + `AndroidManifest.xml`",
                "* **Testing (Unit)** : JUnit4",
                "* **Testing (Instrumentation / E2E on device/emulator)** : AndroidX Test Runner + AndroidX JUnit Ext + Espresso（`androidTest`）",
                "* **Testing (Not configured in this directory)** : JUnit5、Robolectric、MockWebServer、Room in-memory DB",
                ARC_STACK_END,
            ]
        )

    # default: web
    return "\n".join(
        [
            ARC_STACK_START,
            "### Main Stack",
            "- backend: nodejs",
            "- frontend: react",
            "- database: sqlite",
            "",
            "### Frontend",
            "* **Framework**: React 18+ (Vite)",
            "* **Language**: JavaScript (ES6+)",
            "* **Styling**: Tailwind CSS v4",
            "* **HTTP**: Axios (Must use Interceptors for global error handling)",
            "* **Testing**: None in frontend directory. (Verified via E2E in backend).",
            "",
            "### Backend",
            "* **Runtime**: Node.js (LTS)",
            "* **Framework**: Express.js",
            "* **Database**: SQLite3 (`sqlite3` driver, file-based)",
            "* **Testing**:",
            "  * Vitest: Used for Unit and Integration testing.",
            "  * Supertest: Used with Vitest for API route testing.",
            "  * Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`.",
            ARC_STACK_END,
        ]
    )


def _upsert_metadata(project_path: str, app_type: str) -> str:
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    metadata_path = os.path.join(arc_dir, "metadata.md")
    new_block = _build_stack_block(app_type)

    old = ""
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as f:
            old = f.read()

    start = old.find(ARC_STACK_START)
    end = old.find(ARC_STACK_END)
    if start != -1 and end != -1 and end > start:
        before = old[:start].rstrip()
        after = old[end + len(ARC_STACK_END):].lstrip()
        merged = ""
        if before:
            merged += before + "\n\n"
        merged += new_block
        if after:
            merged += "\n\n" + after
        content = merged.strip() + "\n"
    elif old.strip():
        content = old.rstrip() + "\n\n" + new_block + "\n"
    else:
        content = new_block + "\n"

    with open(metadata_path, "w", encoding="utf-8") as f:
        f.write(content)
    return metadata_path


async def _console_broadcast(message: Dict[str, Any]):
    print(_format_log(message))


async def _run(project_path: str, requirement_path: Optional[str], clear_all: bool, app_type: str):
    import main as arc_main

    project_path = os.path.abspath(project_path)
    if not os.path.isdir(project_path):
        raise FileNotFoundError(f"Project path does not exist: {project_path}")

    req_path = _detect_requirement_path(project_path, requirement_path)
    if not os.path.exists(req_path):
        raise FileNotFoundError(f"Requirement file does not exist: {req_path}")

    metadata_path = _upsert_metadata(project_path, app_type)

    print("=============== ARC CLI ===============")
    print(f"Project Path: {project_path}")
    print(f"Requirement File: {req_path}")
    print(f"App Type: {app_type}")
    print(f"Tech Stack Source: {metadata_path}")
    print(f"Resolved Stack: {_read_stack_summary(project_path)}")
    print(f"Mode: {'clear-and-recompile' if clear_all else 'start-compilation'}")
    print("=======================================")

    original_broadcast = arc_main.manager.broadcast
    arc_main.manager.broadcast = _console_broadcast
    try:
        await arc_main.run_compilation(
            project_path=project_path,
            requirement_path=req_path,
            clear_all=clear_all,
            app_type=app_type,
        )
    finally:
        arc_main.manager.broadcast = original_broadcast


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run ARC agent workflow directly from terminal (no websocket UI needed)."
    )
    parser.add_argument(
        "project_path",
        nargs="?",
        help="Target project root path (contains requirements/ and .arc/). If omitted, will prompt interactively.",
    )
    parser.add_argument(
        "--requirement-path",
        help="Requirement yaml path. Absolute path, or relative to project path.",
    )
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="Clear project workspace and recompile (same semantics as 'Clear and Restart Compilation').",
    )
    parser.add_argument(
        "--app-type",
        choices=["web", "android"],
        default="web",
        help="Application type for stack metadata writing (default: web).",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    project_path = args.project_path
    if not project_path:
        project_path = input("Enter target project path: ").strip()
    if not project_path:
        raise ValueError("Target project path is required.")

    asyncio.run(_run(project_path, args.requirement_path, args.clear_all, args.app_type))


if __name__ == "__main__":
    main()
