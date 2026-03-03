import * as vscode from "vscode";

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
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "webview-dist")],
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
        }
    });
    
    // Listen for status updates from manager
    if (manager.onDidUpdateStatus) {
        manager.onDidUpdateStatus((status: Record<string, string>) => {
            panel.webview.postMessage({ command: 'updateStatus', status });
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
    const webviewDistPath = vscode.Uri.joinPath(this._extensionUri, 'webview-dist');
    const indexHtmlPath = vscode.Uri.joinPath(webviewDistPath, 'index.html');
    
    let htmlContent = '';
    try {
        const uint8Array = await vscode.workspace.fs.readFile(indexHtmlPath);
        htmlContent = new TextDecoder().decode(uint8Array);
    } catch (err) {
        webview.html = `<h3>Error loading webview</h3><p>${err}</p><p>Path: ${indexHtmlPath.toString()}</p>`;
        return;
    }

    const webviewUri = webview.asWebviewUri(webviewDistPath);
    
    // Replace relative paths with webview URIs
    htmlContent = htmlContent
        .replace(/src="\.\//g, `src="${webviewUri}/`)
        .replace(/href="\.\//g, `href="${webviewUri}/`);

    // Add CSP
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${webview.cspSource}; script-src 'unsafe-inline' ${webview.cspSource}; connect-src ws://127.0.0.1:8000 http://127.0.0.1:8000; img-src ${webview.cspSource} https: data:;">`;
    
    htmlContent = htmlContent.replace('<head>', `<head>\n${csp}`);
    
    // Inject initial view script for 'main'
    const viewScript = `<script>
        if (window.history.replaceState) {
            window.history.replaceState(null, '', '?view=main');
        }
    </script>`;
    
    htmlContent = htmlContent.replace('<body>', `<body>\n${viewScript}`);

    webview.html = htmlContent;
  }
}
