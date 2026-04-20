import * as vscode from 'vscode';
import * as yaml from 'js-yaml';
import * as path from 'path';
import * as fs from 'fs';

export interface RequirementNode {
    id: string;
    name: string;
    description?: string;
    children?: RequirementNode[];
    scenarios?: any[];
    dependencies?: string[];
    [key: string]: any;
}

export class RequirementManager {
    private _workspaceRoot: string | undefined;
    private _requirementsFile: string | undefined;
    private _statusFile: string | undefined;
    private _data: RequirementNode | undefined;
    private _statusWatcher: vscode.FileSystemWatcher | undefined;
    private _requirementsWatcher: vscode.FileSystemWatcher | undefined;
    private _currentStatus: Record<string, string> = {};
    
    // Event emitter for status updates
    private _onDidUpdateStatus = new vscode.EventEmitter<Record<string, string>>();
    public readonly onDidUpdateStatus = this._onDidUpdateStatus.event;
    private _onDidUpdateData = new vscode.EventEmitter<RequirementNode>();
    public readonly onDidUpdateData = this._onDidUpdateData.event;

    constructor() {
        if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
            this._workspaceRoot = vscode.workspace.workspaceFolders[0].uri.fsPath;
            // Standard path structure: root/requirements/requirements.yaml
            this._requirementsFile = path.join(this._workspaceRoot, 'requirements', 'requirements.yaml');
            this._statusFile = path.join(this._workspaceRoot, '.arc', 'status.json');
        }
    }

    public async initialize() {
        if (!this._requirementsFile) {
            return;
        }

        try {
            await vscode.workspace.fs.stat(vscode.Uri.file(this._requirementsFile));
        } catch {
            // File does not exist, create it
            await this._createDefaultRequirements();
        }

        await this.loadRequirements();
        if (this._data) {
            this._onDidUpdateData.fire(this._data);
        }
        
        // Setup watcher for status.json
        if (this._statusFile) {
            const pattern = new vscode.RelativePattern(path.dirname(this._statusFile), 'status.json');
            this._statusWatcher = vscode.workspace.createFileSystemWatcher(pattern);
            
            this._statusWatcher.onDidChange(() => this._loadAndEmitStatus());
            this._statusWatcher.onDidCreate(() => this._loadAndEmitStatus());
            
            // Initial load
            this._loadAndEmitStatus();
        }

        // Setup watcher for requirements.yaml to keep editor and file always in sync
        if (this._requirementsFile) {
            const reqPattern = new vscode.RelativePattern(path.dirname(this._requirementsFile), path.basename(this._requirementsFile));
            this._requirementsWatcher = vscode.workspace.createFileSystemWatcher(reqPattern);

            const handleRequirementFileChange = async () => {
                await this.loadRequirements();
                if (this._data) {
                    this._onDidUpdateData.fire(this._data);
                }
            };

            this._requirementsWatcher.onDidChange(handleRequirementFileChange);
            this._requirementsWatcher.onDidCreate(handleRequirementFileChange);
            this._requirementsWatcher.onDidDelete(() => {
                this._data = undefined;
            });
        }
    }
    
    private async _loadAndEmitStatus() {
        if (!this._statusFile) return;
        try {
            const uri = vscode.Uri.file(this._statusFile);
            const uint8Array = await vscode.workspace.fs.readFile(uri);
            const content = new TextDecoder().decode(uint8Array);
            const status = JSON.parse(content);
            this._currentStatus = status;
            this._onDidUpdateStatus.fire(status);
        } catch (e) {
            // Ignore error (file might not exist yet)
        }
    }

    private async _createDefaultRequirements() {
        if (!this._requirementsFile) return;

        const defaultData: RequirementNode = {
            id: 'ROOT',
            name: 'System',
            description: 'A demo system.',
            children: [
                {
                    id: 'REQ-1',
                    name: 'New Requirement',
                    description: '',
                    children: [],
                    scenarios: [],
                    dependencies: []
                }
            ],
            dependencies: []
        };

        const yamlStr = yaml.dump(defaultData);
        const uint8Array = new TextEncoder().encode(yamlStr);
        
        // Ensure directory exists
        const dir = path.dirname(this._requirementsFile);
        try {
            await vscode.workspace.fs.createDirectory(vscode.Uri.file(dir));
        } catch (e) {
            // Ignore if exists
        }
        
        await vscode.workspace.fs.writeFile(vscode.Uri.file(this._requirementsFile), uint8Array);
    }

    public async loadRequirements() {
        if (!this._requirementsFile) return null;

        try {
            const fileUri = vscode.Uri.file(this._requirementsFile);
            const uint8Array = await vscode.workspace.fs.readFile(fileUri);
            const content = new TextDecoder().decode(uint8Array);
            this._data = yaml.load(content) as RequirementNode;
            return this._data;
        } catch (error) {
            console.error('Error loading requirements:', error);
            return null;
        }
    }

    public async saveRequirements() {
        if (!this._requirementsFile || !this._data) return;

        try {
            const yamlStr = yaml.dump(this._data);
            const uint8Array = new TextEncoder().encode(yamlStr);
            await vscode.workspace.fs.writeFile(vscode.Uri.file(this._requirementsFile), uint8Array);
            this._onDidUpdateData.fire(this._data);
        } catch (error) {
            console.error('Error saving requirements:', error);
        }
    }

    public getData() {
        return this._data;
    }

    public getCurrentStatus() {
        return this._currentStatus;
    }

    public async addNode(targetId: string, type: 'child' | 'sibling') {
        if (!this._data) return null;

        const newNode: RequirementNode = {
            id: `REQ-${Date.now()}`, // Temporary ID generation
            name: 'New Requirement',
            description: '',
            children: [],
            scenarios: [],
            dependencies: []
        };

        const findAndInsert = (node: RequirementNode): boolean => {
            if (!node.children) node.children = [];

            // Case 1: Add as child of current node
            if (node.id === targetId && type === 'child') {
                node.children.push(newNode);
                return true;
            }

            // Case 2: Add as sibling (check children for target)
            const index = node.children.findIndex(child => child.id === targetId);
            if (index !== -1 && type === 'sibling') {
                node.children.splice(index + 1, 0, newNode);
                return true;
            }

            // Recurse
            for (const child of node.children) {
                if (findAndInsert(child)) return true;
            }
            return false;
        };

        if (this._data.id === targetId && type === 'child') {
             if (!this._data.children) this._data.children = [];
             this._data.children.push(newNode);
        } else {
            findAndInsert(this._data);
        }

        await this.saveRequirements();
        return this._data;
    }
    
    public async updateNode(nodeId: string, updates: any) {
        if (!this._data) return null;
        
        const findAndUpdate = (node: RequirementNode): boolean => {
            if (node.id === nodeId) {
                Object.assign(node, updates);
                return true;
            }
            if (node.children) {
                for (const child of node.children) {
                    if (findAndUpdate(child)) return true;
                }
            }
            return false;
        };
        
        if (findAndUpdate(this._data)) {
            await this.saveRequirements();
        }
        return this._data;
    }

    public async deleteNode(nodeId: string) {
        if (!this._data) return null;

        const findAndDelete = (node: RequirementNode): boolean => {
            if (!node.children) return false;
            
            const index = node.children.findIndex(child => child.id === nodeId);
            if (index !== -1) {
                node.children.splice(index, 1);
                return true;
            }
            
            for (const child of node.children) {
                if (findAndDelete(child)) return true;
            }
            return false;
        };

        findAndDelete(this._data);
        await this.saveRequirements();
        return this._data;
    }
}
