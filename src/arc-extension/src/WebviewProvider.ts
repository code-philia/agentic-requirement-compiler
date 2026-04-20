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
            case "startCompilation": {
                vscode.commands.executeCommand("arc.startCompilation");
                break;
            }
            case "restartCompilation": {
                vscode.commands.executeCommand("arc.restartCompilation");
                break;
            }
            case "requestArcSettingsInit": {
                const settings = await vscode.commands.executeCommand("arc.getSettingsData");
                webviewView.webview.postMessage({ command: 'arcSettingsInit', data: settings });
                break;
            }
            case "saveArcSettings": {
                const result: any = await vscode.commands.executeCommand("arc.saveSettings", data.payload);
                if (result?.ok) {
                    vscode.window.showInformationMessage("ARC settings saved.");
                    webviewView.webview.postMessage({ command: 'arcSettingsSaved', ok: true });
                } else {
                    vscode.window.showErrorMessage(`Failed to save ARC settings: ${result?.message || 'Unknown error'}`);
                    webviewView.webview.postMessage({ command: 'arcSettingsSaved', ok: false, message: result?.message || 'Unknown error' });
                }
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
              :root { color-scheme: light dark; }
              body { font-family: var(--vscode-font-family); padding: 10px; color: var(--vscode-foreground); background: var(--vscode-sideBar-background); }
              .section { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 10px; margin-bottom: 10px; background: color-mix(in srgb, var(--vscode-sideBar-background) 88%, var(--vscode-editor-background)); }
              .title { font-size: 11px; text-transform: uppercase; letter-spacing: .4px; opacity: 0.82; margin-bottom: 8px; font-weight: 600; }
              .row { margin-bottom: 9px; }
              .label { font-size: 11px; opacity: 0.92; display: block; margin-bottom: 4px; }
              input, select {
                width: 100%;
                box-sizing: border-box;
                padding: 6px 8px;
                border: 1px solid var(--vscode-input-border);
                border-radius: 4px;
                background: var(--vscode-input-background);
                color: var(--vscode-input-foreground);
                outline: none;
              }
              input:focus, select:focus {
                border-color: var(--vscode-focusBorder);
                box-shadow: 0 0 0 1px color-mix(in srgb, var(--vscode-focusBorder) 65%, transparent);
              }
              input[disabled] { opacity: 0.72; }
              button {
                border: 1px solid var(--vscode-button-border);
                border-radius: 4px;
                background: var(--vscode-button-background);
                color: var(--vscode-button-foreground);
                padding: 6px 10px;
                cursor: pointer;
              }
              button:hover { filter: brightness(1.06); }
              .btn-row { display: flex; flex-direction: column; gap: 6px; }
              .status { font-size: 11px; opacity: 0.82; margin-top: 6px; min-height: 16px; }
            </style>
          </head>
          <body>
            <div class="section">
              <div class="title">Actions</div>
              <div class="btn-row">
                <button id="openCanvasBtn">Open Canvas</button>
                <button id="startBtn">Start Compilation</button>
                <button id="restartBtn">Clear and Restart Compilation</button>
              </div>
            </div>

            <div class="section">
              <div class="title">ARC Settings</div>
              <div id="envFields"></div>
              <div class="row">
                <label class="label" for="backend">Backend</label>
                <select id="backend">
                  <option value="nodejs">Node.js</option>
                  <option value="python_flask">Python Flask</option>
                </select>
              </div>
              <div class="row">
                <label class="label">Frontend</label>
                <input value="react" disabled />
              </div>
              <div class="row">
                <label class="label">Database</label>
                <input value="sqlite" disabled />
              </div>
              <button id="saveBtn">Save Settings</button>
              <div class="status" id="status"></div>
            </div>

            <script>
              const vscode = acquireVsCodeApi();
              const state = { envKeys: [], envValues: {}, stack: { backend: 'nodejs', frontend: 'react', database: 'sqlite' } };
              const envFields = document.getElementById('envFields');
              const backend = document.getElementById('backend');
              const status = document.getElementById('status');

              function renderEnv() {
                envFields.innerHTML = '';
                state.envKeys.forEach((k) => {
                  const row = document.createElement('div');
                  row.className = 'row';
                  const label = document.createElement('label');
                  label.className = 'label';
                  label.textContent = k;
                  const input = document.createElement('input');
                  input.value = state.envValues[k] || '';
                  input.addEventListener('input', (e) => { state.envValues[k] = e.target.value; });
                  row.appendChild(label);
                  row.appendChild(input);
                  envFields.appendChild(row);
                });
              }

              document.getElementById('openCanvasBtn').addEventListener('click', () => vscode.postMessage({ command: 'openMainEditor' }));
              document.getElementById('startBtn').addEventListener('click', () => vscode.postMessage({ command: 'startCompilation' }));
              document.getElementById('restartBtn').addEventListener('click', () => vscode.postMessage({ command: 'restartCompilation' }));
              document.getElementById('saveBtn').addEventListener('click', () => {
                status.textContent = 'Saving...';
                vscode.postMessage({
                  command: 'saveArcSettings',
                  payload: {
                    envValues: state.envValues,
                    stack: { backend: backend.value, frontend: 'react', database: 'sqlite' }
                  }
                });
              });

              window.addEventListener('message', (event) => {
                const message = event.data;
                if (message.command === 'arcSettingsInit') {
                  const d = message.data || {};
                  state.envKeys = d.envKeys || [];
                  state.envValues = d.envValues || {};
                  state.stack = d.stack || state.stack;
                  backend.value = state.stack.backend || 'nodejs';
                  renderEnv();
                  status.textContent = '';
                }
                if (message.command === 'arcSettingsSaved') {
                  status.textContent = message.ok ? 'Saved.' : ('Save failed: ' + (message.message || 'Unknown error'));
                }
              });

              vscode.postMessage({ command: 'requestArcSettingsInit' });
            </script>
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
