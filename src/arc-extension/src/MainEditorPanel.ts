import * as vscode from "vscode";
import * as cp from "child_process";
import * as path from "path";
import * as fs from "fs";

export class MainEditorPanel {
  public static currentPanel: MainEditorPanel | undefined;
  private readonly _panel: vscode.WebviewPanel;
  private readonly _extensionUri: vscode.Uri;
  private _disposables: vscode.Disposable[] = [];

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri) {
    this._panel = panel;
    this._extensionUri = extensionUri;

    this._panel.onDidDispose(() => this.dispose(), null, this._disposables);
    this._loadHtml();
  }

  public static createOrShow(extensionUri: vscode.Uri, manager: any, nodeId?: string) {
    const column = vscode.window.activeTextEditor
      ? vscode.window.activeTextEditor.viewColumn
      : undefined;

    // If we already have a panel, show it.
    if (MainEditorPanel.currentPanel) {
      MainEditorPanel.currentPanel._panel.reveal(column);
      MainEditorPanel.currentPanel.updateProjectAndSelection(manager, nodeId);
      return;
    }

    // Otherwise, create a new panel.
    const panel = vscode.window.createWebviewPanel(
      "arc.mainEditor",
      "Requirement Editor",
      column || vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "web-dist")],
        retainContextWhenHidden: true, // Keep state when switching tabs
      }
    );

    MainEditorPanel.currentPanel = new MainEditorPanel(panel, extensionUri);
    
    // Wait for webview to load then send data
    setTimeout(() => MainEditorPanel.currentPanel?.updateProjectAndSelection(manager, nodeId), 1000);
    
    // Listen for messages from the webview
    panel.webview.onDidReceiveMessage(async (data) => {
        switch (data.command) {
            case "requestProject": {
                const projectData = manager.getData();
                if (projectData) {
                    panel.webview.postMessage({ command: 'updateProject', data: projectData });
                }
                break;
            }
            case "updateNode": {
                await manager.updateNode(data.nodeId, data.updates);
                // Ideally, send back the updated project or confirm success
                const newData = manager.getData();
                panel.webview.postMessage({ command: 'updateProject', data: newData });
                break;
            }
            case "deleteNode": {
                await manager.deleteNode(data.nodeId);
                const newData = manager.getData();
                panel.webview.postMessage({ command: 'updateProject', data: newData });
                break;
            }
            case "addNode": {
                await manager.addNode(data.targetId, data.type);
                const newData = manager.getData();
                panel.webview.postMessage({ command: 'updateProject', data: newData });
                break;
            }
            case "openFile": {
                const workspaceRoot = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
                if (!workspaceRoot || !data.filePath) return;
                
                const fullPath = path.join(workspaceRoot, data.filePath);
                try {
                    const doc = await vscode.workspace.openTextDocument(fullPath);
                    // Open in a new column (beside) to keep the webview visible
                    const editor = await vscode.window.showTextDocument(doc, {
                        viewColumn: vscode.ViewColumn.Beside,
                        preserveFocus: false
                    });
                    
                    let targetLine = -1;

                    // If a line string (first_line content) is provided, try to find it in the document
                    if (data.line && typeof data.line === 'string') {
                        const searchStr = data.line.trim();
                        for (let i = 0; i < doc.lineCount; i++) {
                            if (doc.lineAt(i).text.includes(searchStr)) {
                                targetLine = i;
                                break;
                            }
                        }
                    }

                    if (targetLine >= 0) {
                        const range = new vscode.Range(targetLine, 0, targetLine, 0);
                        editor.selection = new vscode.Selection(targetLine, 0, targetLine, 0);
                        editor.revealRange(range, vscode.TextEditorRevealType.InCenter);

                        // Briefly highlight the line with a pale yellow color
                        const highlightDecoration = vscode.window.createTextEditorDecorationType({
                            backgroundColor: 'rgba(255, 255, 0, 0.3)',
                            isWholeLine: true
                        });
                        editor.setDecorations(highlightDecoration, [range]);

                        // Remove the highlight after 2 seconds
                        setTimeout(() => {
                            highlightDecoration.dispose();
                        }, 2000);
                    }
                } catch (e) {
                    vscode.window.showErrorMessage(`Could not open file: ${fullPath}`);
                }
                break;
            }
            case "openRequirementById": {
                const workspaceRoot = vscode.workspace.workspaceFolders?.[0].uri.fsPath;
                const reqId = String(data.reqId || '').trim();
                if (!workspaceRoot || !reqId) return;

                const candidates = [
                    path.join(workspaceRoot, 'requirements', 'requirements.yaml'),
                    path.join(workspaceRoot, 'requirements', 'requirents.yaml'),
                ];
                const requirementFile = candidates.find((p) => {
                    try {
                        return fs.existsSync(p);
                    } catch {
                        return false;
                    }
                });
                if (!requirementFile) {
                    vscode.window.showErrorMessage('Could not find requirements YAML file.');
                    return;
                }

                try {
                    const doc = await vscode.workspace.openTextDocument(requirementFile);
                    const editor = await vscode.window.showTextDocument(doc, {
                        viewColumn: vscode.ViewColumn.Beside,
                        preserveFocus: false
                    });

                    let targetLine = -1;
                    const needle = `id: ${reqId}`;
                    for (let i = 0; i < doc.lineCount; i++) {
                        const text = doc.lineAt(i).text;
                        if (text.includes(needle) || text.trim() === `- ${needle}`) {
                            targetLine = i;
                            break;
                        }
                    }
                    if (targetLine < 0) {
                        vscode.window.showWarningMessage(`REQ ID ${reqId} not found in requirements YAML.`);
                        return;
                    }

                    const range = new vscode.Range(targetLine, 0, targetLine, 0);
                    editor.selection = new vscode.Selection(targetLine, 0, targetLine, 0);
                    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);

                    const highlightDecoration = vscode.window.createTextEditorDecorationType({
                        backgroundColor: 'rgba(255, 255, 0, 0.3)',
                        isWholeLine: true
                    });
                    editor.setDecorations(highlightDecoration, [range]);
                    setTimeout(() => highlightDecoration.dispose(), 2200);
                } catch (e) {
                    vscode.window.showErrorMessage('Failed to open requirements file.');
                }
                break;
            }
        }
    });
    
    // Listen for status updates from manager
    if (manager.onDidUpdateStatus) {
        manager.onDidUpdateStatus((status: Record<string, string>) => {
            panel.webview.postMessage({ command: 'updateStatus', status });
        });
    }
    if (manager.onDidUpdateData) {
        manager.onDidUpdateData((data: any) => {
            panel.webview.postMessage({ command: 'updateProject', data });
        });
    }
  }

  public updateNode(nodeId: string) {
     // Deprecated or unused
  }

  public async updateProjectAndSelection(manager: any, selectedNodeId?: string) {
      const data = manager.getData();
      if (data) {
          await this._panel.webview.postMessage({ command: 'updateProject', data });
      }
      if (manager.getCurrentStatus) {
          const status = manager.getCurrentStatus();
          await this._panel.webview.postMessage({ command: 'updateStatus', status });
      }
      
      if (selectedNodeId && data) {
          // Find the specific node data to send for the property panel
          const findNode = (node: any): any => {
              if (node.id === selectedNodeId) return node;
              if (node.children) {
                  for (const child of node.children) {
                      const found = findNode(child);
                      if (found) return found;
                  }
              }
              return null;
          };
          const nodeData = findNode(data);
          if (nodeData) {
               await this._panel.webview.postMessage({ command: 'setNode', node: nodeData });
          }
      }
  }

  public dispose() {
    MainEditorPanel.currentPanel = undefined;
    this._panel.dispose();
    while (this._disposables.length) {
      const x = this._disposables.pop();
      if (x) {
        x.dispose();
      }
    }
  }

  private async _loadHtml() {
    const webview = this._panel.webview;
    const webDistPath = vscode.Uri.joinPath(this._extensionUri, 'web-dist');
    const indexHtmlPath = vscode.Uri.joinPath(webDistPath, 'index.html');
    
    let htmlContent = '';
    try {
        const uint8Array = await vscode.workspace.fs.readFile(indexHtmlPath);
        htmlContent = new TextDecoder().decode(uint8Array);
    } catch (err) {
        webview.html = `<h3>Error loading webview</h3><p>${err}</p><p>Path: ${indexHtmlPath.toString()}</p>`;
        return;
    }

    const webviewUri = webview.asWebviewUri(webDistPath);
    
    // Replace relative paths with webview URIs
    htmlContent = htmlContent
        .replace(/src="\.\//g, `src="${webviewUri}/`)
        .replace(/href="\.\//g, `href="${webviewUri}/`);

    // Add CSP
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${webview.cspSource}; script-src 'unsafe-inline' ${webview.cspSource}; connect-src ws://127.0.0.1:8000 http://127.0.0.1:8000; img-src ${webview.cspSource} https: data:;">`;
    
    htmlContent = htmlContent.replace('<head>', `<head>\n${csp}`);
    
    // Inject workspace path and view script
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0].uri.fsPath || '';
    // Replace backslashes with forward slashes to avoid JSON parse issues in inline script
    const safeWorkspaceRoot = workspaceRoot.replace(/\\/g, '\\\\');
    
    const viewScript = `<script>
        window.arcWorkspaceRoot = "${safeWorkspaceRoot}";
        if (window.history.replaceState) {
            window.history.replaceState(null, '', '?view=main');
        }
    </script>`;
    
    htmlContent = htmlContent.replace('<body>', `<body>\n${viewScript}`);

    webview.html = htmlContent;
  }
}
