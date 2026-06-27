import json
import os
import re


ARC_STACK_START = "<!-- ARC_TECH_STACK_START -->"
ARC_STACK_END = "<!-- ARC_TECH_STACK_END -->"


def update_node_status(workspace_path: str, node_id: str, status: str) -> None:
    status_file = os.path.join(workspace_path, ".arc", "status.json")
    current_status = {}

    if os.path.exists(status_file):
        try:
            with open(status_file, "r", encoding="utf-8") as file:
                current_status = json.load(file)
        except Exception:
            current_status = {}

    current_status[node_id] = status
    with open(status_file, "w", encoding="utf-8") as file:
        json.dump(current_status, file, indent=2)


def read_stack_summary(project_path: str) -> str:
    metadata_path = os.path.join(project_path, ".arc", "metadata.md")
    if not os.path.exists(metadata_path):
        return "No .arc/metadata.md found. Stack defaults will be inferred by templates/tools."

    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            content = file.read()
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
    except Exception as exc:
        return f"Failed to parse metadata.md: {str(exc)}"


def build_stack_block(app_type: str) -> str:
    stack_block = ""
    if app_type == "web":
        stack_block = (
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
    
    
    elif app_type == "android":
        stack_block = "\n".join(
            [
                ARC_STACK_START,
                "* **Platform** : Android Native App (Single-module `app` template)",
                "* **Build System** : Gradle Wrapper + Android Gradle Plugin `8.1.4`",
                "* **Language** : Java 8 (`sourceCompatibility` / `targetCompatibility` = 1.8)",
                "* **UI Stack** : XML Layout + AndroidX AppCompat + Material Components + ConstraintLayout",
                "* **SDK Target** : `compileSdk 34` / `minSdk 31` / `targetSdk 34`",
                "* **Runtime Entry** : `MainActivity` + `AndroidManifest.xml`",
                "* **Database** : Room 2.6.1 (runtime + annotation processor)",
                "* **Lifecycle** : ViewModel 2.6.2 + LiveData 2.6.2",
                "* **Testing (Unit)** : JUnit5 5.10.2 + Robolectric 4.11.1 + Mockito 5.8.0 (`app/src/test/`)",
                "* **Test Discovery** : android-junit5 1.11.0.0 Gradle plugin",
                "* **Testing (Integration)** : JUnit5 + Robolectric + MockWebServer 4.12.0 + Room in-memory DB (`app/src/test/`)",
                "* **Testing (E2E)** : JUnit5 + Robolectric + ActivityScenario (`app/src/test/`)",
                ARC_STACK_END,
            ]
        )

    return stack_block


def upsert_metadata(project_path: str, app_type: str) -> str:
    arc_dir = os.path.join(project_path, ".arc")
    os.makedirs(arc_dir, exist_ok=True)
    metadata_path = os.path.join(arc_dir, "metadata.md")
    new_block = build_stack_block(app_type)

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
