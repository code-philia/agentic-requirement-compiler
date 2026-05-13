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
    update_interface_implemented
)

from utils import run_npm_install, run_git_init, run_git_commit, set_workspace_root, set_app_type

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
        * *If Chinese:* "首页", "产品中心", "联系我们" (Regular, Gray).
* **Child Element 2:** [Type: Form Component]
    * **Container Style:** Border, shadow, padding.
    * **Internal Layout:** Vertical stack.
    * **Content (Transcription Examples):**
        * **Label:** "Username" OR "用户名" (Exact text).
        * **Input Placeholder:** "Enter your email..." OR "请输入邮箱地址..." (Exact text).
        * **Button:** "Submit" OR "立即提交" (White text on Blue bg).
* **Child Element 3:** [Type: Banner/Hero]
    * **Headline:** "Build Faster" OR "极速构建" (Font size ~32px, Bold).
    * **Sub-text:** "Start your journey today." OR "开启您的数字化之旅。" (Gray, ~16px).

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
            # TODO hard code sdk path for different OS, and also consider using `sdkmanager` command if available for more robust detection
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
                if tc.function.name == "write_file":
                    try:
                        args = json.loads(tc.function.arguments)
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
            target_package = await self._extract_android_package_name_via_llm()
            if target_package:
                await self._log("System", f"Extracted package name: {target_package}")
                self._setup_android_package(target_package)
            else:
                await self._log("System", f"Package extraction failed. Using fallback: com.example.app")
                self._setup_android_package("com.example.app")

            # Write local.properties with SDK path
            sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
            if sdk_root:
                local_props_path = os.path.join(self.workspace_path, "local.properties")
                # Use forward slashes for Gradle compatibility
                sdk_dir_gradle = sdk_root.replace("\\", "/")
                with open(local_props_path, "w", encoding="utf-8") as f:
                    f.write(f"sdk.dir={sdk_dir_gradle}\n")
                await self._log("System", f"Wrote local.properties with sdk.dir={sdk_dir_gradle}")

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
        """Extract the target Android package name using an LLM call.
        The LLM reads all requirement descriptions and identifies the application's
        package name from resource-id patterns, class references, or any other mentions.
        This handles all formats (backtick-quoted, bare, with special chars, etc.)
        that static regex would miss.

        Fallback strategy if LLM fails:
        1. Extract from first resource-id pattern in requirements (e.g., org.billthefarmer.editor:id/xxx)
        2. Generate from project directory name (e.g., Echo -> com.echo.app)
        """
        all_reqs = get_all_requirements()

        # Collect all description texts (truncate each to avoid excessive context)
        desc_texts = []
        for req in all_reqs:
            desc = req.get("description", "")
            if desc:
                # Truncate long descriptions but keep the beginning (where package info usually is)
                if len(desc) > 500:
                    desc = desc[:500] + "..."
                desc_texts.append(f"- [{req.get('id', '?')}] {desc}")

        if not desc_texts:
            return self._fallback_package_name_extraction(all_reqs)

        all_descriptions = "\n".join(desc_texts)
        # Limit total context to ~4000 chars
        if len(all_descriptions) > 4000:
            all_descriptions = all_descriptions[:4000] + "\n... (truncated)"

        system_prompt = """You are an Android package name extractor.
Given requirement descriptions that may contain resource-id patterns (e.g., `org.billthefarmer.editor:id/newFile`), fully-qualified class names (e.g., com.example.app.MainActivity), or other package references, identify the application's own package name.

Rules:
- Ignore system packages: com.android.*, android.*, com.google.*, androidx.*, java.*, javax.*, kotlin.*
- The app's package is the one that appears most frequently in resource-id patterns or is clearly the application's own package.
- Return ONLY the package name as a single line (e.g., org.billthefarmer.editor).
- If no app package can be identified, return "UNKNOWN"."""

        user_prompt = f"""Identify the Android application package name from these requirement descriptions:

{all_descriptions}

Return ONLY the package name (e.g., org.billthefarmer.editor) or "UNKNOWN" if not found."""

        try:
            client = self.interface_designer.client
            model = self.interface_designer.model
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.0
            )
            result = response.choices[0].message.content.strip()
            # Clean up: remove any markdown formatting or extra text
            result = result.strip('`').strip('"').strip("'")
            # Validate: must look like a package name (at least 2 dot-separated segments, lowercase letters)
            if result == "UNKNOWN" or not result:
                return self._fallback_package_name_extraction(all_reqs)
            if '.' not in result:
                return self._fallback_package_name_extraction(all_reqs)
            # Basic sanity check: each segment should be a valid Java identifier
            segments = result.split('.')
            for seg in segments:
                if not seg or not (seg[0].isalpha() or seg[0] == '_'):
                    return self._fallback_package_name_extraction(all_reqs)
            await self._log("System", f"LLM extracted package name: {result}")
            return result
        except Exception as e:
            await self._log("System", f"Package extraction via LLM failed: {str(e)}")
            return self._fallback_package_name_extraction(all_reqs)

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
            content = content.replace("namespace ''", f"namespace '{target_package}'")
            content = content.replace('applicationId ""', f'applicationId "{target_package}"')
            with open(build_gradle_path, 'w', encoding='utf-8') as f:
                f.write(content)

        # 2. Create package directory structure for main and test
        src_main = os.path.join(ws, "app", "src", "main", "java", pkg_dir)
        src_test = os.path.join(ws, "app", "src", "test", "java", pkg_dir)
        os.makedirs(src_main, exist_ok=True)
        os.makedirs(src_test, exist_ok=True)

        # Create .gitkeep to preserve empty directories
        with open(os.path.join(src_main, ".gitkeep"), "w") as f:
            f.write("")
        with open(os.path.join(src_test, ".gitkeep"), "w") as f:
            f.write("")

        # 3. Store the target package in utils so agents can use it
        from utils import set_android_package
        set_android_package(target_package)

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

        # 5. Store the target package in utils so agents can use it
        from utils import set_android_package, get_android_package
        set_android_package(target_package)

    def _collect_stub_artifacts(self, interfaces: list) -> str:
        """Read stub files from disk and format as <source_code> context.
        This allows downstream agents to skip re-reading these files.
        """
        from utils import get_abs_path
        lines = []
        total = 0
        for iface in interfaces:
            if iface.get("reuse", False):
                continue
            fp = iface.get("file_path", "")
            if not fp:
                continue
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

    async def process_node(self, node_id: str) -> dict:
        """Process a single requirement node through the workflow.

        Leaf nodes (no children): full 4-step pipeline (Design → Test → TDD).
        Non-leaf nodes (have children): only Step 1-2 (Design shared interfaces), skip Test/TDD.
        """

        # Get requirement data from database
        requirement_data = get_requirement_by_id(node_id)
        if not requirement_data:
            await self._log("System", f"Error: Requirement node {node_id} not found in database.", node_id=node_id)
            return False

        # Pre-warm context pipeline cache for this node (global layers only)
        context_pipeline.prewarm(node_id)

        # Determine if this is a leaf node (no children)
        children_ids = requirement_data.get("children_ids", [])
        is_leaf = not children_ids

        if not is_leaf:
            await self._log("System", f"Node {node_id} is a non-leaf node (children: {children_ids}). Will design shared interfaces only.", node_id=node_id)
        else:
            await self._log("System", f"Node {node_id} is a leaf node. Will run full pipeline (Design → Test → TDD).", node_id=node_id)
        
        try:
            # ==========================================
            # Step 1: Design IR (Interface Architecture) — JSON only, no code
            # ==========================================
            await self._log("InterfaceDesigner", f"Starting interface design for node {node_id}", "analyzing", node_id)

            dependency_context = build_dependency_context(node_id)

            await self.parse_and_store_visual_elements(self.workspace_path, requirement_data)
            # Refresh requirement data after parsing visual elements
            requirement_data = get_requirement_by_id(node_id)

            raw_ir_output, design_messages = await self.interface_designer.design_ir(
                node_id=node_id,
                requirement_data=requirement_data,
                is_leaf=is_leaf
            )
            # Parse IR JSON
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
                # Store interfaces in DB
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
                    # Invalidate DB-dependent cache layers since new interfaces were inserted
                    context_pipeline.cache.invalidate_db_layers(node_id)
                except Exception as e:
                    await self._log("System", f"Failed to parse/store interface JSON block: {str(e)}", node_id=node_id)
            else:
                await self._log("System", "Warning: No valid interface JSON block found in output.", node_id=node_id)

            await self._log("System", "Interface design phase completed.", "designed", node_id)

            # ==========================================
            # Step 2: Implement Stub Code (if interfaces were designed)
            # ==========================================
            stub_artifacts = ""  # Will be populated if stubs are written
            if interfaces is not None and len(interfaces) > 0:
                await self._log("InterfaceDesigner", f"Implementing stub code for {len(interfaces)} interfaces...", node_id=node_id)

                impl_output, _ = await self.interface_designer.implement_stubs_from_session(
                    messages=design_messages,
                    interfaces=interfaces,
                    node_id=node_id,
                    is_leaf=is_leaf
                )

                # Mark interfaces as implemented
                for iface in interfaces:
                    interface_id = iface.get("interface_id", "")
                    if interface_id and not iface.get("reuse", False):
                        update_interface_implemented(interface_id, True)

                await self._log("System", f"Stub code implementation completed for node {node_id}.", node_id=node_id)

                # Invalidate file-dependent cache layers (stubs were written to disk)
                context_pipeline.cache.invalidate_file_layers(node_id)

                # Collect stub artifacts to pass to downstream agents (avoids re-reading from disk)
                stub_artifacts = self._collect_stub_artifacts(interfaces)

                # Git Commit for Design + Implementation
                designed_interfaces = [m.get("interface_id", "UNKNOWN") for m in interfaces]
                commit_msg = f"feat(design): [{node_id}] designed & implemented: {', '.join(designed_interfaces)}"
                await run_git_commit(self.workspace_path, commit_msg, self._log)
            else:
                await self._log("System", f"No interfaces to implement for node {node_id}.", node_id=node_id)



            if is_leaf:
                # ==========================================
                # Step 3: Generate Tests (TDD Preparations)
                # ==========================================
                await self._log("TestGenerator", f"Generating test suite for node {node_id}...", node_id=node_id)

                interfaces_ir = get_interfaces_by_req_id(node_id)

                if not interfaces_ir:
                    # Fallback: generate tests based on requirement description + existing files
                    # even without formal IR entries
                    await self._log("System", "No IR in database. Falling back to file-based test generation.", node_id=node_id)
                    interfaces_ir = []  # will use requirement_data directly

                all_test_outputs = []

                # Single batch call: generate ALL test types (Unit + Integration + E2E) at once
                label = f"{len(interfaces_ir)} interfaces" if interfaces_ir else "requirement description (no IR)"
                await self._log("TestGenerator", f"Generating all test types for {label}...", node_id=node_id)
                test_messages, test_tools = self.test_generator.build_initial_messages(
                    node_id=node_id,
                    requirement_data=requirement_data,
                    interfaces_ir=interfaces_ir,
                    test_type="All",
                    preloaded_source=stub_artifacts
                )
                all_test_output, _ = await self.test_generator.run_from_messages(
                    test_messages, node_id=node_id, max_steps=15, tools=test_tools
                )
                all_test_outputs.append(all_test_output)

                # Combine outputs to store in state
                if all_test_outputs:
                    combined_output = "\n\n".join(all_test_outputs)
                    await self._log("System", f"Generated test output bundle length: {len(combined_output)} chars", node_id=node_id)

                    # parse test mappings
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
                                # Invalidate DB and file caches (tests were written and registered)
                                context_pipeline.cache.invalidate_db_layers(node_id)
                                context_pipeline.cache.invalidate_file_layers(node_id)
                            except Exception as e:
                                await self._log("System", f"Failed to parse/register test mappings from TestGenerator: {str(e)}", node_id=node_id)
                else:
                    await self._log("System", "No tests generated.", node_id=node_id)


                # ==========================================
                # Step 4: Implement (TDD Loop)
                # ==========================================
                req_desc = requirement_data.get("description", "")
                req_scenario = requirement_data.get("scenario", [])

                # Get tests from DB to know what files to run
                tests = get_tests_by_req_id(node_id)

                # Group tests by type
                tests_by_type = {"Unit": [], "Integration": [], "E2E": []}
                for t in tests:
                    t_type = t.get("type", "Unit")
                    if t_type in tests_by_type:
                        tests_by_type[t_type].append(t)

                # Helper to run TDD loop
                current_interfaces = get_interfaces_by_req_id(node_id)

                async def run_tdd_loop(target_type: str, tests_batch: list, budget: int, scenario: list = None, preloaded_source: str = None):
                    test_files = [t.get("file_path") for t in tests_batch if t.get("file_path")]
                    test_ids = [t.get("test_id") for t in tests_batch if t.get("test_id")]

                    if not test_files:
                        return True # Nothing to test

                    await self._log("TestDrivenDeveloper", f"Starting {target_type} TDD loop with {len(test_files)} tests (Budget: {budget})...", node_id=node_id)

                    # Build initial messages ONCE — all iterations share this session
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
                        await self._log("TestDrivenDeveloper", f"[{target_type}] Iteration {iteration}/{budget}...", node_id=node_id)

                        final_output, messages = await self.test_driven_developer.run_from_messages(
                            messages, node_id=node_id, max_steps=15, tools=tools
                        )

                        if "IMPLEMENTED" in final_output:
                            await self._log("System", f"[{target_type}] All tests passed! TDD loop completed successfully.", node_id=node_id)

                            # Mark specifically related interfaces as implemented based on this batch
                            try:
                                update_test_implemented_status(test_ids)
                                await self._log("System", f"Database updated: Interfaces covered by {target_type} tests marked as implemented.", node_id=node_id)
                            except Exception as db_err:
                                await self._log("System", f"Warning: Failed to update DB status for {target_type} batch: {str(db_err)}", node_id=node_id)

                            return True
                        else:
                            # Detect repeated failures to avoid wasting budget on the same error
                            error_sig = final_output[:200] if final_output else ""
                            if error_sig == last_error_sig:
                                consecutive_same_errors += 1
                            else:
                                consecutive_same_errors = 0
                                last_error_sig = error_sig

                            if consecutive_same_errors >= 2:
                                await self._log("System", f"Warning: [{target_type}] Same error repeated 3 consecutive times. Breaking TDD loop early to save budget.", node_id=node_id)
                                break

                            await self._log("System", f"[{target_type}] Tests not yet passing. Continuing in same session...", node_id=node_id)

                            # Nudge the LLM to continue fixing — stays in same session
                            if iteration > 1:
                                modified_files = _extract_modified_files_from_messages(messages)
                                delta_ctx = context_pipeline.build_incremental_context(node_id, modified_files=modified_files)
                                if delta_ctx:
                                    nudge = (
                                        f"The {target_type} tests are still failing. "
                                        f"Here are the files you modified in previous iterations:\n{delta_ctx}\n\n"
                                        f"Analyze the error output above carefully, fix the code, and rerun `run_tests` with type `{target_type}`. "
                                        f"Reply \"IMPLEMENTED\" only when all target tests pass."
                                    )
                                else:
                                    nudge = f"The {target_type} tests are still failing. Analyze the error output above carefully, fix the code, and rerun `run_tests` with type `{target_type}`. Reply \"IMPLEMENTED\" only when all target tests pass."
                            else:
                                nudge = f"The {target_type} tests are still failing. Analyze the error output above carefully, fix the code, and rerun `run_tests` with type `{target_type}`. Reply \"IMPLEMENTED\" only when all target tests pass."
                            messages.append({"role": "user", "content": nudge})

                    await self._log("System", f"Warning: [{target_type}] Max TDD iterations reached without a definitive 'IMPLEMENTED' signal.", node_id=node_id)
                    return False

                # 1. DB & FUNC -> Unit tests
                unit_tests = tests_by_type["Unit"]
                if unit_tests:
                    await run_tdd_loop("Unit", unit_tests, budget=5, preloaded_source=stub_artifacts)

                # 2. API -> Integration tests
                int_tests = tests_by_type["Integration"]
                if int_tests:
                    await run_tdd_loop("Integration", int_tests, budget=5, preloaded_source=stub_artifacts)

                # 3. UI -> E2E tests (One by one per scenario)
                e2e_tests = tests_by_type["E2E"]
                if e2e_tests:
                    # E2E tests are matched to the single scenario
                    for idx, e2e_test in enumerate(e2e_tests):
                        file_path = e2e_test.get("file_path")
                        if not file_path:
                            continue

                        scenario_name = f"Scenario {idx+1}"
                        await self._log("System", f"Running E2E for {scenario_name}...", node_id=node_id)
                        await run_tdd_loop("E2E", [e2e_test], budget=3, scenario=req_scenario, preloaded_source=stub_artifacts)

            else:
                await self._log("System", f"Non-leaf node {node_id}: skipping Test/TDD (shared interfaces only).", node_id=node_id)

            return True

        except Exception as e:
            await self._log("System", f"Workflow failed due to an error: {str(e)}", node_id=node_id)
            return False



async def run_agent_workflow(manager: ARCWorkflowManager, node_id: str, requirement_data: dict):
    """Unified entry point for processing a node using an existing manager"""
    _ = requirement_data
    final_state = await manager.process_node(node_id)
    return final_state
