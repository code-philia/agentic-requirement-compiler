import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import * as os from 'os';
import { WebviewProvider } from './WebviewProvider';
import { MainEditorPanel } from './MainEditorPanel';
import { RequirementManager } from './RequirementManager';

let backendProcess: cp.ChildProcess | null = null;
let requirementManager: RequirementManager | null = null;
const ARC_STACK_START = '<!-- ARC_TECH_STACK_START -->';
const ARC_STACK_END = '<!-- ARC_TECH_STACK_END -->';

type ArcTechStack = {
    backend: 'nodejs' | 'python_flask';
    frontend: 'react';
    database: 'sqlite';
};

type ArcStackProfile = {
    frontend: {
        framework: string;
        language: string;
        styling: string;
        http: string;
        testing: string;
    };
    backend: {
        runtime: string;
        framework: string;
        database: string;
        testing: string[];
    };
};

type SettingsInitData = {
    envKeys: string[];
    envValues: Record<string, string>;
    stack: ArcTechStack;
};

export async function activate(context: vscode.ExtensionContext) {
    console.log('ARC Extension is active!');

    // Initialize Requirement Manager
    requirementManager = new RequirementManager();
    await requirementManager.initialize();

    // Start Python Backend Process
    const backendDir = path.join(context.extensionPath, '..', 'arc-agent');
    const pythonScript = path.join(backendDir, 'main.py');
    
    const isWindows = os.platform() === 'win32';
    const pythonExecutable = isWindows 
        ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
        : path.join(backendDir, '.venv', 'bin', 'python');

    // Enable Backend Process
    if (vscode.workspace.getConfiguration('arc').get('enableBackend', true)) {
        backendProcess = cp.spawn(pythonExecutable, [pythonScript]);
        backendProcess.stdout?.on('data', (data) => console.log(`[ARC Backend]: ${data}`));
        backendProcess.stderr?.on('data', (data) => console.error(`[ARC Error]: ${data}`));
    }

    // Register ARC launcher view provider (no requirement tree panel)
    const launcherProvider = new WebviewProvider(context.extensionUri, 'launcher', requirementManager);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.sidebarView",
            launcherProvider
        )
    );

    // Register Logs View Provider
    const logPanelProvider = new WebviewProvider(context.extensionUri, 'logs');
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.logPanelView",
            logPanelProvider,
            {
                webviewOptions: {
                    retainContextWhenHidden: true // Keep webview alive when hidden
                }
            }
        )
    );

    // Register Main Editor Panel Command
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.openMainEditor", (nodeId?: string) => {
            MainEditorPanel.createOrShow(context.extensionUri, requirementManager, nodeId);
        })
    );

    // Register Start Compilation Command
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.startCompilation", async () => {
            await vscode.commands.executeCommand('arc.logPanelView.focus');
            setTimeout(() => {
                logPanelProvider.postMessage({ command: 'startCompilation' });
            }, 500);
        })
    );

    // Register Restart Compilation Command
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.restartCompilation", async () => {
            await vscode.commands.executeCommand('arc.logPanelView.focus');
            setTimeout(() => {
                logPanelProvider.postMessage({ command: 'restartCompilation' });
            }, 500);
        })
    );

    // Register command for launcher panel to fetch settings data
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.getSettingsData", async () => {
            const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            if (!workspaceRoot) {
                return { envKeys: [], envValues: {}, stack: { backend: 'nodejs', frontend: 'react', database: 'sqlite' } };
            }
            return await loadSettingsInitData(context, workspaceRoot);
        })
    );

    // Register command for launcher panel to save settings
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.saveSettings", async (payload: {
            envValues: Record<string, string>;
            stack: ArcTechStack;
            profile?: ArcStackProfile;
        }) => {
            const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            if (!workspaceRoot) {
                return { ok: false, message: "ARC Settings requires an opened workspace." };
            }
            if (!payload || !payload.envValues || !payload.stack) {
                return { ok: false, message: "Invalid ARC settings payload." };
            }
            try {
                const initData = await loadSettingsInitData(context, workspaceRoot);
                await saveArcSettings(context, workspaceRoot, initData.envKeys, payload);
                return { ok: true };
            } catch (error) {
                const msg = error instanceof Error ? error.message : String(error);
                return { ok: false, message: msg };
            }
        })
    );

}

