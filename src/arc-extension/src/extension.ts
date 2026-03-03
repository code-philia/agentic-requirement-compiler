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

    // 0. Initialize Requirement Manager
    requirementManager = new RequirementManager();
    await requirementManager.initialize();

    // 1. 启动 Python 后端进程
    const backendDir = path.join(context.extensionPath, '..', 'arc-agent');
    const pythonScript = path.join(backendDir, 'main.py');
    
    // 2. 动态获取虚拟环境中的 python 解释器路径
    const isWindows = os.platform() === 'win32';
    const pythonExecutable = isWindows 
        ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
        : path.join(backendDir, '.venv', 'bin', 'python');

    // 3. 使用虚拟环境的 Python 启动后端进程
    // 注意这里把 'python' 替换成了 pythonExecutable
    if (vscode.workspace.getConfiguration('arc').get('enableBackend', true)) {
        backendProcess = cp.spawn(pythonExecutable, [pythonScript]);
        backendProcess.stdout?.on('data', (data) => console.log(`[ARC Backend]: ${data}`));
        backendProcess.stderr?.on('data', (data) => console.error(`[ARC Error]: ${data}`));
    }

    // 4. 注册左侧 Webview 视图 (Sidebar - Project Structure)
    const sidebarProvider = new WebviewProvider(context.extensionUri, 'sidebar', requirementManager);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.sidebarView",
            sidebarProvider
        )
    );

    // 5. 注册底部 Log Panel 视图
    const logPanelProvider = new WebviewProvider(context.extensionUri, 'logs');
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "arc.logPanelView",
            logPanelProvider
        )
    );

    // 6. 注册打开主编辑器命令
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.openMainEditor", (nodeId?: string) => {
            MainEditorPanel.createOrShow(context.extensionUri, requirementManager, nodeId);
        })
    );

    // 7. 注册开始编译命令
    context.subscriptions.push(
        vscode.commands.registerCommand("arc.startCompilation", async () => {
            await vscode.commands.executeCommand('arc.logPanelView.focus');
            setTimeout(() => {
                logPanelProvider.postMessage({ command: 'startCompilation' });
            }, 500);
        })
    );

    // 8. 注册清空并重启编译命令
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