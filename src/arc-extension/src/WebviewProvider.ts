import * as vscode from "vscode";
import { RequirementManager } from "./RequirementManager";

export class WebviewProvider implements vscode.WebviewViewProvider {
  _view?: vscode.WebviewView;

  constructor(
    private readonly _extensionUri: vscode.Uri,
    private readonly _viewType: 'launcher' | 'logs',
    private readonly _requirementManager?: RequirementManager
  ) {}

  public async resolveWebviewView(webviewView: vscode.WebviewView) {
    this._view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this._extensionUri],
    };

    webviewView.webview.html = await this._getHtmlForWebview(webviewView.webview);

    // Send workspace path context to the webview
    if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
        const workspacePath = vscode.workspace.workspaceFolders[0].uri.fsPath;
        setTimeout(() => {
            webviewView.webview.postMessage({ command: 'setContext', workspacePath });
        }, 600);
    }

    // Launcher view: clicking ARC icon should open the canvas.
    if (this._viewType === 'launcher') {
        const openMain = () => vscode.commands.executeCommand("arc.openMainEditor");
        // Open once when the launcher becomes visible.
        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) {
                openMain();
            }
        });
        // Also trigger immediately if already visible.
        if (webviewView.visible) {
            openMain();
        }
    }

    // Listen for messages from the webview
    webviewView.webview.onDidReceiveMessage(async (data) => {
        switch (data.command) {
            case "openMainEditor": {
                vscode.commands.executeCommand("arc.openMainEditor", data.nodeId);
                break;
            }
        }
    });
  }

  // Method to send messages to the webview
  public postMessage(message: any) {
      if (this._view) {
          this._view.webview.postMessage(message);
      }
  }

  private async _getHtmlForWebview(webview: vscode.Webview) {
    if (this._viewType === 'launcher') {
      return `
        <!DOCTYPE html>
        <html lang="en">
          <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0" />
            <style>
              body { font-family: var(--vscode-font-family); padding: 12px; color: var(--vscode-foreground); }
              .hint { font-size: 12px; opacity: 0.8; }
              button { margin-top: 10px; border: 1px solid var(--vscode-button-border); background: var(--vscode-button-background); color: var(--vscode-button-foreground); padding: 6px 10px; cursor: pointer; }
            </style>
          </head>
          <body>
            <div class="hint">ARC canvas launcher</div>
            <button onclick="acquireVsCodeApi().postMessage({ command: 'openMainEditor' })">Open Canvas</button>
          </body>
        </html>
      `;
    }

    const webviewDistPath = vscode.Uri.joinPath(this._extensionUri, 'webview-dist');
    const indexHtmlPath = vscode.Uri.joinPath(webviewDistPath, 'index.html');
    
    let htmlContent = '';
    try {
        const uint8Array = await vscode.workspace.fs.readFile(indexHtmlPath);
        htmlContent = new TextDecoder().decode(uint8Array);
    } catch (err) {
        return `<h3>Error loading webview</h3><p>${err}</p><p>Path: ${indexHtmlPath.toString()}</p>`;
    }

    const webviewUri = webview.asWebviewUri(webviewDistPath);
    
    // Replace relative paths with webview URIs
    htmlContent = htmlContent
        .replace(/src="\.\//g, `src="${webviewUri}/`)
        .replace(/href="\.\//g, `href="${webviewUri}/`);

    // Add CSP
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${webview.cspSource}; script-src 'unsafe-inline' ${webview.cspSource}; connect-src ws://127.0.0.1:8000 http://127.0.0.1:8000; img-src ${webview.cspSource} https: data:;">`;
    
    // Insert new CSP after <head>
    htmlContent = htmlContent.replace('<head>', `<head>\n${csp}`);
    
    // Inject initial view script
    const viewScript = `<script>
        if (window.history.replaceState) {
            window.history.replaceState(null, '', '?view=${this._viewType}');
        }
    </script>`;
    
    htmlContent = htmlContent.replace('<body>', `<body>\n${viewScript}`);

    return htmlContent;
  }
}
