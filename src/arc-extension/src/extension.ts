import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as path from 'path';
import * as os from 'os';
import { WebviewProvider } from './WebviewProvider';
import { MainEditorPanel } from './MainEditorPanel';
import { RequirementManager } from './RequirementManager';

let backendProcess: cp.ChildProcess | null = null;
let requirementManager: RequirementManager | null = null;

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

    // Register Sidebar View Provider
    const sidebarProvider = new WebviewProvider(context.extensionUri, 'sidebar', requirementManager);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.sidebarView",
            sidebarProvider
        )
    );

    // Register Logs View Provider
    const logPanelProvider = new WebviewProvider(context.extensionUri, 'logs');
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.logPanelView",
            logPanelProvider
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
}

export function deactivate() {
    if (backendProcess) {
        backendProcess.kill();
    }
}