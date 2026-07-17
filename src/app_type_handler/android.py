import asyncio
import json
import os
import re
import shutil

from core.service import get_runtime
from core.utils import finalize_subprocess, get_android_package, set_android_package

from .base import AppTypeHandler


def _android_file_to_test_class(file_path: str) -> str:
    parts = file_path.replace("\\", "/").split("/")
    for index, part in enumerate(parts):
        if part == "java" and index + 1 < len(parts):
            class_parts = parts[index + 1:]
            if class_parts:
                class_parts[-1] = class_parts[-1].replace(".java", "").replace(".kt", "")
            return ".".join(class_parts)
    return file_path


def _gradlew_cmd() -> str:
    if os.name == "nt":
        return "cmd /c gradlew.bat"
    return "./gradlew"


def _filter_android_gradle_output(output: str, error: str, exit_code: int) -> str:
    raw = output + "\n" + error
    filtered_lines = []
    noise_patterns = [
        "Transforming ", "Compiling XML table", "Compiling file ",
        "Caching disabled", "is not up-to-date", "VCS Checkout Cache",
        "dependencies-accessors", "cleaned up in", "removing files",
        "Watched directory", "Input property ", "Value of input",
        "Merging result: MERGED", "ADDED from", "android:supportsRtl",
        "android:roundIcon", "android:allowBackup", "android:icon",
        "android:label", "android:theme", "android:exported",
        "android:name", "xmlns:android", "intent-filter#",
        "action#", "category#", "See https://developer.android.com",
        "Run with --stacktrace", "Run with --debug", "Run with --scan",
        "Get more help at", "actionable tasks",
    ]
    keep_patterns = [
        "PASSED", "FAILED", "SKIPPED", "BUILD ", "FAILURE:",
        "What went wrong", "Execution failed", "Exception",
        "Caused by", "error:", "Error:", "閿欒:",
        "cannot find symbol", "package does not exist",
        "Merging result: ERROR", "Manifest merger failed",
        "Exit Code", "Test result", "tests,", "no tests found",
        "Task :app:", "at ", "WARNING:",
    ]

    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern in line for pattern in keep_patterns):
            filtered_lines.append(line)
            continue
        if any(pattern in line for pattern in noise_patterns):
            continue
        filtered_lines.append(line)

    result = f"Exit Code: {exit_code}\n"
    filtered = "\n".join(filtered_lines)
    if len(filtered) > 30000:
        result += filtered[:15000] + "\n...[OUTPUT TRUNCATED]...\n" + filtered[-15000:]
    else:
        result += filtered
    return result


async def _run_android_gradle_test(workspace_path: str, file_path: str) -> str:
    command = f'{_gradlew_cmd()} testDebugUnitTest --info --tests "{_android_file_to_test_class(file_path)}"'
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return _filter_android_gradle_output(output, error, process.returncode)
    except asyncio.TimeoutError:
        if process:
            await finalize_subprocess(process, force_kill=True)
        return "Command timed out after 180.0 seconds."
    except Exception as exc:
        return f"Execution failed: {str(exc)}"


async def _run_android_gradle_build(workspace_path: str) -> str:
    command = f"{_gradlew_cmd()} assembleDebug compileDebugUnitTestJavaWithJavac --info"
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180.0)
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        return _filter_android_gradle_output(output, error, process.returncode)
    except asyncio.TimeoutError:
        if process:
            await finalize_subprocess(process, force_kill=True)
        return "Exit Code: 124\nSTDERR:\nCommand timed out after 180.0 seconds.\n"
    except Exception as exc:
        return f"Exit Code: 1\nSTDERR:\nExecution failed: {str(exc)}\n"


