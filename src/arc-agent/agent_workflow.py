import os
import json
import asyncio
from typing import Callable, Awaitable
import shutil
import re
import base64
import mimetypes
import requests

from agents.interface_designer import InterfaceDesigner
from agents.test_generator import TestGenerator
from agents.test_driven_developer import TestDrivenDeveloper
from agents.context_pipeline import context_pipeline

from traceability.database import (
    get_requirement_by_id,
    get_all_requirements,
    set_db_path,
    init_db,
    update_interface_file_info,
    insert_test,
    update_test_implemented_status,
    get_interfaces_by_req_id,
    get_tests_by_req_id,
    update_requirement_visuals,
    insert_interface,
    update_interface_req_ids,
    update_interface_implemented,
    insert_call_edge,
    upsert_implementation
)

from utils import run_npm_install, run_git_init, set_workspace_root, set_app_type

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Global Debug Flag
DEBUG_MODE = int(os.environ.get("ARC_DEBUG", "1"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..', '..'))
TEMPLATES_ROOT = os.path.join(PROJECT_ROOT, 'templates')

def extract_json_array_from_markdown(raw_output: str):
    """Extract a JSON array from fenced markdown block or fallback to first JSON array span."""
    if not raw_output:
        return None
    fenced = re.search(r'```json\s*(.*?)\s*```', raw_output, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else raw_output
    try:
        data = json.loads(candidate)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # Fallback: pick first [...] span to improve robustness against extra prose.
    span = re.search(r'(\[\s*{[\s\S]*}\s*\])', raw_output)
    if span:
        try:
            data = json.loads(span.group(1))
            if isinstance(data, list):
                return data
        except Exception:
            return None
    return None

def visual_analysis_prompt() -> str:
    return """
**CRITICAL ROLE:** You are a "Headless" Frontend Reverse-Engineer.
**SCENARIO:** You must describe this UI screenshot to a blind developer who CANNOT see the image. They must reconstruct this page **pixel-perfectly** and **content-perfectly** using only your text description.

**CORE DIRECTIVES:**
1.  **FULL OCR TRANSCRIPTION:** You MUST transcribe **ALL** visible text content exactly as it appears. Do not summarize text.
2.  **STRICT DOM HIERARCHY:** Describe the layout as a tree structure (Parent -> Child -> Sibling).
3.  **PRECISE VISUAL SPECS:** Specify Geometry (px), Layout (Flex/Grid), Style (Hex colors), and Typography.

**OUTPUT FORMAT (Strict Markdown Tree):**

### 1. Global Design Tokens
* **Colors:** Define Primary, Secondary, Backgrounds (Estimate Hex).
* **Font:** Suggest font stack.

### 2. Page Structure & Content (Iterate from Top to Bottom)

#### [A] [Section Name] (e.g., Header, Sidebar, Card)
* **Container:** Dimensions, background color, layout properties.
* **Child Element 1:** [Type: Navigation/List]
    * **Layout:** Flex-row, gap 20px.
    * **Items (Transcription Examples):**
        * *If English:* "Home", "Products", "Contact Us" (Bold, Black).
        * *If Chinese:* "Shouye", "Chanpin Zhongxin", "Lianxi Women" (Regular, Gray).
* **Child Element 2:** [Type: Form Component]
    * **Container Style:** Border, shadow, padding.
    * **Internal Layout:** Vertical stack.
    * **Content (Transcription Examples):**
        * **Label:** "Username" OR "Yonghu Ming" (Exact text).
        * **Input Placeholder:** "Enter your email..." OR "Qingshuru Youxiang Dizhi..." (Exact text).
        * **Button:** "Submit" OR "Liji Tijiao" (White text on Blue bg).
* **Child Element 3:** [Type: Banner/Hero]
    * **Headline:** "Build Faster" OR "Jisu Goujian" (Font size ~32px, Bold).
    * **Sub-text:** "Start your journey today." OR "Kaiqi Ninde Shuzihua Zhilu." (Gray, ~16px).

**Action:** Start the "Blind Transcription". Ensure EVERY character (CN/EN) visible in the image is recorded in your description.
"""

def build_dependency_context(node_id: str) -> str:
    """
    Builds a contextual string containing information about already implemented 
    dependencies for the current requirement node.
    """
    req = get_requirement_by_id(node_id)
    if not req: 
        return "No dependency information available."
        
    deps = req.get("dependencies", [])
    if not deps: 
        return "No dependencies for this node. This is a root/independent feature."
        
    ctx = "### Dependency Context (Previously Implemented Modules)\n"
    ctx += "IMPORTANT: You MUST reuse and import the following existing interfaces if your current feature relies on them, instead of reinventing them. If you reuse them, set `reuse: true` in your JSON output.\n\n"
    
    for dep_id in deps:
        dep_req = get_requirement_by_id(dep_id)
        if not dep_req: continue
        
        ctx += f"#### Dependency Requirement Node: [{dep_id}]\n"
        ctx += f"Description: {dep_req.get('description', 'N/A')}\n"
        
        dep_ifaces = get_interfaces_by_req_id(dep_id)
        if dep_ifaces:
            ctx += "Available Interfaces from this Dependency:\n"
            for iface in dep_ifaces:
                ctx += f"  - ID: `{iface.get('interface_id')}` (Type: {iface.get('type')})\n"
                if iface.get('file_path'):
                    ctx += f"    File Path: `{iface.get('file_path')}`\n"
                if iface.get('first_line'):
                    ctx += f"    Signature: `{iface.get('first_line')}`\n"
                
                try:
                    content = json.loads(iface.get('content', '{}'))
                    desc = content.get('description', '')
                    if desc:
                        ctx += f"    Description: {desc}\n"
                    if content.get('inputs'):
                        ctx += f"    Inputs: {content['inputs']}\n"
                    if content.get('outputs'):
                        ctx += f"    Outputs: {content['outputs']}\n"
                    if content.get('callers'):
                        ctx += f"    Callers: {content['callers']}\n"
                    if content.get('callees'):
                        ctx += f"    Callees: {content['callees']}\n"
                except:
                    pass
        ctx += "\n"
    return ctx

async def check_prerequisites(app_type: str, log_cb: Callable[[str], Awaitable[None]]) -> bool:
    """Check that required tools are installed for the given app_type.
    Returns True if all prerequisites are met, False otherwise.
    """
    if app_type == "android":
        # Check Java
        try:
            process = await asyncio.create_subprocess_shell(
                "java -version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            version_output = stderr.decode() if stderr else stdout.decode()
            if process.returncode != 0:
                await log_cb("System", "Prerequisite check FAILED: Java is not installed or not on PATH. Android builds require JDK 17+.")
                return False
            first_line = version_output.strip().split('\n')[0] if version_output.strip() else "unknown"
            await log_cb("System", f"Prerequisite check passed: Java found ({first_line})")
        except Exception as e:
            await log_cb("System", f"Prerequisite check FAILED: Could not verify Java installation: {str(e)}")
            return False

        # Check Android SDK
        sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        if sdk_root and os.path.isdir(sdk_root):
            await log_cb("System", f"Prerequisite check passed: Android SDK found at {sdk_root}")
        else:
            # Try common default locations
            default_paths = [
                "D:/Android/Sdk",
                os.path.expanduser("~/AppData/Local/Android/Sdk"),
                os.path.expanduser("~/Android/Sdk"),
                "/usr/local/android-sdk",
                os.path.expanduser("~/Library/Android/sdk"),
            ]
            found = False
            for p in default_paths:
                if os.path.isdir(p):
                    sdk_root = p
                    os.environ["ANDROID_SDK_ROOT"] = p
                    await log_cb("System", f"Prerequisite check passed: Android SDK found at {p} (auto-detected)")
                    found = True
                    break
            if not found:
                await log_cb("System", "Prerequisite check FAILED: Android SDK not found. Set ANDROID_SDK_ROOT environment variable or install Android Studio.")
                return False

        # Auto-accept Android SDK licenses
        sdkmanager_path = os.path.join(sdk_root, "cmdline-tools", "latest", "bin", "sdkmanager")
        if os.name == "nt":
            sdkmanager_path = sdkmanager_path + ".bat"
        if os.path.exists(sdkmanager_path):
            await log_cb("System", "Auto-accepting Android SDK licenses...")
            try:
                accept_process = await asyncio.create_subprocess_shell(
                    f'yes | "{sdkmanager_path}" --licenses',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(accept_process.communicate(), timeout=30)
                await log_cb("System", "Android SDK licenses accepted.")
            except Exception as e:
                await log_cb("System", f"SDK license acceptance skipped (non-fatal): {str(e)}")
        else:
            await log_cb("System", "sdkmanager not found at expected path; skipping license acceptance.")

        return True

    elif app_type == "web":
        try:
            process = await asyncio.create_subprocess_shell(
                "node --version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
            if process.returncode == 0:
                version = stdout.decode().strip()
                await log_cb("System", f"Prerequisite check passed: Node.js found ({version})")
                return True
            else:
                await log_cb("System", "Prerequisite check FAILED: Node.js is not installed or not on PATH. Web builds require Node.js LTS.")
                return False
        except Exception as e:
            await log_cb("System", f"Prerequisite check FAILED: Could not verify Node.js installation: {str(e)}")
            return False

    return True  # Unknown app_type -- don't block


def _extract_modified_files_from_messages(messages):
    """Scan messages for write_file tool calls and return the set of modified file paths."""
    modified = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {}) if isinstance(tc, dict) else (tc.function if hasattr(tc, 'function') else {})
                if func.get("name") == "write_file" or (hasattr(func, 'name') and func.name == "write_file"):
                    try:
                        args_raw = func.get("arguments", "{}") if isinstance(func, dict) else (func.arguments if hasattr(func, 'arguments') else "{}")
                        args = json.loads(args_raw)
                        fp = args.get("file_path", "")
                        if fp:
                            modified.add(fp)
                    except Exception:
                        pass
    return list(modified)


class ARCWorkflowManager:
    """Manage the lifecycle of a single requirement node and multi-agent TDD state transitions"""
    
    def __init__(
        self,
        workspace_path: str,
        requirement_path: str = "",
        app_type: str = "web",
        broadcast_cb: Callable[[dict], Awaitable[None]] = None,
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.app_type = (app_type or "web").strip().lower()
        self.broadcast_cb = broadcast_cb
        
        # Instantiate all participating agents and pass the WebSocket broadcast callback to them
        self.interface_designer = InterfaceDesigner(broadcast_cb)
        self.test_generator = TestGenerator(broadcast_cb)
        self.test_driven_developer = TestDrivenDeveloper(broadcast_cb)

        # Global state context for the entire workflow (optional)
        # self.global_context = {} 

    async def _log(self, agent: str, message: str, status: str = None, node_id: str = None):
        if self.broadcast_cb:
            payload = {"type": "log", "agent": agent, "message": message}
            if node_id:
                payload["nodeId"] = node_id
            if status:
                payload["status"] = status
            await self.broadcast_cb(payload)
        else:
            # Default to console print if no callback provided
            prefix = f"[{node_id}] " if node_id else ""
            print(f"{prefix}[{agent}] {message}")
            if status:
                print(f"{prefix}[Status Update] {status}")

    async def parse_and_store_visual_elements(self, workspace_path: str, requirement_data: dict) -> None:
        """
        Extract the image url from the description of the requirement.
        Parse the content using llm and store the result in the requirement table in the database.
        """
        description = requirement_data.get("description", "")
        req_id = requirement_data.get("req_id", "")
        if not description or not req_id:
            await self._log("System", f"Invalid requirement data", node_id=req_id, status="error")
            return

        # 1. Extract image paths
        # Format: ![image](path/to/image)
        matches = re.findall(r'!\[image\]\(([^)]+)\)', description)
        if not matches:
            await self._log("System", "No image found in the description.", node_id=req_id, status="info")
            return

        visual_references = []

        for image_path in matches:
            normalized_path = os.path.normpath(image_path)
            
            # Strip leading separators to ensure it is treated as a relative path to workspace_path
            # This handles cases where markdown path starts with / or \
            if normalized_path.startswith(os.sep):
                normalized_path = normalized_path.lstrip(os.sep)
                
            full_path = os.path.join(workspace_path, normalized_path)
            full_path = os.path.abspath(full_path)
            
            if not os.path.exists(full_path):
                await self._log("System", f"Image not found: {full_path}", node_id=req_id, status="warning")
                continue
                
            # Encode image
            try:
                mime_type, _ = mimetypes.guess_type(full_path)
                if not mime_type:
                    mime_type = "image/png" # Default
                    
                with open(full_path, "rb") as image_file:
                    base64_image = base64.b64encode(image_file.read()).decode('utf-8')
                    
                # Call LLM
                prompt = visual_analysis_prompt()
                
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ]
                
                await self._log("System", f"Analyzing visual element: {image_path}", node_id=req_id)
                
                url = os.environ.get("VISUAL_OPENAI_API_BASE_URL", os.environ.get("OPENAI_API_BASE_URL", ""))
                api_key = os.environ.get("VISUAL_OPENAI_API_KEY")
                visual_model = os.environ.get("VISUAL_MODEL", os.environ.get("MODEL", ""))
                
                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
                data = {
                    "model": visual_model,
                    "messages": messages
                }
                
                response = await asyncio.to_thread(
                    requests.post, url, headers=headers, data=json.dumps(data), verify=False
                )
                
                if response.status_code == 200:
                    response_data = response.json()
                    analysis = response_data['choices'][0]['message']['content']
                else:
                    raise Exception(f"ModelArts API Error {response.status_code}: {response.text}")
                
                visual_references.append({
                    "image_path": image_path,
                    "analysis": analysis
                })
                
            except Exception as e:
                print(f"[Error] Failed to analyze image {full_path}: {e}")
                await self._log("System", f"Failed to analyze image {image_path}: {e}", node_id=req_id, status="error")
                
        # 2. Update database
        if visual_references:
            update_requirement_visuals(req_id, visual_references)
            await self._log("System", f"Stored {len(visual_references)} visual references for {req_id}", node_id=req_id)

    async def initialize_project(self):
        """Initialize the project workspace by setting up directories and files."""
        await self._log("System", f"Initializing project environment in {self.workspace_path}...")

        # Configure tool context
        set_workspace_root(self.workspace_path)
        set_app_type(self.app_type)

        # Check prerequisites
        prereqs_ok = await check_prerequisites(self.app_type, self._log)
        if not prereqs_ok:
            return False

        # Initialize .arc database
        arc_dir = os.path.join(self.workspace_path, '.arc')
        db_path = os.path.join(arc_dir, 'database.db')

        await self._log("System", f"Initializing traceability database at {db_path}...")
        set_db_path(db_path)
        init_db()

        if self.app_type not in {"web", "android"}:
            await self._log("System", f"Unsupported app_type '{self.app_type}', fallback to 'web'.")
            self.app_type = "web"

        template_dir = os.path.join(TEMPLATES_ROOT, self.app_type)
        if not os.path.exists(template_dir):
            await self._log("System", f"Error: Template directory not found at {template_dir}")
            return False

        await self._log("System", f"Using app_type={self.app_type}, template={template_dir}")
        await self._log("System", f"Copying template from {template_dir} to {self.workspace_path}...")
        try:
            await asyncio.to_thread(shutil.copytree, template_dir, self.workspace_path, dirs_exist_ok=True)
            await self._log("System", "Template files copied successfully.")
        except Exception as e:
            await self._log("System", f"Error copying template: {str(e)}")
            return False

        # For Android: extract target package name from requirements and repackage
        if self.app_type == "android":
            # Extract target package via LLM (understands natural language, handles any format)
            # Also extracts resource-id mappings and writes to .arc/metadata.md as global context
            target_package = await self._extract_android_package_name_via_llm()
            if target_package:
                await self._log("System", f"Extracted package name: {target_package}")
                self._setup_android_package(target_package)
            else:
                await self._log("System", f"Package extraction failed. Using fallback: com.example.app")
                self._setup_android_package("com.example.app")
                # Write fallback package info to metadata
                self._write_android_package_metadata("com.example.app", {})

            # Write local.properties with SDK path
            sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
            if sdk_root:
                local_props_path = os.path.join(self.workspace_path, "local.properties")
                # Use forward slashes for Gradle compatibility
                sdk_dir_gradle = sdk_root.replace("\\", "/")
                with open(local_props_path, "w", encoding="utf-8") as f:
                    f.write(f"sdk.dir={sdk_dir_gradle}\n")
                await self._log("System", f"Wrote local.properties with sdk.dir={sdk_dir_gradle}")

            # Fix gradlew execution permission (required on Linux/macOS; Windows uses gradlew.bat)
            if os.name != "nt":
                gradlew_path = os.path.join(self.workspace_path, "gradlew")
                if os.path.exists(gradlew_path):
                    try:
                        os.chmod(gradlew_path, 0o755)
                        await self._log("System", "Set gradlew execution permission (chmod +x)")
                    except Exception as e:
                        await self._log("System", f"Could not set gradlew permission (non-fatal): {str(e)}")

            # Auto-detect JDK path and update gradle.properties
            gradle_props_path = os.path.join(self.workspace_path, "gradle.properties")
            if os.path.exists(gradle_props_path):
                jdk_home = await self._detect_jdk_home()
                if jdk_home:
                    jdk_gradle = jdk_home.replace("\\", "/")
                    with open(gradle_props_path, 'r', encoding='utf-8') as f:
                        props = f.read()
                    # Replace or add org.gradle.java.home
                    if "org.gradle.java.home" in props:
                        import re
                        props = re.sub(r'org\.gradle\.java\.home=.*', f'org.gradle.java.home={jdk_gradle}', props)
                    else:
                        props += f"\norg.gradle.java.home={jdk_gradle}\n"
                    with open(gradle_props_path, 'w', encoding='utf-8') as f:
                        f.write(props)
                    await self._log("System", f"Set org.gradle.java.home={jdk_gradle} in gradle.properties")
                else:
                    await self._log("System", "Could not auto-detect JDK path. Please set org.gradle.java.home in gradle.properties manually.")

        backend_path = os.path.join(self.workspace_path, 'backend')
        if os.path.exists(backend_path):
            await self._log("System", "Installing backend dependencies. This might take a moment...")
            await run_npm_install(backend_path, self._log)

        frontend_path = os.path.join(self.workspace_path, 'frontend')
        if os.path.exists(frontend_path):
            await self._log("System", "Installing frontend dependencies. This might take a moment...")
            await run_npm_install(frontend_path, self._log)

        await self._log("System", "Full-stack workspace initialized completely.")

        # Initialize Git
        await self._log("System", "Initializing Git repository...")
        await run_git_init(self.workspace_path, self._log)

    async def _extract_android_package_name_via_llm(self) -> str:
        """Extract the target Android package name and resource-id mapping using an LLM call.
        The LLM reads all requirement descriptions and identifies:
        1. The application's package name from resource-id patterns, class references, etc.
        2. All resource-id ->component mappings (e.g., floatingActionButton ->FAB)

        Results are written to .arc/metadata.md as global context for downstream agents.

        Reads requirements directly from the YAML file (not from DB, since DB may not
        be populated yet at this point in the initialization flow).

        Fallback strategy if LLM fails:
        1. Extract from first resource-id pattern in requirements (e.g., org.billthefarmer.editor:id/xxx)
        2. Generate from project directory name (e.g., Echo -> com.echo.app)
        """
        # Read requirements directly from YAML file instead of DB
        # (DB is not yet populated at this point in the initialization flow)
        all_reqs = []
        if self.requirement_path and os.path.exists(self.requirement_path):
            try:
                from utils import load_requirements
                data = load_requirements(self.requirement_path)
                # Flatten the tree into a list of requirement dicts
                def flatten(node, result=None):
                    if result is None:
                        result = []
                    if isinstance(node, dict):
                        result.append(node)
                        for child in node.get("children", []):
                            flatten(child, result)
                    return result
                all_reqs = flatten(data)
            except Exception as e:
                await self._log("System", f"Failed to read requirements from YAML: {str(e)}. Trying DB fallback.")
                all_reqs = get_all_requirements()
        else:
            all_reqs = get_all_requirements()

        # Collect all description texts (truncate each to avoid excessive context)
        desc_texts = []
        for req in all_reqs:
            desc = req.get("description", "")
            if desc:
                # Truncate long descriptions but keep the beginning (where package info usually is)
                if len(desc) > 800:
                    desc = desc[:800] + "..."
                desc_texts.append(f"- [{req.get('id', '?')}] {desc}")

        if not desc_texts:
            return self._fallback_package_name_extraction(all_reqs)

        all_descriptions = "\n".join(desc_texts)
        # Limit total context to ~8000 chars
        if len(all_descriptions) > 8000:
            all_descriptions = all_descriptions[:8000] + "\n... (truncated)"

        system_prompt = """You are an Android package and resource analyzer.
Given requirement descriptions that contain resource-id patterns (e.g., `org.billthefarmer.editor:id/newFile`), fully-qualified class names, or other package references, extract:

1. The application's own package name
2. All resource-id mappings (resource name ->UI component type)

Rules for package name:
- Ignore system packages: com.android.*, android.*, com.google.*, androidx.*, java.*, javax.*, kotlin.*
- The app's package is the one that appears most frequently in resource-id patterns or is clearly the application's own package.

Rules for resource-id mapping:
- From patterns like `org.billthefarmer.editor:id/newFile`, extract: newFile ->Button
- From patterns like `org.billthefarmer.editor:id/vscroll`, extract: vscroll ->ScrollView
- Infer the UI component type from the resource name (e.g., "button" ->Button, "scroll" ->ScrollView, "fab" ->FloatingActionButton, "edit" ->Button/EditText, "view" ->Button, "save" ->Button, "pathText" ->EditText, "count" ->TextView)

Return a JSON object with exactly these fields:
{
  "package_name": "the.app.package.name",
  "resource_ids": {
    "resourceName": "ComponentType",
    ...
  }
}

If no app package can be identified, set package_name to "UNKNOWN"."""

        user_prompt = f"""Analyze these requirement descriptions and extract the Android package name and resource-id mappings:

{all_descriptions}

Return a JSON object with "package_name" and "resource_ids" fields."""

        try:
            client = self.interface_designer.client
            model = self.interface_designer.model
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.0
                ),
                timeout=60.0
            )
            result_text = response.choices[0].message.content.strip()

            # Parse JSON from response (handle markdown code blocks)
            import re as re_mod
            json_match = re_mod.search(r'\{[\s\S]*\}', result_text)
            if not json_match:
                await self._log("System", f"Package extraction: no JSON found in LLM response, using fallback")
                return self._fallback_package_name_extraction(all_reqs)

            parsed = json.loads(json_match.group())
            package_name = parsed.get("package_name", "UNKNOWN")
            resource_ids = parsed.get("resource_ids", {})

            # Validate package name
            package_name = package_name.strip().strip('`').strip('"').strip("'")
            if package_name == "UNKNOWN" or not package_name or '.' not in package_name:
                return self._fallback_package_name_extraction(all_reqs)
            segments = package_name.split('.')
            for seg in segments:
                if not seg or not (seg[0].isalpha() or seg[0] == '_'):
                    return self._fallback_package_name_extraction(all_reqs)

            await self._log("System", f"LLM extracted package name: {package_name}")
            if resource_ids:
                await self._log("System", f"LLM extracted {len(resource_ids)} resource-id mappings")

            # Write package info and resource-id mapping to .arc/metadata.md as global context
            self._write_android_package_metadata(package_name, resource_ids)

            return package_name
        except Exception as e:
            await self._log("System", f"Package extraction via LLM failed: {str(e)}")
            return self._fallback_package_name_extraction(all_reqs)

    def _write_android_package_metadata(self, package_name: str, resource_ids: dict):
        """Write Android package info and resource-id mapping to .arc/metadata.md.
        This becomes global context visible to all downstream agents.
        """
        metadata_path = os.path.join(self.workspace_path, ".arc", "metadata.md")
        if not os.path.exists(metadata_path):
            return

        with open(metadata_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Build the package info block
        pkg_dir = package_name.replace('.', '/')
        lines = [
            "",
            "## Android Package Configuration",
            f"- **Package**: `{package_name}`",
            f"- **Package directory**: `{pkg_dir}`",
            f"- **Main source**: `app/src/main/java/{pkg_dir}/`",
            f"- **Unit tests**: `app/src/test/java/{pkg_dir}/unit/` --package `{package_name}.unit`",
            f"- **Integration tests**: `app/src/test/java/{pkg_dir}/integration/` --package `{package_name}.integration`",
            f"- **E2E tests**: `app/src/test/java/{pkg_dir}/e2e/` --package `{package_name}.e2e`",
        ]

        if resource_ids:
            lines.append("")
            lines.append("## Resource-ID Mapping (from requirements)")
            lines.append("All resource-ids MUST use this package as prefix:")
            lines.append(f"  Pattern: `{package_name}:id/<resourceName>`")
            lines.append("")
            lines.append("| Resource ID | Component Type |")
            lines.append("|-------------|---------------|")
            for res_name, comp_type in sorted(resource_ids.items()):
                lines.append(f"| `{package_name}:id/{res_name}` | {comp_type} |")

        # List example reference files in .arc/examples/ so agents know they exist
        # and can read them for architectural patterns.
        examples_root = os.path.join(self.workspace_path, ".arc", "examples")
        example_files = []
        if os.path.exists(examples_root):
            for root, dirs, files in os.walk(examples_root):
                for fname in sorted(files):
                    if fname.endswith('.java') or fname.endswith('.kt'):
                        rel = os.path.relpath(os.path.join(root, fname), self.workspace_path)
                        example_files.append(rel.replace(os.sep, "/"))
        if example_files:
            lines.append("")
            lines.append("## Template Examples (read-only reference --NOT on the build path)")
            lines.append("These counter-app files demonstrate the correct architectural patterns")
            lines.append("(ViewModel, Repository, DAO, Room setup, Robolectric test helpers).")
            lines.append("Read them for guidance. Do NOT copy their `com.example.template` package")
            lines.append("declarations --use the target package instead.")
            for ef in example_files[:40]:
                lines.append(f"- `{ef}`")
            if len(example_files) > 40:
                lines.append(f"- ... ({len(example_files) - 40} more)")

        # List generic test infra already in the real test package (agents can import directly)
        test_root = os.path.join(self.workspace_path, "app", "src", "test", "java", pkg_dir)
        infra_files = []
        if os.path.exists(test_root):
            for root, dirs, files in os.walk(test_root):
                for fname in sorted(files):
                    if (fname.endswith('.java') or fname.endswith('.kt')) and not fname.endswith('Test.java'):
                        rel = os.path.relpath(os.path.join(root, fname), self.workspace_path)
                        infra_files.append(rel.replace(os.sep, "/"))
        if infra_files:
            lines.append("")
            lines.append("## Test Infrastructure (already in your test package --import directly)")
            lines.append("These utility classes are compiled alongside your tests. Do NOT recreate them.")
            for inf in infra_files:
                lines.append(f"- `{inf}`")

        lines.append("")

        pkg_block = "\n".join(lines)

        # Replace existing package config block or append
        start_marker = "## Android Package Configuration"
        start = content.find(start_marker)
        if start != -1:
            # Find end of this section (next ## heading or end of file)
            end = content.find("\n## ", start + len(start_marker))
            if end == -1:
                end = len(content)
            content = content[:start] + pkg_block + content[end:]
        else:
            content = content.rstrip() + "\n" + pkg_block

        with open(metadata_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def _fallback_package_name_extraction(self, all_reqs: list) -> str:
        """Fallback package name extraction when LLM fails.
        Strategy:
        1. Extract from first resource-id pattern (e.g., org.billthefarmer.editor:id/xxx)
        2. Generate from project directory name (e.g., Echo -> com.echo.app)
        """
        import re
        # Strategy 1: Extract from resource-id patterns
        for req in all_reqs:
            desc = req.get("description", "")
            # Match resource-id patterns like `org.billthefarmer.editor:id/newFile`
            matches = re.findall(r'`([a-z][a-z0-9_.]*):id/[a-zA-Z0-9_]+`', desc)
            for match in matches:
                # Filter out system packages
                if match.startswith(('com.android.', 'android.', 'com.google.', 'androidx.')):
                    continue
                if '.' in match and len(match.split('.')) >= 2:
                    return match

        # Strategy 2: Generate from project directory name
        project_name = os.path.basename(self.workspace_path).lower()
        # Sanitize: remove non-alphanumeric, replace spaces/dashes with nothing
        project_name = re.sub(r'[^a-z0-9]', '', project_name)
        if project_name:
            return f"com.{project_name}.app"

        # Last resort: use a generic package
        return "com.example.app"

    def _setup_android_package(self, target_package: str):
        """Setup Android project with the target package name.
        Creates package directory structure, updates build.gradle, and stores package name.
        """
        ws = self.workspace_path
        pkg_dir = target_package.replace('.', '/')

        # 1. Update app/build.gradle: namespace and applicationId
        build_gradle_path = os.path.join(ws, "app", "build.gradle")
        if os.path.exists(build_gradle_path):
            with open(build_gradle_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Replace both empty and default template namespace
            content = content.replace("namespace 'com.example.template'", f"namespace '{target_package}'")
            content = content.replace("namespace ''", f"namespace '{target_package}'")
            content = content.replace('applicationId "com.example.template"', f'applicationId "{target_package}"')
            content = content.replace('applicationId ""', f'applicationId "{target_package}"')
            with open(build_gradle_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 2. Update AndroidManifest.xml: package attribute
        manifest_path = os.path.join(ws, "app", "src", "main", "AndroidManifest.xml")
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace('package="com.example.template"', f'package="{target_package}"')
            content = content.replace('package=""', f'package="{target_package}"')
            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 3. Update template_info.json: package_name
        info_path = os.path.join(ws, "template_info.json")
        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace('"package_name": "com.example.template"', f'"package_name": "{target_package}"')
            with open(info_path, 'w', encoding='utf-8') as f:
                f.write(content)

        import shutil as _shutil

        template_src_base  = os.path.join(ws, "app", "src", "main", "java", "com", "example", "template")
        template_test_base = os.path.join(ws, "app", "src", "test", "java", "com", "example", "template")
        new_test_base      = os.path.join(ws, "app", "src", "test", "java", pkg_dir)
        examples_main      = os.path.join(ws, ".arc", "examples", "main")
        examples_test      = os.path.join(ws, ".arc", "examples", "test")
        os.makedirs(examples_main, exist_ok=True)
        os.makedirs(examples_test, exist_ok=True)

        def _clean_empty_parents(path, stop_at):
            """Remove empty ancestor directories up to (but not including) stop_at."""
            while path and path != stop_at:
                if os.path.isdir(path) and not os.listdir(path):
                    os.rmdir(path)
                    path = os.path.dirname(path)
                else:
                    break

        # 4. Archive ALL main source files to .arc/examples/main/ (original declarations
        #    preserved --readable reference for agents, not on the Gradle compile path).
        if os.path.exists(template_src_base):
            for root, dirs, files in os.walk(template_src_base, topdown=False):
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, template_src_base)
                    dst = os.path.join(examples_main, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    _shutil.copy2(src, dst)
                    os.remove(src)
            _shutil.rmtree(template_src_base, ignore_errors=True)
            _clean_empty_parents(
                os.path.dirname(template_src_base),
                os.path.join(ws, "app", "src", "main", "java"),
            )

        # 5. Split template test files:
        #    - Counter-specific tests (*Test.java) ->.arc/examples/test/ (reference only)
        #    - Generic infra (InstantTaskExecutorExtension, TestCounterApp, etc.) ->move to
        #      the real test package with updated declarations so agents can import them.
        if os.path.exists(template_test_base):
            for root, dirs, files in os.walk(template_test_base, topdown=False):
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, template_test_base)
                    is_test_class = fname.endswith("Test.java") or fname.endswith("Test.kt")
                    if is_test_class:
                        dst = os.path.join(examples_test, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        _shutil.copy2(src, dst)
                    else:
                        # Generic infra --move to real test tree with repackaged declarations
                        new_path = os.path.join(new_test_base, rel)
                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                        if fname.endswith(".java") or fname.endswith(".kt"):
                            with open(src, "r", encoding="utf-8") as f:
                                content = f.read()
                            content = content.replace("package com.example.template", f"package {target_package}")
                            content = content.replace("import com.example.template", f"import {target_package}")
                            with open(new_path, "w", encoding="utf-8") as f:
                                f.write(content)
                        else:
                            _shutil.copy2(src, new_path)
                    os.remove(src)
            _shutil.rmtree(template_test_base, ignore_errors=True)
            _clean_empty_parents(
                os.path.dirname(template_test_base),
                os.path.join(ws, "app", "src", "test", "java"),
            )

        # 6. Ensure test sub-package directories exist (agents write into these)
        for sub in ("unit", "integration", "e2e"):
            d = os.path.join(new_test_base, sub)
            os.makedirs(d, exist_ok=True)
            if not any(f for f in os.listdir(d) if not f.startswith(".")):
                with open(os.path.join(d, ".gitkeep"), "w") as f:
                    f.write("")

        # 7. Store the target package in utils so agents can use it
        from utils import set_android_package
        set_android_package(target_package)

    async def _detect_jdk_home(self) -> str:
        """Auto-detect JDK home path for Gradle.
        Checks JAVA_HOME, common install paths, and java -XshowSettings:properties.
        """
        # 1. Check JAVA_HOME env var
        java_home = os.environ.get("JAVA_HOME")
        if java_home and os.path.isdir(java_home):
            return java_home

        # 2. Check common install paths
        common_paths = [
            "D:/JDK/jdk21.0.6",
            "D:/JDK/jdk-21",
            "C:/Program Files/Java/jdk-21",
            "C:/Program Files/Eclipse Adoptium/jdk-21",
            "/usr/lib/jvm/java-21",
            "/usr/lib/jvm/jdk-21",
        ]
        for p in common_paths:
            if os.path.isdir(p):
                return p

        # 3. Detect from running java binary
        try:
            process = await asyncio.create_subprocess_shell(
                "java -XshowSettings:properties -version 2>&1 | grep 'java.home'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
            output = stdout.decode('utf-8', errors='replace')
            for line in output.split('\n'):
                line = line.strip()
                if line.startswith("java.home"):
                    path = line.split("=", 1)[1].strip()
                    if os.path.isdir(path):
                        return path
        except Exception:
            pass

        return ""

    def _repackage_android_project(self, target_package: str):
        """Repackage the Android project from com.example.template to the target package name.
        Updates: build.gradle (namespace + applicationId), Java source directories,
        agent prompts, and moves source files to the correct package directory.
        """
        import re as re_mod

        old_pkg = "com.example.template"
        old_path = old_pkg.replace('.', '/')
        new_path = target_package.replace('.', '/')
        ws = self.workspace_path

        # 1. Update app/build.gradle: namespace and applicationId
        build_gradle_path = os.path.join(ws, "app", "build.gradle")
        if os.path.exists(build_gradle_path):
            with open(build_gradle_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace(f"namespace '{old_pkg}'", f"namespace '{target_package}'")
            content = content.replace(f'applicationId "{old_pkg}"', f'applicationId "{target_package}"')
            with open(build_gradle_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 2. Move Java source files from old package dir to new package dir
        src_main = os.path.join(ws, "app", "src", "main", "java")
        old_dir = os.path.join(src_main, old_path)
        new_dir = os.path.join(src_main, new_path)

        if os.path.exists(old_dir):
            os.makedirs(new_dir, exist_ok=True)
            # Move all files and subdirectories
            for root, dirs, files in os.walk(old_dir):
                rel = os.path.relpath(root, old_dir)
                new_root = os.path.join(new_dir, rel)
                os.makedirs(new_root, exist_ok=True)
                for f in files:
                    old_file = os.path.join(root, f)
                    new_file = os.path.join(new_root, f)
                    # Update package declaration in the file
                    with open(old_file, 'r', encoding='utf-8', errors='replace') as fh:
                        file_content = fh.read()
                    file_content = file_content.replace(f"package {old_pkg}", f"package {target_package}")
                    file_content = file_content.replace(f"import {old_pkg}", f"import {target_package}")
                    with open(new_file, 'w', encoding='utf-8') as fh:
                        fh.write(file_content)
            # Remove old directory tree
            shutil.rmtree(old_dir)

        # 3. Move test source files similarly
        src_test = os.path.join(ws, "app", "src", "test", "java")
        old_test_dir = os.path.join(src_test, old_path)
        new_test_dir = os.path.join(src_test, new_path)

        if os.path.exists(old_test_dir):
            os.makedirs(new_test_dir, exist_ok=True)
            for root, dirs, files in os.walk(old_test_dir):
                rel = os.path.relpath(root, old_test_dir)
                new_root = os.path.join(new_test_dir, rel)
                os.makedirs(new_root, exist_ok=True)
                for f in files:
                    old_file = os.path.join(root, f)
                    new_file = os.path.join(new_root, f)
                    with open(old_file, 'r', encoding='utf-8', errors='replace') as fh:
                        file_content = fh.read()
                    file_content = file_content.replace(f"package {old_pkg}", f"package {target_package}")
                    file_content = file_content.replace(f"import {old_pkg}", f"import {target_package}")
                    with open(new_file, 'w', encoding='utf-8') as fh:
                        fh.write(file_content)
            shutil.rmtree(old_test_dir)

        # 4. Update AndroidManifest.xml references
        manifest_path = os.path.join(ws, "app", "src", "main", "AndroidManifest.xml")
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Replace both ".{old_pkg}" (activity shorthand) and full "{old_pkg}" references
            content = content.replace(f".{old_pkg}", f".{target_package}")
            content = content.replace(old_pkg, target_package)
            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 5. Update template_info.json: package_name
        info_path = os.path.join(ws, "template_info.json")
        if os.path.exists(info_path):
            with open(info_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content = content.replace(f'"package_name": "{old_pkg}"', f'"package_name": "{target_package}"')
            with open(info_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 6. Store the target package in utils so agents can use it
        from utils import set_android_package, get_android_package
        set_android_package(target_package)

    def _collect_stub_artifacts(self, interfaces: list, impl_messages: list = None) -> str:
        """Read stub files from disk and format as <source_code> context.
        Covers files declared in interfaces IR plus any extra files the InterfaceDesigner
        wrote during implementation (captured from its message history).
        """
        from utils import get_abs_path
        # Collect paths from IR (non-reused interfaces)
        paths = []
        seen = set()
        for iface in interfaces:
            if iface.get("reuse", False):
                continue
            fp = iface.get("file_path", "")
            if fp and fp not in seen:
                paths.append(fp)
                seen.add(fp)
        # Also capture extra files written by the implementation session
        if impl_messages:
            for fp in _extract_modified_files_from_messages(impl_messages):
                if fp not in seen:
                    paths.append(fp)
                    seen.add(fp)

        lines = []
        total = 0
        for fp in paths:
            abs_path = get_abs_path(fp)
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                if len(content) > 3000:
                    content = content[:3000] + "\n// ... [truncated]"
                lines.append(f"// === {fp} ===\n{content}")
                total += len(content)
                if total > 25000:
                    break
            except Exception:
                continue
        if not lines:
            return ""
        return "<source_code>\n" + "\n\n".join(lines) + "\n</source_code>"

    async def synthesize_interfaces_and_tests(self, node_id: str, requirement_data: dict, is_leaf: bool) -> dict:
        """Run design and test synthesis phases (no code implementation)."""
        await self._log("InterfaceDesigner", f"Starting interface design for node {node_id}", "analyzing", node_id)

        await self.parse_and_store_visual_elements(self.workspace_path, requirement_data)
        requirement_data = get_requirement_by_id(node_id) or requirement_data

        raw_ir_output, design_messages = await self.interface_designer.design_ir(
            node_id=node_id,
            requirement_data=requirement_data,
            is_leaf=is_leaf
        )
        interfaces = extract_json_array_from_markdown(raw_ir_output)
        if interfaces is None:
            await self._log("System", "First IR output is not a valid JSON array. Triggering one repair attempt.", node_id=node_id)
            repair_prompt_data = dict(requirement_data)
            repair_prompt_data["__repair_hint"] = "Return ONLY one ```json array``` with valid interface mapping objects. Do NOT write any code."
            raw_ir_output, design_messages = await self.interface_designer.design_ir(
                node_id=node_id,
                requirement_data=repair_prompt_data,
                is_leaf=is_leaf
            )
            interfaces = extract_json_array_from_markdown(raw_ir_output)

        if interfaces is not None:
            try:
                for iface in interfaces:
                    interface_id = iface.get("interface_id", f"{node_id}_UNKNOWN")
                    is_reuse = iface.get("reuse", False)

                    if is_reuse:
                        success = update_interface_req_ids(interface_id, node_id)
                        if success:
                            await self._log("System", f"Reused existing interface: {interface_id}", node_id=node_id)
                        else:
                            await self._log("System", f"Warning: Attempted to reuse interface {interface_id} but it was not found in DB.", node_id=node_id)
                    else:
                        itype = iface.get("type", "FUNC")
                        callers = iface.get("callers", [])
                        callees = iface.get("callees", [])
                        f_path = iface.get("file_path", "")
                        f_line = iface.get("first_line", "")

                        content_dict = {
                            "name": iface.get("name", ""),
                            "description": iface.get("description", ""),
                            "inputs": iface.get("inputs", []),
                            "outputs": iface.get("outputs", [])
                        }
                        content_str = json.dumps(content_dict, ensure_ascii=False)

                        insert_interface(
                            interface_id=interface_id,
                            req_ids=[node_id],
                            type=itype,
                            content=content_str,
                            file_path=f_path,
                            first_line=f_line,
                            implemented=False,
                            callers=callers,
                            callees=callees
                        )

                await self._log("System", f"Designed {len(interfaces)} interfaces for node {node_id}.", node_id=node_id)
                context_pipeline.cache.invalidate_db_layers(node_id)
            except Exception as e:
                await self._log("System", f"Failed to parse/store interface JSON block: {str(e)}", node_id=node_id)
        else:
            await self._log("System", "Warning: No valid interface JSON block found in output.", node_id=node_id)

        await self._log("System", "Interface design phase completed.", "designed", node_id)

        stub_artifacts = ""
        run_tdd = True
        if interfaces is not None and len(interfaces) > 0:
            await self._log(
                "System",
                f"Design phase finished for node {node_id}; moving directly to test generation (test-first).",
                node_id=node_id
            )
        else:
            await self._log(
                "System",
                f"No interfaces designed for node {node_id}; test generation will rely on requirement content.",
                node_id=node_id
            )

        if run_tdd:
            await self._log("TestGenerator", f"Generating test suite for node {node_id}...", node_id=node_id)

            interfaces_ir = get_interfaces_by_req_id(node_id)
            if not interfaces_ir:
                await self._log("System", "No IR in database. Falling back to file-based test generation.", node_id=node_id)
                interfaces_ir = []

            all_test_outputs = []
            label = f"{len(interfaces_ir)} interfaces" if interfaces_ir else "requirement description (no IR)"
            await self._log("TestGenerator", f"Generating all test types for {label}...", node_id=node_id)
            test_messages, test_tools = self.test_generator.build_initial_messages(
                node_id=node_id,
                requirement_data=requirement_data,
                interfaces_ir=interfaces_ir,
                test_type="All",
                preloaded_source=stub_artifacts
            )
            all_test_output, test_run_messages = await self.test_generator.run_from_messages(
                test_messages, node_id=node_id, max_steps=25, tools=test_tools
            )
            if extract_json_array_from_markdown(all_test_output) is None:
                await self._log("TestGenerator", "JSON mapping block missing --requesting it now.", node_id=node_id)
                test_run_messages.append({
                    "role": "user",
                    "content": (
                        "You did not output the required JSON test mapping block. "
                        "Do NOT write any more files. Output ONLY the ```json ... ``` block now, "
                        "listing every test file you wrote with test_id, req_id, interface_ids, "
                        "type, file_path, and first_line."
                    )
                })
                nudge_output, _ = await self.test_generator.run_from_messages(
                    test_run_messages, node_id=node_id, max_steps=3, tools=test_tools
                )
                all_test_output = nudge_output
            all_test_outputs.append(all_test_output)

            if all_test_outputs:
                combined_output = "\n\n".join(all_test_outputs)
                await self._log("System", f"Generated test output bundle length: {len(combined_output)} chars", node_id=node_id)

                for raw_test_output in all_test_outputs:
                    test_mappings = extract_json_array_from_markdown(raw_test_output)
                    if test_mappings is not None:
                        try:
                            for mapping in test_mappings:
                                t_id = mapping.get("test_id", f"TEST_{node_id}_UNKNOWN")
                                r_id = mapping.get("req_id", node_id)
                                i_ids = mapping.get("interface_ids", [])
                                t_type = mapping.get("type", "Unit")
                                f_path = mapping.get("file_path", "")
                                f_line = mapping.get("first_line", "")

                                insert_test(
                                    test_id=t_id,
                                    req_id=r_id,
                                    interface_ids=i_ids,
                                    type=t_type,
                                    file_path=f_path,
                                    first_line=f_line
                                )

                            await self._log("System", f"Successfully registered {len(test_mappings)} tests in traceability database.", node_id=node_id)
                            context_pipeline.cache.invalidate_db_layers(node_id)
                            context_pipeline.cache.invalidate_file_layers(node_id)
                        except Exception as e:
                            await self._log("System", f"Failed to parse/register test mappings from TestGenerator: {str(e)}", node_id=node_id)
            else:
                await self._log("System", "No tests generated.", node_id=node_id)
        return {
            "run_tdd": run_tdd,
            "stub_artifacts": stub_artifacts,
            "requirement_data": requirement_data,
        }

    async def implement_with_reactive_loop(self, node_id: str, requirement_data: dict, stub_artifacts: str) -> None:
        """Run TDD implementation loop with phase-based retries."""
        from traceability.test_result_tracker import TestResultTracker
        from agents.tools.cli_tools import run_tests_impl, parse_test_results

        dependency_context = build_dependency_context(node_id)
        req_desc = requirement_data.get("description", "")
        req_scenario = requirement_data.get("scenario", [])
        EXTRA_BUDGET = 3
        enable_test_downgrade = os.environ.get("ARC_ENABLE_TEST_DOWNGRADE", "0") == "1"
        DOWNGRADE_ATTEMPTS = 2 if enable_test_downgrade else 0

        tracker = TestResultTracker(os.path.join(self.workspace_path, ".arc"))
        tests = get_tests_by_req_id(node_id)

        tests_by_type = {"Unit": [], "Integration": [], "E2E": []}
        for t in tests:
            t_type = t.get("type", "Unit")
            if t_type in tests_by_type:
                tests_by_type[t_type].append(t)

        current_interfaces = get_interfaces_by_req_id(node_id)

        async def run_tdd_loop(target_type: str, tests_batch: list, budget: int,
                               scenario: list = None, preloaded_source: str = None,
                               downgrade_mode: bool = False) -> bool:
            test_files = [t.get("file_path") for t in tests_batch if t.get("file_path")]
            test_ids = [t.get("test_id") for t in tests_batch if t.get("test_id")]

            if not test_files:
                return True

            mode_label = "downgrade" if downgrade_mode else "fix"
            await self._log("TestDrivenDeveloper", f"Starting {target_type} TDD ({mode_label}) with {len(test_files)} test(s) (Budget: {budget})...", node_id=node_id)

            messages, tools = self.test_driven_developer.build_initial_messages(
                node_id=node_id,
                test_files=test_files,
                test_type=target_type,
                req_desc=req_desc,
                scenario=scenario,
                dependency_context=dependency_context,
                current_interfaces=current_interfaces,
                preloaded_source=preloaded_source
            )

            last_error_sig = None
            consecutive_same_errors = 0

            for iteration in range(1, budget + 1):
                await self._log("TestDrivenDeveloper", f"[{target_type}] Iteration {iteration}/{budget} ({mode_label})...", node_id=node_id)

                final_output, messages = await self.test_driven_developer.run_from_messages(
                    messages, node_id=node_id, max_steps=15, tools=tools
                )

                if "IMPLEMENTED" in final_output:
                    await self._log("System", f"[{target_type}] Tests passed! TDD loop completed ({mode_label}).", node_id=node_id)
                    try:
                        update_test_implemented_status(test_ids)
                    except Exception as db_err:
                        await self._log("System", f"Warning: Failed to update DB status: {str(db_err)}", node_id=node_id)
                    return True
                else:
                    error_sig = final_output[:200] if final_output else ""
                    if error_sig == last_error_sig:
                        consecutive_same_errors += 1
                    else:
                        consecutive_same_errors = 0
                        last_error_sig = error_sig

                    if consecutive_same_errors >= 2:
                        await self._log("System", f"Warning: [{target_type}] Same error repeated 3x. Breaking early.", node_id=node_id)
                        break

                    if downgrade_mode:
                        nudge = (
                            f"The {target_type} tests are still failing even after implementation fixes. "
                            f"This suggests the test may be too strict or have incorrect assumptions. "
                            f"Simplify the test: remove flaky assertions, relax strict equality checks, "
                            f"or split into smaller test cases. Rewrite the test file and rerun `run_tests` with type `{target_type}`. "
                            f"Reply \"IMPLEMENTED\" only when all target tests pass."
                        )
                    else:
                        if iteration > 1:
                            modified_files = _extract_modified_files_from_messages(messages)
                            delta_ctx = context_pipeline.build_incremental_context(node_id, modified_files=modified_files)
                            if delta_ctx:
                                nudge = (
                                    f"The {target_type} tests are still failing. "
                                    f"Here are the files you modified:\n{delta_ctx}\n\n"
                                    f"Analyze the error output, fix the code, and rerun `run_tests` with type `{target_type}`. "
                                    f"Reply \"IMPLEMENTED\" only when all target tests pass."
                                )
                            else:
                                nudge = f"The {target_type} tests are still failing. Analyze the error output, fix the code, and rerun `run_tests` with type `{target_type}`. Reply \"IMPLEMENTED\" only when all target tests pass."
                        else:
                            nudge = f"The {target_type} tests are still failing. Analyze the error output, fix the code, and rerun `run_tests` with type `{target_type}`. Reply \"IMPLEMENTED\" only when all target tests pass."
                    messages.append({"role": "user", "content": nudge})

            await self._log("System", f"Warning: [{target_type}] TDD budget exhausted ({mode_label}).", node_id=node_id)
            return False

        for test_type in ["Unit", "Integration", "E2E"]:
            tests_batch = tests_by_type[test_type]
            if not tests_batch:
                continue

            test_files = [t.get("file_path") for t in tests_batch if t.get("file_path")]
            await self._log("System", f"[Phase A] Batch running {len(test_files)} {test_type} test(s)...", node_id=node_id)

            batch_output = await run_tests_impl(test_type)
            parsed = parse_test_results(batch_output)

            passed_files = set()
            failed_tests = []

            if parsed["exit_code"] == 0:
                for t in tests_batch:
                    t_id = t.get("test_id", "")
                    t_path = t.get("file_path", "")
                    tracker.record_test(node_id, test_type, t_id, t_path, "direct_pass", 1)
                await self._log("System", f"[Phase A] All {test_type} tests passed directly!", node_id=node_id)
            else:
                failed_names = set(parsed["failed"])
                for t in tests_batch:
                    t_id = t.get("test_id", "")
                    t_path = t.get("file_path", "")
                    t_first_line = t.get("first_line", "")
                    is_failed = False
                    for fn in failed_names:
                        if t_path and t_path.replace("/", ".").replace("\\", ".").replace(".java", "").replace(".kt", "") in fn:
                            is_failed = True
                            break
                        if t_first_line and t_first_line in fn:
                            is_failed = True
                            break

                    if is_failed:
                        failed_tests.append(t)
                    else:
                        tracker.record_test(node_id, test_type, t_id, t_path, "direct_pass", 1)
                        passed_files.add(t_path)

                if not failed_tests and parsed["exit_code"] != 0:
                    failed_tests = tests_batch
                    for t in tests_batch:
                        t_id = t.get("test_id", "")
                        if tracker.get_test_status(node_id, test_type, t_id) == "direct_pass":
                            node_data = tracker._data.get("nodes", {}).get(node_id, {}).get(test_type, {})
                            node_data.pop(t_id, None)

                await self._log("System", f"[Phase A] {test_type}: {len(passed_files)} passed, {len(failed_tests)} failed in batch.", node_id=node_id)

            if not failed_tests:
                continue

            still_failing = []
            for t in failed_tests:
                t_id = t.get("test_id", "")
                t_path = t.get("file_path", "")
                await self._log("System", f"[Phase B] Retrying {test_type} test: {t_id}...", node_id=node_id)

                success = await run_tdd_loop(test_type, [t], budget=EXTRA_BUDGET,
                                             scenario=req_scenario if test_type == "E2E" else None,
                                             preloaded_source=stub_artifacts)
                if success:
                    tracker.record_test(node_id, test_type, t_id, t_path, "retry_pass", EXTRA_BUDGET)
                    await self._log("System", f"[Phase B] {t_id} passed after individual retry.", node_id=node_id)
                else:
                    still_failing.append(t)
                    await self._log("System", f"[Phase B] {t_id} still failing after individual retry.", node_id=node_id)

            if not still_failing:
                continue

            if DOWNGRADE_ATTEMPTS <= 0:
                for t in still_failing:
                    t_id = t.get("test_id", "")
                    t_path = t.get("file_path", "")
                    tracker.record_test(node_id, test_type, t_id, t_path, "final_fail", EXTRA_BUDGET)
                    await self._log("System", f"[Phase C] Test downgrade disabled; {t_id} marked as final_fail.", node_id=node_id)
                tracker.save()
                continue

            for t in still_failing:
                t_id = t.get("test_id", "")
                t_path = t.get("file_path", "")
                await self._log("System", f"[Phase C] Attempting test downgrade for: {t_id}...", node_id=node_id)

                passed_on_downgrade = False
                for downgrade_attempt in range(1, DOWNGRADE_ATTEMPTS + 1):
                    success = await run_tdd_loop(test_type, [t], budget=EXTRA_BUDGET,
                                                 scenario=req_scenario if test_type == "E2E" else None,
                                                 preloaded_source=stub_artifacts,
                                                 downgrade_mode=True)
                    if success:
                        status = "retry_pass" if downgrade_attempt == 1 else "relaxed_pass"
                        tracker.record_test(node_id, test_type, t_id, t_path, status, EXTRA_BUDGET + downgrade_attempt)
                        await self._log("System", f"[Phase C] {t_id} passed on downgrade attempt {downgrade_attempt}.", node_id=node_id)
                        passed_on_downgrade = True
                        break
                    else:
                        await self._log("System", f"[Phase C] {t_id} still failing on downgrade attempt {downgrade_attempt}.", node_id=node_id)

                if not passed_on_downgrade:
                    tracker.record_test(node_id, test_type, t_id, t_path, "final_fail", EXTRA_BUDGET + DOWNGRADE_ATTEMPTS)
                    await self._log("System", f"[Phase C] {t_id} marked as final_fail.", node_id=node_id)

            tracker.save()

        node_stats = tracker.get_node_stats(node_id)
        stats_parts = []
        for tt in ["Unit", "Integration", "E2E"]:
            s = node_stats.get(tt, {})
            total = s.get("total", 0)
            if total > 0:
                dp = s.get("direct_pass", 0)
                stats_parts.append(f"{tt}: {dp}/{total} direct pass")
        if stats_parts:
            await self._log("System", f"Node {node_id} results: " + ", ".join(stats_parts), node_id=node_id)

    async def record_parent_child_call_edges(self, parent_node_id: str, child_node_id: str) -> int:
        """Persist explicit call-graph edges between a parent node and a child node."""
        try:
            parent_interfaces = get_interfaces_by_req_id(parent_node_id)
            child_interfaces = get_interfaces_by_req_id(child_node_id)
            edge_count = 0
            for p_iface in parent_interfaces:
                p_id = p_iface.get("interface_id")
                if not p_id:
                    continue
                for c_iface in child_interfaces:
                    c_id = c_iface.get("interface_id")
                    if not c_id:
                        continue
                    insert_call_edge(
                        source_req_id=parent_node_id,
                        target_req_id=child_node_id,
                        from_interface_id=p_id,
                        to_interface_id=c_id,
                        edge_type="dfs_parent_child"
                    )
                    edge_count += 1
            if edge_count > 0:
                await self._log(
                    "System",
                    f"Recorded {edge_count} call edge(s) from node {parent_node_id} to child {child_node_id}.",
                    node_id=parent_node_id
                )
            return edge_count
        except Exception as e:
            await self._log(
                "System",
                f"Failed to record call edges for parent {parent_node_id} -> child {child_node_id}: {str(e)}",
                node_id=parent_node_id
            )
            return 0

    async def prepare_node(self, node_id: str) -> dict:
        """Top-down phase for one node: synthesize interfaces and tests."""
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self._log("System", f"Error: Requirement node {node_id} not found in database.", node_id=node_id)
            return {"ok": False, "node_id": node_id}

        context_pipeline.prewarm(node_id)

        children_ids = requirement_data.get("children_ids", [])
        is_leaf = not children_ids
        if is_leaf:
            await self._log("System", f"Node {node_id} is a leaf node. Entering top-down synthesis.", node_id=node_id)
        else:
            await self._log("System", f"Node {node_id} is a non-leaf node (children: {children_ids}). Entering top-down synthesis.", node_id=node_id)

        try:
            phase_a = await self.synthesize_interfaces_and_tests(
                node_id=node_id,
                requirement_data=requirement_data,
                is_leaf=is_leaf
            )
            phase_a["ok"] = True
            phase_a["node_id"] = node_id
            return phase_a
        except Exception as e:
            await self._log("System", f"Top-down synthesis failed due to an error: {str(e)}", node_id=node_id)
            return {"ok": False, "node_id": node_id}

    async def implement_node(self, node_id: str, prepared: dict) -> bool:
        """Bottom-up phase for one node: implement and verify."""
        if not prepared or not prepared.get("ok", False):
            await self._log("System", f"Skipping bottom-up implementation for node {node_id} due to failed preparation.", node_id=node_id)
            return False

        requirement_data = prepared.get("requirement_data", {}) or {}
        stub_artifacts = prepared.get("stub_artifacts", "")

        try:
            await self.implement_with_reactive_loop(
                node_id=node_id,
                requirement_data=requirement_data,
                stub_artifacts=stub_artifacts
            )

            from agents.tools.cli_tools import run_build_impl
            await self._log("System", f"Running final build verification for node {node_id}...", node_id=node_id)
            build_result = await run_build_impl()
            build_ok = "Exit Code: 0" in build_result
            artifact_paths = []
            for iface in get_interfaces_by_req_id(node_id):
                fp = iface.get("file_path")
                if fp and fp not in artifact_paths:
                    artifact_paths.append(fp)
            if build_ok:
                await self._log("System", f"Final build verification PASSED for node {node_id}", node_id=node_id)
                upsert_implementation(
                    req_id=node_id,
                    status="passed",
                    attempts=1,
                    artifact_paths=artifact_paths,
                    summary=build_result[:2000]
                )
            else:
                await self._log("System", f"Final build verification FAILED for node {node_id} - see build output for details", node_id=node_id)
                upsert_implementation(
                    req_id=node_id,
                    status="failed",
                    attempts=1,
                    artifact_paths=artifact_paths,
                    summary=build_result[:2000]
                )
            return build_ok
        except Exception as e:
            await self._log("System", f"Bottom-up implementation failed due to an error: {str(e)}", node_id=node_id)
            upsert_implementation(
                req_id=node_id,
                status="error",
                attempts=1,
                artifact_paths=[],
                summary=str(e)[:2000]
            )
            return False

    async def process_node(self, node_id: str) -> dict:
        """Process a single requirement node via phased orchestration."""
        prepared = await self.prepare_node(node_id)
        if not prepared.get("ok", False):
            return False
        return await self.implement_node(node_id, prepared)


async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    """Unified entry point for processing a node using an existing manager"""
    _ = requirement_data
    final_state = await manager.process_node(node_id)
    return final_state

