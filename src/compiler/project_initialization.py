from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from arcbench_agent_runtime.jsonio import write_json_atomic
from core.logging import SynchronousLog

from .integration_runner import integration_runner_source
from .process_utils import process_group_kwargs, resolve_executable, terminate_process_tree


PROJECT_STATUS = "PROJECT_INITIALIZED"
PROJECT_PROFILE_ID = "web-react18-tailwind4-express-drizzle-sqlite"


class ProjectInitializationError(RuntimeError):
    """A deterministic project initialization failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DependencyCatalog:
    """Compiler-owned, reproducible versions for the generated web workspace."""

    node_major: str = "22.12.0"
    npm_minimum: str = "10.9.0"
    typescript: str = "5.9.2"
    react: str = "18.3.1"
    react_dom: str = "18.3.1"
    react_router_dom: str = "7.8.2"
    vite: str = "7.1.4"
    vite_react_plugin: str = "5.0.2"
    tailwindcss: str = "4.1.13"
    tailwindcss_vite: str = "4.1.13"
    express: str = "5.1.0"
    zod: str = "4.1.5"
    drizzle_orm: str = "0.44.5"
    drizzle_kit: str = "0.31.4"
    better_sqlite3: str = "12.2.0"
    tsx: str = "4.20.5"
    types_node: str = "22.18.0"
    types_react: str = "18.3.23"
    types_react_dom: str = "18.3.7"
    types_express: str = "5.0.3"
    types_better_sqlite3: str = "7.6.13"
    vitest: str = "3.2.4"
    playwright: str = "1.55.0"
    supertest: str = "7.1.4"
    types_supertest: str = "6.0.3"

    def to_dict(self) -> dict[str, str]:
        return {
            "node": self.node_major,
            "npm": self.npm_minimum,
            "typescript": self.typescript,
            "react": self.react,
            "react-dom": self.react_dom,
            "react-router-dom": self.react_router_dom,
            "vite": self.vite,
            "@vitejs/plugin-react": self.vite_react_plugin,
            "tailwindcss": self.tailwindcss,
            "@tailwindcss/vite": self.tailwindcss_vite,
            "express": self.express,
            "zod": self.zod,
            "drizzle-orm": self.drizzle_orm,
            "drizzle-kit": self.drizzle_kit,
            "better-sqlite3": self.better_sqlite3,
            "tsx": self.tsx,
            "@types/node": self.types_node,
            "@types/react": self.types_react,
            "@types/react-dom": self.types_react_dom,
            "@types/express": self.types_express,
            "@types/better-sqlite3": self.types_better_sqlite3,
            "vitest": self.vitest,
            "@playwright/test": self.playwright,
            "supertest": self.supertest,
            "@types/supertest": self.types_supertest,
        }


def frontend_css_source() -> str:
    """Return the compiler-owned Tailwind entry shared by every web workspace."""

    return (
        '@import "tailwindcss";\n\n'
        '@layer base {\n'
        '  :root { font-family: Inter, ui-sans-serif, system-ui, sans-serif; color-scheme: light; '
        '--arc-ink: #0f172a; --arc-muted: #64748b; --arc-accent: #f97316; }\n'
        '  * { box-sizing: border-box; }\n'
        '  html { min-width: 320px; background: #f8fafc; }\n'
        '  body { margin: 0; min-width: 320px; min-height: 100vh; background: #f8fafc; color: var(--arc-ink); }\n'
        '  button, input, select, textarea { font: inherit; }\n'
        '  ::selection { background: #fed7aa; color: #7c2d12; }\n'
        '}\n'
    )


def validate_frontend_environment(
    output_root: Path,
    project_manifest: dict[str, Any],
    *,
    catalog: DependencyCatalog | None = None,
) -> list[str]:
    """Reject reused projects whose pinned Tailwind toolchain is absent or stale."""

    expected = catalog or DependencyCatalog()
    errors: list[str] = []
    if project_manifest.get("profile") != PROJECT_PROFILE_ID:
        errors.append(
            "ARC3202 FRONTEND_ENVIRONMENT_INVALID: project profile does not include "
            "the compiler-owned Tailwind CSS v4 environment."
        )
    frontend_environment = project_manifest.get("frontendEnvironment")
    expected_versions = {
        "tailwindcss": expected.tailwindcss,
        "@tailwindcss/vite": expected.tailwindcss_vite,
    }
    if not isinstance(frontend_environment, dict) or (
        frontend_environment.get("status") != "FRONTEND_ENVIRONMENT_READY"
        or frontend_environment.get("styling") != "tailwindcss-4"
        or frontend_environment.get("cssEntry") != "frontend/src/index.css"
        or frontend_environment.get("versions") != expected_versions
    ):
        errors.append(
            "ARC3202 FRONTEND_ENVIRONMENT_INVALID: frontend environment manifest "
            "does not match the pinned Tailwind CSS v4 configuration."
        )

    package_path = output_root / "frontend" / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(
            f"ARC3202 FRONTEND_ENVIRONMENT_INVALID: cannot read frontend/package.json: {exc}"
        )
        package = {}
    dev_dependencies = package.get("devDependencies", {})
    for name, version in expected_versions.items():
        if not isinstance(dev_dependencies, dict) or dev_dependencies.get(name) != version:
            errors.append(
                "ARC3202 FRONTEND_ENVIRONMENT_INVALID: "
                f"frontend devDependency {name!r} must be pinned to {version}."
            )

    css_path = output_root / "frontend" / "src" / "index.css"
    try:
        css = css_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(
            f"ARC3202 FRONTEND_ENVIRONMENT_INVALID: cannot read frontend/src/index.css: {exc}"
        )
    else:
        if css != frontend_css_source():
            errors.append(
                "ARC3202 FRONTEND_ENVIRONMENT_INVALID: frontend/src/index.css differs "
                "from the compiler-owned Tailwind entry."
            )

    for relative in (
        "node_modules/tailwindcss/package.json",
        "node_modules/@tailwindcss/vite/package.json",
    ):
        if not (output_root / relative).is_file():
            errors.append(
                f"ARC3202 FRONTEND_ENVIRONMENT_INVALID: missing installed {relative}."
            )
    return list(dict.fromkeys(errors))


def test_workspace_spec(
    catalog: DependencyCatalog,
    *,
    backend_port: int,
) -> dict[str, Any]:
    """Return the compiler-owned test workspace created during project initialization."""

    port = max(1, min(65535, int(backend_port)))
    return {
        "package": {
            "name": "@arc/tests",
            "private": True,
            "version": "0.0.0",
            "type": "module",
            "scripts": {
                "typecheck": "tsc --noEmit -p tsconfig.json",
                "list:vitest": "vitest list --passWithNoTests --config vitest.config.ts",
                "list:e2e": (
                    "playwright test --list --pass-with-no-tests "
                    "--config playwright.config.ts"
                ),
                "test:unit": "vitest run --config vitest.config.ts unit",
                "test:integration": "node ./support/run-integration.mjs",
                "test:e2e": "playwright test --config playwright.config.ts",
            },
            "devDependencies": {
                "@playwright/test": catalog.playwright,
                "@types/node": catalog.types_node,
                "@types/supertest": catalog.types_supertest,
                "supertest": catalog.supertest,
                "typescript": catalog.typescript,
                "vitest": catalog.vitest,
            },
        },
        "tsconfig": {
            "compilerOptions": {
                "target": "ES2022",
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "strict": True,
                "noUncheckedIndexedAccess": True,
                "exactOptionalPropertyTypes": True,
                "esModuleInterop": True,
                "resolveJsonModule": True,
                "skipLibCheck": True,
                "noEmit": True,
                "types": ["node", "vitest/globals"],
            },
            "include": ["**/*.ts"],
            "exclude": ["node_modules"],
        },
        "text_files": {
            "vitest.config.ts": (
                'import { defineConfig } from "vitest/config";\n\n'
                "export default defineConfig({\n"
                "  test: {\n"
                '    include: ["unit/**/*.spec.ts", "integration/**/*.spec.ts"],\n'
                '    environment: "node",\n'
                "    fileParallelism: false,\n"
                "    maxWorkers: 1,\n"
                "    testTimeout: 15_000,\n"
                "    hookTimeout: 15_000,\n"
                '    setupFiles: process.env.ARC_TEST_LAYER === "INTEGRATION" ? ["./support/integration-setup.ts"] : ["./support/setup.ts"],\n'
                "  },\n"
                "});\n"
            ),
            "playwright.config.ts": (
                'import { defineConfig, devices } from "@playwright/test";\n\n'
                f'const e2ePort = process.env.ARC_E2E_PORT ?? "{port}";\n'
                'const baseURL = process.env.ARC_TEST_BASE_URL ?? "http://127.0.0.1:" + e2ePort;\n\n'
                "export default defineConfig({\n"
                '  reporter: [["line"], ["./support/progress-reporter.ts"]],\n'
                '  testDir: "./e2e",\n'
                "  fullyParallel: false,\n"
                "  workers: 1,\n"
                "  timeout: 10_000,\n"
                "  expect: { timeout: 10_000 },\n"
                "  use: {\n"
                '    baseURL,\n'
                '    trace: "retain-on-failure",\n'
                '    screenshot: "only-on-failure",\n'
                "  },\n"
                '  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],\n'
                "  webServer: {\n"
                '      command: "npm run build -w @arc/frontend && npm run build -w @arc/backend && node ./support/server.mjs",\n'
                '      url: "http://127.0.0.1:" + e2ePort + "/__arc/health",\n'
                '      env: { DATABASE_URL: ":memory:", NODE_ENV: "test", PORT: e2ePort },\n'
                "      reuseExistingServer: false,\n"
                "      timeout: 60_000,\n"
                '      stdout: "pipe",\n'
                '      stderr: "pipe",\n'
                "    },\n"
                "});\n"
            ),
            "support/runtime.ts": (
                'import type { Page } from "@playwright/test";\n\n'
                "export function uniqueValue(prefix: string): string {\n"
                "  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;\n"
                "}\n\n"
                f'export async function resetE2EState(page: Page): Promise<void> {{\n'
                f'  const baseUrl = process.env.ARC_TEST_BASE_URL ?? "http://127.0.0.1:{port}";\n'
                '  const response = await fetch(`${baseUrl}/__arc/reset`, { method: "POST" });\n'
                '  if (!response.ok) throw new Error(`PRECONDITION: database reset/fixture restore failed: ${response.status} ${await response.text()}`);\n'
                '  await page.context().clearCookies();\n'
                '  await page.goto(`${baseUrl}/__arc/health`);\n'
                '  await page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); });\n'
                '}\n'
            ),
            "support/e2e.ts": (
                "import { test as base, expect } from \"@playwright/test\";\n"
                "import { resetE2EState } from \"./runtime.js\";\n"
                "\n"
                "export const test = base.extend<{ arcIsolation: void }>({\n"
                "  arcIsolation: [async ({ page }, use) => {\n"
                "    page.on(\"console\", message => { if (message.type() === \"error\") console.error(message.text()); });\n"
                "    page.on(\"pageerror\", error => console.error(error));\n"
                "    page.on(\"requestfailed\", request => console.error(request.url(), request.failure()?.errorText));\n"
                "    await resetE2EState(page);\n"
                "    await use();\n"
                "  }, { auto: true }],\n"
                "});\n"
                "export { expect };\n"
            ),
            "support/progress-reporter.ts": (
                "import type { Reporter, TestCase, TestResult, TestStep } from \"@playwright/test/reporter\";\n"
                "\n"
                "class ProgressReporter implements Reporter {\n"
                "  printsToStdio(): boolean { return true; }\n"
                "  onTestBegin(test: TestCase): void {\n"
                "    console.error(`Running ${test.location.file}:${test.location.line} ${test.title}`);\n"
                "  }\n"
                "  onStepBegin(test: TestCase, _result: TestResult, step: TestStep): void {\n"
                "    const location = step.location ?? test.location;\n"
                "    console.error(`${location.file}:${location.line} ${step.title}`);\n"
                "  }\n"
                "}\n"
                "\n"
                "export default ProgressReporter;\n"
            ),
            "support/run-integration.mjs": integration_runner_source(),
            "support/server.mjs": (
                '// Generated by ARC. Dedicated test process; never use the application database.\n'
                'process.env.DATABASE_URL = ":memory:";\n'
                'process.env.NODE_ENV = "test";\n'
                'const { createServer } = await import("node:http");\n'
                'const { resetToBaseline } = await import("../../backend/database-baseline.mjs");\n'
                'const { sqliteDatabase } = await import("../../backend/dist/db/client.js");\n'
                'const { app } = await import("../../backend/dist/app.js");\n'
                'const server = createServer((request, response) => {\n'
                '  if (request.method === "POST" && request.url === "/__arc/reset") {\n'
                '    try {\n'
                '      resetToBaseline(sqliteDatabase);\n'
                '      response.writeHead(204).end();\n'
                '    } catch (error) {\n'
                '      console.error(error);\n'
                '      response.writeHead(500).end("PRECONDITION: database reset/fixture restore failed: " + String(error));\n'
                '    }\n'
                '    return;\n'
                '  }\n'
                '  app(request, response);\n'
                '});\n'
                'server.listen(Number(process.env.PORT ?? "3000"), "127.0.0.1");\n'
            ),
            "support/integration-setup.ts": (
                'import { beforeEach } from "vitest";\n\n'
                "const baseUrl = process.env.ARC_TEST_BASE_URL;\n"
                'if (!baseUrl) throw new Error("Integration tests require npm run test:integration.");\n\n'
                "beforeEach(async () => {\n"
                '  const response = await fetch(`${baseUrl}/__arc/reset`, { method: "POST" });\n'
                '  if (!response.ok) throw new Error(`PRECONDITION: database reset/fixture restore failed: ${response.status} ${await response.text()}`);\n'
                "});\n"
            ),
            "support/setup.ts": (
                'process.env.DATABASE_URL = ":memory:";\n'
                'process.env.NODE_ENV = "test";\n\n'
                'import { beforeEach } from "vitest";\n\n'
                'beforeEach(async () => {\n'
                '  const { sqliteDatabase } = await import("../../backend/src/db/client.js");\n'
                '  const { resetToBaseline } = await import("../../backend/database-baseline.mjs");\n'
                '  resetToBaseline(sqliteDatabase);\n'
                '});\n'
            ),
        },
    }


@dataclass(slots=True)
class ProjectInitializationResult:
    ok: bool
    status: str
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class ProjectInitializer:
    """Materialize the fixed, buildable workspace before requirement processing."""

    _DEFAULT_COMMAND_TIMEOUT_SECONDS = 300.0
    _COMMAND_HEARTBEAT_SECONDS = 15.0

    _PROJECT_TARGETS = (
        "package.json",
        "package-lock.json",
        "node_modules",
        ".git",
        ".gitignore",
        "README.md",
        ".env.example",
        "frontend",
        "backend",
        "shared",
        "tests",
    )

    def __init__(
        self,
        output_root: Path,
        *,
        web_port: int,
        catalog: DependencyCatalog | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.output_root = output_root.expanduser().resolve()
        self.web_port = int(web_port)
        self.catalog = catalog or DependencyCatalog()
        self.environment = dict(os.environ if environment is None else environment)
        self.arc_root = self.output_root / ".arc"
        self.staging_root = self.arc_root / "staging" / "project-init"
        self.staged_project = self.staging_root / "project"
        self.project_artifact_root = self.arc_root / "project"
        self._promoted_targets: list[Path] = []
        self._project_artifact_owned = False
        self._test_browser_installed = False
        self._log = SynchronousLog("ProjectInitializer", workspace_root=self.output_root)
        self._command_timeout_seconds = self._read_command_timeout()

    def _read_command_timeout(self) -> float:
        """Read a bounded timeout so a registry outage cannot look like a hang."""

        raw = self.environment.get("ARC_PROJECT_COMMAND_TIMEOUT_SECONDS", "")
        if not raw.strip():
            return self._DEFAULT_COMMAND_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError:
            self._log.info(
                "Invalid ARC_PROJECT_COMMAND_TIMEOUT_SECONDS; using "
                f"{self._DEFAULT_COMMAND_TIMEOUT_SECONDS:g}s."
            )
            return self._DEFAULT_COMMAND_TIMEOUT_SECONDS
        # A very small timeout makes npm fail before it can produce useful
        # diagnostics, while an unbounded value recreates the old behaviour.
        return max(30.0, min(value, 1800.0))

    def initialize(self) -> ProjectInitializationResult:
        try:
            self._validate_toolchain()
            self._validate_target()
            self._prepare_staging()
            self._run_official_initializers()
            self._normalize_workspace()
            self._create_lockfile()
            self._validate_staged_project()
            self._promote()
            self._install_promoted_workspace()
            artifacts = self._emit_manifests()
            self._cleanup_staging()
            return ProjectInitializationResult(ok=True, status=PROJECT_STATUS, artifacts=artifacts)
        except ProjectInitializationError as exc:
            self._rollback_promoted_targets()
            self._cleanup_staging()
            return ProjectInitializationResult(
                ok=False,
                status="PROJECT_INITIALIZATION_FAILED",
                errors=[str(exc)],
            )
        except OSError as exc:
            self._rollback_promoted_targets()
            self._cleanup_staging()
            return ProjectInitializationResult(
                ok=False,
                status="PROJECT_INITIALIZATION_FAILED",
                errors=[f"PROJECT_SCAFFOLD_FAILED: {exc}"],
            )
        except Exception as exc:  # Keep unexpected initializer faults inside the compiler stage boundary.
            self._rollback_promoted_targets()
            self._cleanup_staging()
            return ProjectInitializationResult(
                ok=False,
                status="PROJECT_INITIALIZATION_FAILED",
                errors=[f"PROJECT_INITIALIZATION_INTERNAL_ERROR: {exc}"],
            )

    def _validate_target(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        conflicts = [name for name in self._PROJECT_TARGETS if (self.output_root / name).exists()]
        if self.project_artifact_root.exists():
            conflicts.append(".arc/project")
        if conflicts:
            raise ProjectInitializationError(
                "PROJECT_TARGET_CONFLICT",
                f"Project target already exists: {', '.join(conflicts)}",
            )

    def _validate_toolchain(self) -> None:
        command_root = self.output_root if self.output_root.is_dir() else self.output_root.parent
        node = self._run(["node", "--version"], cwd=command_root)
        npm = self._run(["npm", "--version"], cwd=command_root)
        actual_node = self._parse_version(node.stdout.strip().lstrip("v"))
        required_node = self._parse_version(self.catalog.node_major)
        if actual_node < required_node:
            raise ProjectInitializationError(
                "PROJECT_PROFILE_INVALID",
                f"Node.js {self.catalog.node_major}+ is required, found {node.stdout.strip()!r}.",
            )
        actual_npm = self._parse_version(npm.stdout.strip())
        required_npm = self._parse_version(self.catalog.npm_minimum)
        if actual_npm < required_npm:
            raise ProjectInitializationError(
                "PROJECT_PROFILE_INVALID",
                f"npm {self.catalog.npm_minimum}+ is required, found {npm.stdout.strip()!r}.",
            )

    def _prepare_staging(self) -> None:
        self._cleanup_staging()
        self.staged_project.mkdir(parents=True, exist_ok=False)

    def _run_official_initializers(self) -> None:
        self._run(["npm", "init", "-y"], cwd=self.staged_project)
        self._run(
            [
                "npm",
                "create",
                "vite",
                "frontend",
                "--",
                "--no-interactive",
                "--template",
                "react-ts",
                "--no-eslint",
                "--no-immediate",
            ],
            cwd=self.staged_project,
        )
        for workspace in ("backend", "shared"):
            workspace_root = self.staged_project / workspace
            workspace_root.mkdir(parents=True, exist_ok=False)
            self._run(["npm", "init", "-y"], cwd=workspace_root)
        # _normalize_workspace writes the canonical backend/shared tsconfig
        # files. Running npm exec tsc --init here would only perform extra
        # registry lookups and immediately produce files that are overwritten.

    def _normalize_workspace(self) -> None:
        self._write_json(self.staged_project / "package.json", self._root_package())
        self._write_json(self.staged_project / "frontend" / "package.json", self._frontend_package())
        self._write_json(self.staged_project / "backend" / "package.json", self._backend_package())
        self._write_json(self.staged_project / "shared" / "package.json", self._shared_package())
        test_spec = test_workspace_spec(self.catalog, backend_port=self.web_port)
        tests_root = self.staged_project / "tests"
        for directory in ("unit", "integration", "e2e", "support"):
            (tests_root / directory).mkdir(parents=True, exist_ok=True)
        self._write_json(tests_root / "package.json", test_spec["package"])
        self._write_json(tests_root / "tsconfig.json", test_spec["tsconfig"])
        for relative, content in test_spec["text_files"].items():
            self._write_text(tests_root / relative, content)

        frontend_root = self.staged_project / "frontend"
        for relative in ("public", "eslint.config.js", "README.md", ".gitignore"):
            self._remove_exact(frontend_root / relative)
        frontend_src = self.staged_project / "frontend" / "src"
        for relative in ("assets", "App.css"):
            self._remove_exact(frontend_src / relative)
        for directory in ("api", "app", "components", "pages", "runtime"):
            (frontend_src / directory).mkdir(parents=True, exist_ok=True)
        (frontend_src / "app" / "stores").mkdir(parents=True, exist_ok=True)
        self._write_text(
            frontend_src / "App.tsx",
            'export default function App() {\n  return <div id="arc-app" />;\n}\n',
        )
        self._write_text(
            frontend_src / "index.css",
            frontend_css_source(),
        )
        self._write_text(
            frontend_src / "main.tsx",
            'import { StrictMode } from "react";\n'
            'import { createRoot } from "react-dom/client";\n'
            'import "./index.css";\n'
            'import App from "./App";\n\n'
            'createRoot(document.getElementById("root")!).render(\n'
            '  <StrictMode>\n    <App />\n  </StrictMode>,\n);\n',
        )

        backend_src = self.staged_project / "backend" / "src"
        backend_src.mkdir(parents=True, exist_ok=True)
        self._write_text(
            backend_src / "server.ts",
            'import express from "express";\n'
            'import { fileURLToPath } from "node:url";\n\n'
            'const app = express();\n'
            'const frontendDist = fileURLToPath(new URL("../../frontend/dist/", import.meta.url));\n'
            'app.get("/__arc/health", (_request, response) => response.json({ status: "ok" }));\n'
            'app.use(express.static(frontendDist));\n'
            'app.use((_request, response) => response.sendFile("index.html", { root: frontendDist }));\n'
            f'app.listen(Number(process.env.PORT ?? {self.web_port}));\n',
        )
        self._write_json(self.staged_project / "backend" / "tsconfig.json", self._backend_tsconfig())
        self._write_text(
            self.staged_project / "backend" / "drizzle.config.ts",
            'import { defineConfig } from "drizzle-kit";\n\n'
            'export default defineConfig({\n'
            '  dialect: "sqlite",\n'
            '  schema: "./src/db/schema/index.ts",\n'
            '  out: "./drizzle",\n'
            '  dbCredentials: {\n'
            '    url: process.env.DATABASE_URL ?? "./data/app.db",\n'
            '  },\n'
            '});\n',
        )

        shared_src = self.staged_project / "shared" / "src"
        (shared_src / "contracts").mkdir(parents=True, exist_ok=True)
        self._write_text(shared_src / "index.ts", "export {};\n")
        self._write_json(self.staged_project / "shared" / "tsconfig.json", self._shared_tsconfig())

        self._write_text(
            self.staged_project / ".env.example",
            f"PORT={self.web_port}\nDATABASE_URL=./data/app.db\n",
        )
        self._write_text(
            self.staged_project / ".gitignore",
            "node_modules/\ndist/\n*.tsbuildinfo\ndata/\n.env\n"
            ".arc/debug.log\n.arc/runner-events.jsonl\n"
            ".arc/model_logs/\n.arc/runtime/test-output/\n",
        )
        self._write_text(
            self.staged_project / "README.md",
            "# Generated application\n\n"
            "Generated by the ARC requirement compiler.\n\n"
            "- Build: \u0060npm run build\u0060\n"
            "- Typecheck: \u0060npm run typecheck\u0060\n"
            "- Start: \u0060npm run start -w @arc/backend\u0060\n",
        )
        for lockfile in self.staged_project.glob("*/package-lock.json"):
            self._remove_exact(lockfile)

    def _create_lockfile(self) -> None:
        self._run(
            ["npm", "install", "--package-lock-only", "--ignore-scripts"],
            cwd=self.staged_project,
        )

    def _install_promoted_workspace(self) -> None:
        node_modules = self.output_root / "node_modules"
        if node_modules.exists():
            raise ProjectInitializationError(
                "PROJECT_TARGET_CONFLICT",
                f"Project target appeared during initialization: {node_modules}",
            )
        # npm workspace links are absolute junctions on Windows. They must be
        # created after promotion or they continue pointing into .arc/staging.
        self._promoted_targets.append(node_modules)
        self._run(["npm", "ci"], cwd=self.output_root)

        shared_link = node_modules / "@arc" / "shared"
        expected_shared = (self.output_root / "shared").resolve()
        actual_shared = shared_link.resolve()
        if not shared_link.exists() or actual_shared != expected_shared:
            raise ProjectInitializationError(
                "PROJECT_INSTALL_FAILED",
                "npm did not create a valid @arc/shared workspace link in the promoted project: "
                f"expected {expected_shared}, resolved {actual_shared}.",
            )

        tests_link = node_modules / "@arc" / "tests"
        expected_tests = (self.output_root / "tests").resolve()
        actual_tests = tests_link.resolve()
        if not tests_link.exists() or actual_tests != expected_tests:
            raise ProjectInitializationError(
                "PROJECT_INSTALL_FAILED",
                "npm did not create a valid @arc/tests workspace link in the promoted project: "
                f"expected {expected_tests}, resolved {actual_tests}.",
            )

    def _validate_staged_project(self) -> None:
        expected = (
            "package.json",
            "package-lock.json",
            "frontend/package.json",
            "frontend/src/main.tsx",
            "frontend/src/index.css",
            "backend/package.json",
            "backend/tsconfig.json",
            "backend/drizzle.config.ts",
            "shared/package.json",
            "shared/src/index.ts",
            "tests/package.json",
            "tests/tsconfig.json",
            "tests/vitest.config.ts",
            "tests/playwright.config.ts",
            "tests/support/runtime.ts",
            "tests/support/e2e.ts",
            "tests/support/progress-reporter.ts",
            "tests/support/run-integration.mjs",
            "tests/support/integration-setup.ts",
            "tests/support/setup.ts",
        )
        missing = [relative for relative in expected if not (self.staged_project / relative).exists()]
        if missing:
            raise ProjectInitializationError(
                "PROJECT_NORMALIZATION_FAILED",
                f"Normalized project is missing: {', '.join(missing)}",
            )
        lockfile_candidates = (
            self.staged_project / "package-lock.json",
            *self.staged_project.glob("*/package-lock.json"),
        )
        lockfiles = [path for path in lockfile_candidates if path.is_file()]
        expected_lockfile = self.staged_project / "package-lock.json"
        if len(lockfiles) != 1 or lockfiles[0] != expected_lockfile:
            relative = [path.relative_to(self.staged_project).as_posix() for path in lockfiles]
            raise ProjectInitializationError(
                "PROJECT_LOCKFILE_FAILED",
                f"Expected exactly one root lockfile, found: {relative}",
            )
        backend_tsconfig = self._read_json(self.staged_project / "backend" / "tsconfig.json")
        if backend_tsconfig.get("references") != [{"path": "../shared"}]:
            raise ProjectInitializationError(
                "PROJECT_NORMALIZATION_FAILED",
                "backend/tsconfig.json must reference the shared TypeScript project.",
            )
        for package_path in self.staged_project.glob("*/package.json"):
            package = self._read_json(package_path)
            for section in ("dependencies", "devDependencies"):
                for name, version in package.get(section, {}).items():
                    if name == "@arc/shared" and version == "*":
                        continue
                    if not isinstance(version, str) or version.startswith(("^", "~")) or version == "latest":
                        raise ProjectInitializationError(
                            "PROJECT_PROFILE_INVALID",
                            f"Dependency {name!r} in {package_path.name} is not pinned: {version!r}",
                        )

    def _promote(self) -> None:
        for item in sorted(self.staged_project.iterdir(), key=lambda value: value.name):
            target = self.output_root / item.name
            if target.exists():
                raise ProjectInitializationError(
                    "PROJECT_TARGET_CONFLICT",
                    f"Project target appeared during initialization: {target}",
                )
            try:
                shutil.move(str(item), str(target))
            except OSError as exc:
                raise ProjectInitializationError(
                    "PROJECT_SCAFFOLD_FAILED",
                    f"Cannot promote staged project target {item.name}: {exc}",
                ) from exc
            self._promoted_targets.append(target)

    def _emit_manifests(self) -> dict[str, str]:
        project_manifest_path = self.project_artifact_root / "project-manifest.json"
        project_manifest = {
            "schemaVersion": 1,
            "status": PROJECT_STATUS,
            "profile": PROJECT_PROFILE_ID,
            "layout": "npm-workspaces",
            "packageManager": "npm",
            "lockfile": "package-lock.json",
            "testEnvironment": {
                "status": "TEST_ENVIRONMENT_PENDING",
                "workspace": "tests",
                "browserInstalled": self._test_browser_installed,
                "versions": {
                    "typescript": self.catalog.typescript,
                    "vitest": self.catalog.vitest,
                    "@playwright/test": self.catalog.playwright,
                    "supertest": self.catalog.supertest,
                    "@types/supertest": self.catalog.types_supertest,
                },
            },
            "frontendEnvironment": {
                "status": "FRONTEND_ENVIRONMENT_READY",
                "styling": "tailwindcss-4",
                "cssEntry": "frontend/src/index.css",
                "versions": {
                    "tailwindcss": self.catalog.tailwindcss,
                    "@tailwindcss/vite": self.catalog.tailwindcss_vite,
                },
            },
            "deployment": {
                "workingDirectory": "backend",
                "startCommand": "npm run start",
                "serverEntry": "backend/dist/server.js",
                "frontendDist": "frontend/dist",
                "spaFallback": "frontend/dist/index.html",
                "e2eBaseUrl": f"http://127.0.0.1:{self.web_port}",
            },
            "workspaces": {
                "frontend": {"root": "frontend", "sourceRoot": "frontend/src"},
                "backend": {
                    "root": "backend",
                    "sourceRoot": "backend/src",
                    "moduleSystem": "NodeNext",
                },
                "shared": {
                    "root": "shared",
                    "sourceRoot": "shared/src",
                    "packageName": "@arc/shared",
                },
                "tests": {
                    "root": "tests",
                    "sourceRoot": "tests",
                    "packageName": "@arc/tests",
                },
            },
            "owners": {
                "frontend/src/main.tsx": "PROJECT_INITIALIZER",
                "frontend/src/index.css": "PROJECT_INITIALIZER",
                "frontend/src/App.tsx": "FRONTEND_SKELETON_COMPILER",
                "frontend/vite.config.ts": "FRONTEND_SKELETON_COMPILER",
                "shared/src/index.ts": "COMPILER",
                "backend/src": "SKELETON_COMPILER",
                "shared/src/contracts": "SKELETON_COMPILER",
                "tests/package.json": "PROJECT_INITIALIZER",
                "tests/tsconfig.json": "PROJECT_INITIALIZER",
                "tests/vitest.config.ts": "PROJECT_INITIALIZER",
                "tests/playwright.config.ts": "PROJECT_INITIALIZER",
                "tests/support": "PROJECT_INITIALIZER",
            },
            "allowedOutputRoots": {
                "skeleton": ["backend/src", "shared/src/contracts", "shared/src/index.ts"],
                "frontendSkeleton": [
                    "frontend/vite.config.ts",
                    "frontend/src/App.tsx",
                    "frontend/src/app",
                    "frontend/src/api",
                    "frontend/src/components",
                    "frontend/src/pages",
                    "frontend/src/runtime",
                ],
                "tests": [
                    "tests/unit",
                    "tests/integration",
                    "tests/e2e",
                ],
            },
        }
        self._validate_manifest_paths(project_manifest)
        self._project_artifact_owned = True
        write_json_atomic(project_manifest_path, project_manifest)
        return {
            "project_manifest": str(project_manifest_path),
        }

    def _validate_manifest_paths(self, manifest: dict[str, Any]) -> None:
        values: list[str] = [str(manifest["lockfile"])]
        deployment = manifest.get("deployment", {})
        values.extend(
            str(deployment[key])
            for key in ("workingDirectory", "serverEntry", "frontendDist", "spaFallback")
            if deployment.get(key)
        )
        for workspace in manifest["workspaces"].values():
            values.extend(value for key, value in workspace.items() if key.endswith("Root") or key == "root")
        for roots in manifest["allowedOutputRoots"].values():
            values.extend(roots)
        values.extend(manifest["owners"].keys())
        for relative in values:
            candidate = (self.output_root / relative).resolve()
            if candidate != self.output_root and self.output_root not in candidate.parents:
                raise ProjectInitializationError(
                    "PROJECT_MANIFEST_INVALID",
                    f"Manifest path escapes the output root: {relative}",
                )
        for roots in manifest["allowedOutputRoots"].values():
            for relative in roots:
                if not (self.output_root / relative).exists():
                    raise ProjectInitializationError(
                        "PROJECT_MANIFEST_INVALID",
                        f"Declared output root does not exist: {relative}",
                    )

    def _run(self, args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        executable = resolve_executable(args[0], self.environment)
        if executable is None:
            raise ProjectInitializationError(
                "PROJECT_SCAFFOLD_FAILED",
                f"Required command is unavailable: {args[0]}",
            )
        command = [executable, *args[1:]]
        command_label = " ".join(str(value) for value in args)
        self._log.info(f"COMMAND_START cwd={cwd} command={command_label}")
        command_environment = dict(self.environment)
        command_environment.setdefault("npm_config_yes", "true")
        command_environment.setdefault("npm_config_audit", "false")
        command_environment.setdefault("npm_config_fund", "false")
        command_environment.setdefault("npm_config_progress", "false")
        command_environment.setdefault("npm_config_update_notifier", "false")
        command_environment.setdefault("CI", "true")
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=command_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_kwargs(),
        )
        try:
            output = ""
            while True:
                elapsed = time.monotonic() - started
                remaining = self._command_timeout_seconds - elapsed
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        command,
                        self._command_timeout_seconds,
                        output=output,
                    )
                try:
                    output, _ = process.communicate(
                        timeout=min(self._COMMAND_HEARTBEAT_SECONDS, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    self._log.info(
                        "COMMAND_RUNNING "
                        f"cwd={cwd} command={command_label} "
                        f"elapsed_ms={round((time.monotonic() - started) * 1000)}"
                    )
            completed = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout=output or "",
                stderr="",
            )
        except FileNotFoundError as exc:
            raise ProjectInitializationError(
                "PROJECT_SCAFFOLD_FAILED",
                f"Required command is unavailable: {args[0]}",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            terminate_process_tree(process)
            output, _ = process.communicate()
            self._log.info(
                "COMMAND_TIMEOUT "
                f"cwd={cwd} command={command_label} duration_ms={elapsed_ms} "
                f"timeout_s={self._command_timeout_seconds:g}"
            )
            detail = (output or str(getattr(exc, "output", ""))).strip()
            if len(detail) > 2000:
                detail = detail[-2000:]
            raise ProjectInitializationError(
                "PROJECT_SCAFFOLD_FAILED",
                f"Command {list(args)!r} exceeded the "
                f"{self._command_timeout_seconds:g} second timeout"
                + (f": {detail}" if detail else "."),
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if len(detail) > 2000:
                detail = detail[-2000:]
            raise ProjectInitializationError(
                "PROJECT_SCAFFOLD_FAILED",
                f"Command {list(args)!r} exited with {completed.returncode}: {detail}",
            )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self._log.info(
            f"COMMAND_COMPLETED cwd={cwd} command={command_label} duration_ms={elapsed_ms}"
        )
        return completed

    def _root_package(self) -> dict[str, Any]:
        return {
            "name": "generated-application",
            "private": True,
            "workspaces": ["frontend", "backend", "shared", "tests"],
            "scripts": {
                "build": (
                    "npm run build -w @arc/shared && npm run build -w @arc/frontend "
                    "&& npm run build -w @arc/backend"
                ),
                "typecheck": (
                    "npm run build -w @arc/shared && npm run typecheck -w @arc/frontend "
                    "&& npm run typecheck -w @arc/backend "
                    "&& npm run typecheck -w @arc/tests"
                ),
                "test:typecheck": "npm run typecheck",
                "test:list": (
                    "npm run list:vitest -w @arc/tests && "
                    "npm run list:e2e -w @arc/tests"
                ),
                "test:unit": "npm run test:unit -w @arc/tests",
                "test:integration": "npm run test:integration -w @arc/tests",
                "test:e2e": "npm run test:e2e -w @arc/tests",
            },
            "engines": {"node": f">={self.catalog.node_major}"},
        }

    def _frontend_package(self) -> dict[str, Any]:
        return {
            "name": "@arc/frontend",
            "private": True,
            "version": "0.0.0",
            "type": "module",
            "scripts": {"dev": "vite", "build": "tsc -b && vite build", "typecheck": "tsc -b"},
            "dependencies": {
                "@arc/shared": "*",
                "react": self.catalog.react,
                "react-dom": self.catalog.react_dom,
                "react-router-dom": self.catalog.react_router_dom,
            },
            "devDependencies": {
                "@types/react": self.catalog.types_react,
                "@types/react-dom": self.catalog.types_react_dom,
                "@types/node": self.catalog.types_node,
                "@vitejs/plugin-react": self.catalog.vite_react_plugin,
                "@tailwindcss/vite": self.catalog.tailwindcss_vite,
                "tailwindcss": self.catalog.tailwindcss,
                "typescript": self.catalog.typescript,
                "vite": self.catalog.vite,
            },
        }

    def _backend_package(self) -> dict[str, Any]:
        return {
            "name": "@arc/backend",
            "private": True,
            "version": "0.0.0",
            "type": "module",
            "scripts": {
                "dev": "tsx watch src/server.ts",
                "build": "tsc -b tsconfig.json",
                "typecheck": (
                    "npm run build -w @arc/shared && tsc --noEmit -p tsconfig.json"
                ),
                "start": (
                    "npm run build -w @arc/shared && npm run build && node ./dist/server.js"
                ),
                "db:generate": "drizzle-kit generate",
                "db:migrate": "drizzle-kit migrate",
            },
            "dependencies": {
                "@arc/shared": "*",
                "better-sqlite3": self.catalog.better_sqlite3,
                "drizzle-orm": self.catalog.drizzle_orm,
                "express": self.catalog.express,
                "zod": self.catalog.zod,
            },
            "devDependencies": {
                "@types/better-sqlite3": self.catalog.types_better_sqlite3,
                "@types/express": self.catalog.types_express,
                "@types/node": self.catalog.types_node,
                "drizzle-kit": self.catalog.drizzle_kit,
                "tsx": self.catalog.tsx,
                "typescript": self.catalog.typescript,
            },
        }

    def _shared_package(self) -> dict[str, Any]:
        return {
            "name": "@arc/shared",
            "private": True,
            "version": "0.0.0",
            "type": "module",
            "exports": {".": {"types": "./dist/index.d.ts", "default": "./dist/index.js"}},
            "scripts": {
                "build": "tsc -b tsconfig.json",
                "typecheck": "tsc --noEmit -p tsconfig.json",
            },
            "dependencies": {"zod": self.catalog.zod},
            "devDependencies": {"typescript": self.catalog.typescript},
        }

    def _backend_tsconfig(self) -> dict[str, Any]:
        config = self._node_tsconfig()
        config["references"] = [{"path": "../shared"}]
        return config

    def _node_tsconfig(self) -> dict[str, Any]:
        return {
            "compilerOptions": {
                "target": "ES2022",
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "rootDir": "src",
                "outDir": "dist",
                "strict": True,
                "noUncheckedIndexedAccess": True,
                "exactOptionalPropertyTypes": True,
                "esModuleInterop": True,
                "resolveJsonModule": True,
                "skipLibCheck": True,
                "forceConsistentCasingInFileNames": True,
                "types": ["node"],
            },
            "include": ["src/**/*.ts"],
            "exclude": ["dist", "node_modules"],
        }

    def _shared_tsconfig(self) -> dict[str, Any]:
        config = self._node_tsconfig()
        config["compilerOptions"].update({"declaration": True, "composite": True})
        return config

    def _cleanup_staging(self) -> None:
        if self.staging_root.exists():
            shutil.rmtree(self.staging_root, ignore_errors=True)

    def _rollback_promoted_targets(self) -> None:
        for target in reversed(self._promoted_targets):
            self._remove_exact(target)
        self._promoted_targets.clear()
        if self._project_artifact_owned:
            self._remove_exact(self.project_artifact_root)
            self._project_artifact_owned = False

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectInitializationError(
                "PROJECT_NORMALIZATION_FAILED",
                f"Cannot read normalized JSON file {path}: {exc}",
            ) from exc
        if not isinstance(value, dict):
            raise ProjectInitializationError(
                "PROJECT_NORMALIZATION_FAILED",
                f"Normalized JSON file must contain an object: {path}",
            )
        return value

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    @staticmethod
    def _remove_exact(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    @staticmethod
    def _parse_version(value: str) -> tuple[int, int, int]:
        parts = value.split(".")
        if len(parts) < 2:
            raise ProjectInitializationError(
                "PROJECT_PROFILE_INVALID",
                f"Cannot parse toolchain version: {value!r}",
            )
        numbers: list[int] = []
        for part in parts[:3]:
            digits = "".join(character for character in part if character.isdigit())
            if not digits:
                raise ProjectInitializationError(
                    "PROJECT_PROFILE_INVALID",
                    f"Cannot parse toolchain version: {value!r}",
                )
            numbers.append(int(digits))
        while len(numbers) < 3:
            numbers.append(0)
        return numbers[0], numbers[1], numbers[2]