export function deactivate() {
    if (backendProcess) {
        backendProcess.kill();
    }
}

async function loadSettingsInitData(
    context: vscode.ExtensionContext,
    workspaceRoot: string,
): Promise<SettingsInitData> {
    const arcAgentDir = path.join(context.extensionPath, '..', 'arc-agent');
    const envExamplePath = path.join(arcAgentDir, '.env_example');
    const envPath = path.join(arcAgentDir, '.env');
    const metadataPath = path.join(workspaceRoot, '.arc', 'metadata.md');

    const envExampleContent = await readTextFile(vscode.Uri.file(envExamplePath));
    const envContent = await readTextFile(vscode.Uri.file(envPath));
    const metadataContent = await readTextFile(vscode.Uri.file(metadataPath));

    const envKeys = parseEnvKeys(envExampleContent);
    const envValues = parseEnvValues(envContent);
    const stack = parseStackFromMetadata(metadataContent);

    return { envKeys, envValues, stack };
}

async function saveArcSettings(
    context: vscode.ExtensionContext,
    workspaceRoot: string,
    envKeys: string[],
    payload: { envValues: Record<string, string>; stack: ArcTechStack; profile?: ArcStackProfile },
): Promise<void> {
    const arcAgentDir = path.join(context.extensionPath, '..', 'arc-agent');
    const envPath = path.join(arcAgentDir, '.env');
    const metadataPath = path.join(workspaceRoot, '.arc', 'metadata.md');

    const existingEnvContent = await readTextFile(vscode.Uri.file(envPath));
    const existingEnvValues = parseEnvValues(existingEnvContent);

    const mergedValues: Record<string, string> = { ...existingEnvValues };
    for (const key of envKeys) {
        mergedValues[key] = payload.envValues[key] ?? '';
    }

    const lines = envKeys.map((key) => `${key}=${mergedValues[key] ?? ''}`);
    const knownKeySet = new Set(envKeys);
    for (const [key, value] of Object.entries(existingEnvValues)) {
        if (!knownKeySet.has(key)) {
            lines.push(`${key}=${value}`);
        }
    }
    await writeTextFile(vscode.Uri.file(envPath), `${lines.join('\n')}\n`);

    const existingMetadata = await readTextFile(vscode.Uri.file(metadataPath));
    const profile = payload.profile ?? deriveStackProfile(payload.stack);
    const updatedMetadata = upsertStackMetadata(existingMetadata, payload.stack, profile);
    await writeTextFile(vscode.Uri.file(metadataPath), updatedMetadata);
}

function parseEnvKeys(content: string): string[] {
    const keys: string[] = [];
    for (const raw of content.split(/\r?\n/)) {
        const line = raw.trim();
        if (!line || line.startsWith('#')) {
            continue;
        }
        const idx = line.indexOf('=');
        if (idx <= 0) {
            continue;
        }
        keys.push(line.slice(0, idx).trim());
    }
    return keys;
}

function parseEnvValues(content: string): Record<string, string> {
    const result: Record<string, string> = {};
    for (const raw of content.split(/\r?\n/)) {
        const line = raw.trim();
        if (!line || line.startsWith('#')) {
            continue;
        }
        const idx = line.indexOf('=');
        if (idx <= 0) {
            continue;
        }
        const key = line.slice(0, idx).trim();
        const value = line.slice(idx + 1);
        result[key] = value;
    }
    return result;
}