class AndroidAppType(AppTypeHandler):
    name = "android"

    @classmethod
    def prerequisite_commands(cls) -> list[str]:
        return ["java"]

    @classmethod
    def runtime_contract_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return [
            "For Android apps, the runtime is the packaged app module built by the Gradle wrapper, not a hosted web server.",
            "The user-visible flow runs through Android components such as `MainActivity`, layouts, ViewModels, repositories, and owned persistence/services.",
            "Unit, integration, and end-to-end verification should align with the app-module test sources under `app/src/test/...`.",
        ]

    @classmethod
    def project_structure_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port
        package_name = android_package or get_android_package()
        package_dir = package_name.replace(".", "/")
        return [
            "- Android structure rules:",
            f"- Main source root: app/src/main/java/{package_dir}/",
            f"- Unit tests: app/src/test/java/{package_dir}/unit/",
            f"- Integration tests: app/src/test/java/{package_dir}/integration/",
            f"- E2E tests: app/src/test/java/{package_dir}/e2e/",
            "- Prefer app entrypoints, activities, fragments, and owner classes before broader search.",
        ]

    @classmethod
    def test_harness_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port
        package_name = android_package or get_android_package()
        package_dir = package_name.replace(".", "/")
        return [
            "Test manifest `type` must be one of `Unit`, `Integration`, or `E2E`.",
            f"Unit tests: place under `app/src/test/java/{package_dir}/unit/`.",
            f"Integration tests: place under `app/src/test/java/{package_dir}/integration/`.",
            f"E2E tests: place under `app/src/test/java/{package_dir}/e2e/`.",
            "Use Java/Kotlin test filenames supported by the Android app handler.",
        ]

    async def run_test_file(self, test_type: str, file_path: str) -> str:
        await self._log("System", f"System test execution ({test_type}): {file_path}")
        return await _run_android_gradle_test(self.workspace_path, file_path)

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )
        return await super().run_test_group(test_type, file_paths)

    async def run_build(self) -> str:
        return await _run_android_gradle_build(self.workspace_path)

    async def post_template_setup(self) -> bool:
        target_package = await self._extract_android_package_name_via_llm()
        if target_package:
            await self._log("System", f"Extracted package name: {target_package}")
            self._setup_android_package(target_package)
        else:
            await self._log("System", "Package extraction failed. Using fallback: com.example.app")
            self._setup_android_package("com.example.app")

        sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
        if sdk_root:
            local_props_path = os.path.join(self.workspace_path, "local.properties")
            sdk_dir_gradle = sdk_root.replace("\\", "/")
            with open(local_props_path, "w", encoding="utf-8") as file:
                file.write(f"sdk.dir={sdk_dir_gradle}\n")
            await self._log("System", f"Wrote local.properties with sdk.dir={sdk_dir_gradle}")

        gradle_props_path = os.path.join(self.workspace_path, "gradle.properties")
        if os.path.exists(gradle_props_path):
            jdk_home = await self._detect_jdk_home()
            if jdk_home:
                jdk_gradle = jdk_home.replace("\\", "/")
                with open(gradle_props_path, "r", encoding="utf-8") as file:
                    props = file.read()
                if "org.gradle.java.home" in props:
                    props = re.sub(
                        r"org\.gradle\.java\.home=.*",
                        f"org.gradle.java.home={jdk_gradle}",
                        props,
                    )
                else:
                    props += f"\norg.gradle.java.home={jdk_gradle}\n"
                with open(gradle_props_path, "w", encoding="utf-8") as file:
                    file.write(props)
                await self._log("System", f"Set org.gradle.java.home={jdk_gradle} in gradle.properties")
            else:
                await self._log(
                    "System",
                    "Could not auto-detect JDK path. Please set org.gradle.java.home in gradle.properties manually.",
                )
        return True

    @classmethod
    def build_stack_block(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> str:
        del web_port, android_package
        return "\n".join(
            [
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
            ]
        )

    @classmethod
    def default_stack_summary(cls) -> str:
        return "platform=Android Native App"

    @classmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        platform = re.search(r"\*\*\s*Platform\s*\*\*\s*:\s*(.+)", metadata_content, re.IGNORECASE)
        if platform:
            return f"platform={platform.group(1).strip()}"
        return cls.default_stack_summary()

    async def _extract_android_package_name_via_llm(self) -> str:
        from core.utils import load_requirements

        all_reqs = []
        if self.requirement_path and os.path.exists(self.requirement_path):
            try:
                data = load_requirements(self.requirement_path)

                def flatten(node, result=None):
                    if result is None:
                        result = []
                    if isinstance(node, dict):
                        result.append(node)
                        for child in node.get("children", []):
                            flatten(child, result)
                    return result

                all_reqs = flatten(data)
            except Exception as exc:
                await self._log("System", f"Failed to read requirements from YAML: {str(exc)}. Trying DB fallback.")
                all_reqs = get_runtime().traceability.list_requirements()
        else:
            all_reqs = get_runtime().traceability.list_requirements()

        desc_texts = []
        for req in all_reqs:
            desc = req.get("description", "")
            if desc:
                if len(desc) > 800:
                    desc = desc[:800] + "..."
                desc_texts.append(f"- [{req.get('id', '?')}] {desc}")

        if not desc_texts:
            return self._fallback_package_name_extraction(all_reqs)

        all_descriptions = "\n".join(desc_texts)
        if len(all_descriptions) > 8000:
            all_descriptions = all_descriptions[:8000] + "\n... (truncated)"

        system_prompt = """You are an Android package and resource analyzer.
Given requirement descriptions that contain resource-id patterns (e.g., `org.billthefarmer.editor:id/newFile`), fully-qualified class names, or other package references, extract:

1. The application's own package name
2. All resource-id mappings (resource name -> UI component type)

Rules for package name:
- Ignore system packages: com.android.*, android.*, com.google.*, androidx.*, java.*, javax.*, kotlin.*
- The app's package is the one that appears most frequently in resource-id patterns or is clearly the application's own package.

Rules for resource-id mapping:
- From patterns like `org.billthefarmer.editor:id/newFile`, extract: newFile -> Button
- Infer the UI component type from the resource name.

Return a JSON object with exactly these fields:
{
  "package_name": "the.app.package.name",
  "resource_ids": {
    "resourceName": "ComponentType"
  }
}

If no app package can be identified, set package_name to "UNKNOWN"."""

        user_prompt = (
            "Analyze these requirement descriptions and extract the Android package name and "
            f"resource-id mappings:\n\n{all_descriptions}\n\n"
            'Return a JSON object with "package_name" and "resource_ids" fields.'
        )

        try:
            client = self.interface_designer.client
            model = self.interface_designer.model
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.0,
                ),
                timeout=60.0,
            )
            result_text = response.choices[0].message.content.strip()
            json_match = re.search(r"\{[\s\S]*\}", result_text)
            if not json_match:
                await self._log("System", "Package extraction: no JSON found in LLM response, using fallback")
                return self._fallback_package_name_extraction(all_reqs)

            parsed = json.loads(json_match.group())
            package_name = parsed.get("package_name", "UNKNOWN")
            resource_ids = parsed.get("resource_ids", {})
            package_name = package_name.strip().strip("`").strip('"').strip("'")
            if package_name == "UNKNOWN" or not package_name or "." not in package_name:
                return self._fallback_package_name_extraction(all_reqs)
            for segment in package_name.split("."):
                if not segment or not (segment[0].isalpha() or segment[0] == "_"):
                    return self._fallback_package_name_extraction(all_reqs)

            await self._log("System", f"LLM extracted package name: {package_name}")
            if resource_ids:
                await self._log("System", f"LLM extracted {len(resource_ids)} resource-id mappings")
            return package_name
        except Exception as exc:
            await self._log("System", f"Package extraction via LLM failed: {str(exc)}")
            return self._fallback_package_name_extraction(all_reqs)

    def _write_android_package_metadata(self, package_name: str, resource_ids: dict):
        return None

    def _fallback_package_name_extraction(self, all_reqs: list) -> str:
        for req in all_reqs:
            desc = req.get("description", "")
            matches = re.findall(r"`([a-z][a-z0-9_.]*):id/[a-zA-Z0-9_]+`", desc)
            for match in matches:
                if match.startswith(("com.android.", "android.", "com.google.", "androidx.")):
                    continue
                if "." in match and len(match.split(".")) >= 2:
                    return match

        project_name = os.path.basename(self.workspace_path).lower()
        project_name = re.sub(r"[^a-z0-9]", "", project_name)
        if project_name:
            return f"com.{project_name}.app"
        return "com.example.app"

    def _setup_android_package(self, target_package: str):
        workspace_path = self.workspace_path
        pkg_dir = target_package.replace(".", "/")

        build_gradle_path = os.path.join(workspace_path, "app", "build.gradle")
        if os.path.exists(build_gradle_path):
            with open(build_gradle_path, "r", encoding="utf-8") as file:
                content = file.read()
            content = content.replace("namespace 'com.example.template'", f"namespace '{target_package}'")
            content = content.replace("namespace ''", f"namespace '{target_package}'")
            content = content.replace('applicationId "com.example.template"', f'applicationId "{target_package}"')
            content = content.replace('applicationId ""', f'applicationId "{target_package}"')
            with open(build_gradle_path, "w", encoding="utf-8") as file:
                file.write(content)

        manifest_path = os.path.join(workspace_path, "app", "src", "main", "AndroidManifest.xml")
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as file:
                content = file.read()
            content = content.replace('package="com.example.template"', f'package="{target_package}"')
            content = content.replace('package=""', f'package="{target_package}"')
            with open(manifest_path, "w", encoding="utf-8") as file:
                file.write(content)

        info_path = os.path.join(workspace_path, "template_info.json")
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as file:
                content = file.read()
            content = content.replace('"package_name": "com.example.template"', f'"package_name": "{target_package}"')
            with open(info_path, "w", encoding="utf-8") as file:
                file.write(content)

        template_src_base = os.path.join(
            workspace_path, "app", "src", "main", "java", "com", "example", "template"
        )
        template_test_base = os.path.join(
            workspace_path, "app", "src", "test", "java", "com", "example", "template"
        )
        new_test_base = os.path.join(workspace_path, "app", "src", "test", "java", pkg_dir)
        examples_main = os.path.join(workspace_path, ".arc", "examples", "main")
        examples_test = os.path.join(workspace_path, ".arc", "examples", "test")
        os.makedirs(examples_main, exist_ok=True)
        os.makedirs(examples_test, exist_ok=True)

        def clean_empty_parents(path, stop_at):
            while path and path != stop_at:
                if os.path.isdir(path) and not os.listdir(path):
                    os.rmdir(path)
                    path = os.path.dirname(path)
                else:
                    break

        if os.path.exists(template_src_base):
            for root, _, files in os.walk(template_src_base, topdown=False):
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, template_src_base)
                    dst = os.path.join(examples_main, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    os.remove(src)
            shutil.rmtree(template_src_base, ignore_errors=True)
            clean_empty_parents(
                os.path.dirname(template_src_base),
                os.path.join(workspace_path, "app", "src", "main", "java"),
            )

        if os.path.exists(template_test_base):
            for root, _, files in os.walk(template_test_base, topdown=False):
                for fname in files:
                    src = os.path.join(root, fname)
                    rel = os.path.relpath(src, template_test_base)
                    is_test_class = fname.endswith("Test.java") or fname.endswith("Test.kt")
                    if is_test_class:
                        dst = os.path.join(examples_test, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                    else:
                        new_path = os.path.join(new_test_base, rel)
                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                        if fname.endswith(".java") or fname.endswith(".kt"):
                            with open(src, "r", encoding="utf-8") as file:
                                content = file.read()
                            content = content.replace("package com.example.template", f"package {target_package}")
                            content = content.replace("import com.example.template", f"import {target_package}")
                            with open(new_path, "w", encoding="utf-8") as file:
                                file.write(content)
                        else:
                            shutil.copy2(src, new_path)
                    os.remove(src)
            shutil.rmtree(template_test_base, ignore_errors=True)
            clean_empty_parents(
                os.path.dirname(template_test_base),
                os.path.join(workspace_path, "app", "src", "test", "java"),
            )

        for subdir in ("unit", "integration", "e2e"):
            path = os.path.join(new_test_base, subdir)
            os.makedirs(path, exist_ok=True)
            if not any(name for name in os.listdir(path) if not name.startswith(".")):
                with open(os.path.join(path, ".gitkeep"), "w", encoding="utf-8") as file:
                    file.write("")

        set_android_package(target_package)

    async def _detect_jdk_home(self) -> str:
        java_home = os.environ.get("JAVA_HOME")
        if java_home and os.path.isdir(java_home):
            return java_home

        common_paths = [
            "D:/JDK/jdk21.0.6",
            "D:/JDK/jdk-21",
            "C:/Program Files/Java/jdk-21",
            "C:/Program Files/Eclipse Adoptium/jdk-21",
            "/usr/lib/jvm/java-21",
            "/usr/lib/jvm/jdk-21",
        ]
        for path in common_paths:
            if os.path.isdir(path):
                return path

        try:
            process = None
            process = await asyncio.create_subprocess_shell(
                "java -XshowSettings:properties -version 2>&1 | grep 'java.home'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
            output = stdout.decode("utf-8", errors="replace")
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("java.home"):
                    path = line.split("=", 1)[1].strip()
                    if os.path.isdir(path):
                        return path
        except Exception:
            await finalize_subprocess(process, force_kill=True)
            pass

        return ""