function parseStackFromMetadata(content: string): ArcTechStack {
    const defaults: ArcTechStack = {
        backend: 'nodejs',
        frontend: 'react',
        database: 'sqlite',
    };
    if (!content) {
        return defaults;
    }

    const normalized = content.toLowerCase();
    const backendMatch = normalized.match(/-\s*backend:\s*(nodejs|python_flask)/);
    const frontendMatch = normalized.match(/-\s*frontend:\s*(react)/);
    const databaseMatch = normalized.match(/-\s*database:\s*(sqlite)/);

    return {
        backend: (backendMatch?.[1] as ArcTechStack['backend']) ?? defaults.backend,
        frontend: (frontendMatch?.[1] as ArcTechStack['frontend']) ?? defaults.frontend,
        database: (databaseMatch?.[1] as ArcTechStack['database']) ?? defaults.database,
    };
}

function deriveStackProfile(stack: ArcTechStack): ArcStackProfile {
    // Current release only supports React + Node.js + SQLite stack profile.
    if (stack.backend !== 'nodejs' || stack.frontend !== 'react' || stack.database !== 'sqlite') {
        // Fallback to the same profile to keep metadata stable.
    }
    return {
        frontend: {
            framework: 'React 18+ (Vite)',
            language: 'JavaScript (ES6+)',
            styling: 'Tailwind CSS v4',
            http: 'Axios (Must use Interceptors for global error handling)',
            testing: 'None in frontend directory. (Verified via E2E in backend).',
        },
        backend: {
            runtime: 'Node.js (LTS)',
            framework: 'Express.js',
            database: 'SQLite3 (`sqlite3` driver, file-based)',
            testing: [
                'Vitest: Used for Unit and Integration testing.',
                'Supertest: Used with Vitest for API route testing.',
                'Playwright: Used for End-to-End (E2E) testing, located in `backend/test-e2e`.',
            ],
        },
    };
}

function upsertStackMetadata(content: string, stack: ArcTechStack, profile: ArcStackProfile): string {
    const block = [
        ARC_STACK_START,
        '## ARC Metadata',
        '',
        '### Main Stack',
        `- backend: ${stack.backend}`,
        `- frontend: ${stack.frontend}`,
        `- database: ${stack.database}`,
        '',
        '### Frontend',
        `* **Framework**: ${profile.frontend.framework}`,
        `* **Language**: ${profile.frontend.language}`,
        `* **Styling**: ${profile.frontend.styling}`,
        `* **HTTP**: ${profile.frontend.http}`,
        `* **Testing**: ${profile.frontend.testing}`,
        '',
        '### Backend',
        `* **Runtime**: ${profile.backend.runtime}`,
        `* **Framework**: ${profile.backend.framework}`,
        `* **Database**: ${profile.backend.database}`,
        '* **Testing**:',
        ...profile.backend.testing.map(item => `  * ${item}`),
        ARC_STACK_END,
    ].join('\n');

    if (!content.trim()) {
        return `${block}\n`;
    }

    const start = content.indexOf(ARC_STACK_START);
    const end = content.indexOf(ARC_STACK_END);
    if (start !== -1 && end !== -1 && end > start) {
        const before = content.slice(0, start).trimEnd();
        const after = content.slice(end + ARC_STACK_END.length).trimStart();
        if (before && after) {
            return `${before}\n\n${block}\n\n${after}\n`;
        }
        if (before) {
            return `${before}\n\n${block}\n`;
        }
        if (after) {
            return `${block}\n\n${after}\n`;
        }
        return `${block}\n`;
    }

    return `${content.trimEnd()}\n\n${block}\n`;
}

async function readTextFile(uri: vscode.Uri): Promise<string> {
    try {
        const bytes = await vscode.workspace.fs.readFile(uri);
        return new TextDecoder().decode(bytes);
    } catch {
        return '';
    }
}

async function writeTextFile(uri: vscode.Uri, content: string): Promise<void> {
    const dir = path.dirname(uri.fsPath);
    await vscode.workspace.fs.createDirectory(vscode.Uri.file(dir));
    await vscode.workspace.fs.writeFile(uri, new TextEncoder().encode(content));
}
