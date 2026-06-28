import { useState, useEffect } from 'react';
import { X, Plus, Trash2, ChevronDown, ChevronRight, FileText, Code, CheckCircle, Loader2, Search, RotateCcw } from 'lucide-react';
import { cn } from '../lib/utils';

interface Step {
    keyword: string;
    content: string;
}

interface RequirementNode {
    id: string;
    name: string;
    description?: string;
    dependencies?: string[];
    scenario?: Step[]; // Changed from scenarios: Scenario[] to a single scenario array of Steps
    [key: string]: any;
}

interface PropertiesPanelProps {
    node: RequirementNode | null;
    onUpdate: (id: string, updates: any) => void;
    onDelete: (id: string) => void;
    onClose?: () => void;
    onClearSelection?: () => void;
}

interface InterfaceData {
    interface_id: string;
    req_ids?: string[];
    type: string;
    file_path: string;
    first_line: string;
    implemented: boolean;
    callers: string[];
    callees: string[];
}

interface TestData {
    test_id: string;
    req_id?: string;
    type: string;
    file_path: string;
    first_line: string;
    interface_ids: string[];
}

interface RequirementRow {
    req_id: string;
    description: string;
    scenario?: any[];
    dependencies?: string[];
}

const CollapsibleSection = ({ title, children, defaultOpen = true, onAdd }: { title: string, children: React.ReactNode, defaultOpen?: boolean, onAdd?: () => void }) => {
    const [isOpen, setIsOpen] = useState(defaultOpen);

    return (
        <div className="border-b border-[var(--vscode-panel-border)] last:border-0">
            <div className="flex items-center justify-between px-4 py-2 hover:bg-[var(--vscode-list-hoverBackground)] cursor-pointer select-none group" onClick={() => setIsOpen(!isOpen)}>
                <div className="flex items-center gap-1 font-bold text-xs uppercase text-[var(--vscode-sideBarTitle-foreground)]">
                    {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    {title}
                </div>
                {onAdd && (
                    <button 
                        onClick={(e) => { e.stopPropagation(); onAdd(); }}
                        className="opacity-0 group-hover:opacity-100 p-1 hover:bg-[var(--vscode-toolbar-hoverBackground)] rounded text-[var(--vscode-icon-foreground)] transition-opacity"
                        title="Add Item"
                    >
                        <Plus size={14} />
                    </button>
                )}
            </div>
            {isOpen && (
                <div className="px-4 pb-3">
                    {children}
                </div>
            )}
        </div>
    );
};

const TraceabilityTab = ({ selectedNodeId, onClearReqFilter }: { selectedNodeId?: string, onClearReqFilter?: () => void }) => {
    const [data, setData] = useState<{ requirements: RequirementRow[], interfaces: InterfaceData[], tests: TestData[] } | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [expandedItems, setExpandedItems] = useState<Record<string, boolean>>({});
    const [entityTab, setEntityTab] = useState<'REQ' | 'IF' | 'TEST'>('REQ');
    const [searchText, setSearchText] = useState('');
    const [appliedKeyword, setAppliedKeyword] = useState('');
    const [reqFilterId, setReqFilterId] = useState('');

    useEffect(() => {
        setReqFilterId(selectedNodeId || '');
    }, [selectedNodeId]);

    useEffect(() => {
        const ws = new WebSocket('ws://127.0.0.1:8000/ws/compiler');
        setLoading(true);
        setError(null);

        ws.onopen = () => {
            const storedProjectPath = (window as any).arcWorkspaceRoot || localStorage.getItem('arc_project_path');
            ws.send(JSON.stringify({
                command: 'traceabilityData',
                nodeId: reqFilterId || '',
                keyword: appliedKeyword || '',
                projectPath: storedProjectPath
            }));
        };

        ws.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);
                if (message.type === 'traceabilityData') {
                    setData(message.data);
                    setLoading(false);
                    ws.close();
                }
            } catch (e) {
                console.error("Failed to parse websocket message", e);
            }
        };

        ws.onerror = (err) => {
            console.error("WebSocket error", err);
            setError("Failed to connect to backend");
            setLoading(false);
        };

        return () => {
            if (ws.readyState === WebSocket.OPEN) {
                ws.close();
            }
        };
    }, [reqFilterId, appliedKeyword]);

    const toggleExpand = (id: string) => {
        setExpandedItems(prev => ({ ...prev, [id]: !prev[id] }));
    };

    const handleOpenFile = (filePath: string, line?: string) => {
        const vscode = (window as any).vscode;
        if (vscode && filePath) {
            vscode.postMessage({ command: 'openFile', filePath, line });
        }
    };

    const handleOpenRequirementById = (reqId?: string) => {
        const vscode = (window as any).vscode;
        const id = String(reqId || '').trim();
        if (vscode && id) {
            vscode.postMessage({ command: 'openRequirementById', reqId: id });
        }
    };

    const clearFilters = () => {
        setSearchText('');
        setAppliedKeyword('');
        setReqFilterId('');
        onClearReqFilter?.();
    };

    const applySearch = () => {
        setAppliedKeyword(searchText.trim());
    };

    if (loading) return <div className="p-4 flex justify-center text-[var(--vscode-descriptionForeground)]"><Loader2 className="animate-spin mr-2" size={16}/> Loading...</div>;
    if (error) return <div className="p-4 text-[var(--vscode-errorForeground)]">Error: {error}</div>;
    if (!data) return <div className="p-4 text-[var(--vscode-descriptionForeground)]">No traceability data available.</div>;

    return (
        <div className="flex flex-col h-full overflow-y-auto custom-scrollbar pb-4">
            <div className="sticky top-0 z-10 px-3 py-2 bg-[var(--vscode-sideBar-background)] border-b border-[var(--vscode-panel-border)] space-y-2">
                <div className="flex gap-1">
                    {[
                        { key: 'REQ', label: `REQ (${data.requirements.length})` },
                        { key: 'IF', label: `IF (${data.interfaces.length})` },
                        { key: 'TEST', label: `TEST (${data.tests.length})` }
                    ].map(tab => (
                        <button
                            key={tab.key}
                            onClick={() => setEntityTab(tab.key as 'REQ' | 'IF' | 'TEST')}
                            className={cn(
                                "px-2 py-1 text-[10px] rounded border",
                                entityTab === tab.key
                                    ? "border-[var(--vscode-focusBorder)] text-[var(--vscode-foreground)] bg-[var(--vscode-list-activeSelectionBackground)]"
                                    : "border-[var(--vscode-panel-border)] text-[var(--vscode-descriptionForeground)] hover:text-[var(--vscode-foreground)]"
                            )}
                        >
                            {tab.label}
                        </button>
                    ))}
                </div>
                <div className="flex gap-1">
                    <div className="relative flex-1">
                        <Search size={12} className="absolute left-2 top-1.5 text-[var(--vscode-descriptionForeground)]" />
                        <input
                            value={searchText}
                            onChange={(e) => setSearchText(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter') {
                                    e.preventDefault();
                                    applySearch();
                                }
                            }}
                            placeholder="Search name/description/id/file..."
                            className="w-full pl-7 pr-2 py-1 text-xs bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-sm focus:outline-none focus:border-[var(--vscode-focusBorder)]"
                        />
                    </div>
                    <button
                        onClick={applySearch}
                        className="px-2 py-1 text-[10px] rounded border border-[var(--vscode-panel-border)] hover:bg-[var(--vscode-list-hoverBackground)] inline-flex items-center gap-1"
                        title="Apply search"
                    >
                        Search
                    </button>
                    <button
                        onClick={clearFilters}
                        className="px-2 py-1 text-[10px] rounded border border-[var(--vscode-panel-border)] hover:bg-[var(--vscode-list-hoverBackground)] inline-flex items-center gap-1"
                        title="Clear filters (REQ + Search)"
                    >
                        <RotateCcw size={11} />
                        Clear
                    </button>
                </div>
                {(reqFilterId || appliedKeyword) && (
                    <div className="text-[10px] text-[var(--vscode-descriptionForeground)]">
                        {reqFilterId ? `Filter REQ: ${reqFilterId}` : 'Filter REQ: ALL'}{appliedKeyword ? ` | Search: "${appliedKeyword}"` : ''}
                    </div>
                )}
            </div>

            {entityTab === 'REQ' && (
                <CollapsibleSection title={`Requirements (${data.requirements.length})`} defaultOpen={true}>
                    <div className="space-y-2">
                        {data.requirements.map(req => (
                            <div
                                key={req.req_id}
                                className="border border-[var(--vscode-panel-border)] rounded-sm bg-[var(--vscode-editor-background)] p-2 cursor-pointer hover:bg-[var(--vscode-list-hoverBackground)]"
                                onClick={() => handleOpenRequirementById(req.req_id)}
                                title={`Open and highlight ${req.req_id} in requirements.yaml`}
                            >
                                <div className="text-xs font-semibold">{req.req_id}</div>
                                <div className="text-[11px] opacity-85 mt-1 whitespace-pre-wrap break-words">
                                    {req.description || 'No description'}
                                </div>
                                <div className="text-[10px] text-[var(--vscode-descriptionForeground)] mt-1">
                                    deps: {(req.dependencies || []).length} | scenario: {(req.scenario || []).length}
                                </div>
                            </div>
                        ))}
                        {data.requirements.length === 0 && <div className="text-xs text-[var(--vscode-descriptionForeground)] italic px-1">No requirement rows.</div>}
                    </div>
                </CollapsibleSection>
            )}

            {entityTab === 'IF' && (
            <CollapsibleSection title={`Interfaces (${data.interfaces.length})`} defaultOpen={true}>
                <div className="space-y-2">
                    {data.interfaces.map(iface => (
                        <div key={iface.interface_id} className="border border-[var(--vscode-panel-border)] rounded-sm bg-[var(--vscode-editor-background)] overflow-hidden">
                            <div 
                                className="px-2 py-1.5 flex items-center justify-between cursor-pointer hover:bg-[var(--vscode-list-hoverBackground)] transition-colors"
                                onClick={() => toggleExpand(iface.interface_id)}
                            >
                                <div className="flex items-center gap-2 overflow-hidden flex-1">
                                    {expandedItems[iface.interface_id] ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
                                    <span className={cn(
                                        "text-[10px] font-bold px-1.5 py-0.5 rounded shrink-0",
                                        iface.type === 'UI' ? 'bg-blue-500/10 text-blue-500' :
                                        iface.type === 'API' ? 'bg-green-500/10 text-green-500' :
                                        iface.type === 'FUNC' ? 'bg-purple-500/10 text-purple-500' :
                                        'bg-gray-500/10 text-gray-500'
                                    )}>{iface.type}</span>
                                    <span className="text-xs font-medium truncate" title={iface.interface_id}>{iface.interface_id}</span>
                                </div>
                                {iface.implemented ? <CheckCircle size={14} className="text-[var(--vscode-charts-green)] shrink-0" /> : <div className="w-3.5 h-3.5 rounded-full border border-[var(--vscode-descriptionForeground)] opacity-30 shrink-0" />}
                            </div>
                            
                            {expandedItems[iface.interface_id] && (
                                <div className="px-3 pb-2 pt-1 border-t border-[var(--vscode-panel-border)] bg-[var(--vscode-textBlockQuote-background)]">
                                    {iface.file_path ? (
                                        <div 
                                            className="text-xs flex items-center gap-1.5 text-[var(--vscode-textLink-foreground)] hover:text-[var(--vscode-textLink-activeForeground)] cursor-pointer mb-2 group"
                                            onClick={() => handleOpenFile(iface.file_path, iface.first_line)}
                                            title={iface.file_path}
                                        >
                                            <Code size={12} className="group-hover:scale-110 transition-transform" />
                                            <span className="truncate underline decoration-dotted underline-offset-2">{iface.file_path.split(/[/\\]/).pop()} {iface.first_line ? `:${iface.first_line}` : ''}</span>
                                        </div>
                                    ) : <div className="text-[10px] text-[var(--vscode-descriptionForeground)] italic mb-2">Not implemented yet</div>}
                                    
                                    <div className="space-y-1.5">
                                        {iface.req_ids && iface.req_ids.length > 0 && (
                                            <div className="text-[10px]">
                                                <span className="font-semibold text-[var(--vscode-descriptionForeground)] opacity-70">Requirements:</span>
                                                <div className="ml-1 flex flex-wrap gap-1 mt-0.5">
                                                    {iface.req_ids.map(r => (
                                                        <span
                                                            key={r}
                                                            onClick={() => handleOpenRequirementById(r)}
                                                            className="bg-[var(--vscode-badge-background)] text-[var(--vscode-badge-foreground)] px-1.5 py-0.5 rounded-sm cursor-pointer hover:opacity-85"
                                                            title={`Open and highlight ${r}`}
                                                        >
                                                            {r}
                                                        </span>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                                        {iface.callers && iface.callers.length > 0 && (
                                            <div className="text-[10px]">
                                                <span className="font-semibold text-[var(--vscode-descriptionForeground)] opacity-70">Callers:</span>
                                                <div className="ml-1 flex flex-wrap gap-1 mt-0.5">
                                                    {iface.callers.map(c => <span key={c} className="bg-[var(--vscode-badge-background)] text-[var(--vscode-badge-foreground)] px-1.5 py-0.5 rounded-sm">{c}</span>)}
                                                </div>
                                            </div>
                                        )}
                                        {iface.callees && iface.callees.length > 0 && (
                                            <div className="text-[10px]">
                                                <span className="font-semibold text-[var(--vscode-descriptionForeground)] opacity-70">Callees:</span>
                                                <div className="ml-1 flex flex-wrap gap-1 mt-0.5">
                                                    {iface.callees.map(c => <span key={c} className="bg-[var(--vscode-badge-background)] text-[var(--vscode-badge-foreground)] px-1.5 py-0.5 rounded-sm">{c}</span>)}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}
                    {data.interfaces.length === 0 && <div className="text-xs text-[var(--vscode-descriptionForeground)] italic px-1">No interfaces linked.</div>}
                </div>
            </CollapsibleSection>
            )}

            {entityTab === 'TEST' && (
            <CollapsibleSection title={`Tests (${data.tests.length})`} defaultOpen={true}>
                 <div className="space-y-2">
                    {data.tests.map(test => (
                        <div key={test.test_id} className="border border-[var(--vscode-panel-border)] rounded-sm bg-[var(--vscode-editor-background)] overflow-hidden">
                            <div 
                                className="px-2 py-1.5 flex items-center justify-between cursor-pointer hover:bg-[var(--vscode-list-hoverBackground)] transition-colors"
                                onClick={() => toggleExpand(test.test_id)}
                            >
                                <div className="flex items-center gap-2 overflow-hidden flex-1">
                                    {expandedItems[test.test_id] ? <ChevronDown size={14} className="shrink-0" /> : <ChevronRight size={14} className="shrink-0" />}
                                    <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-yellow-500/10 text-yellow-500 shrink-0">{test.type}</span>
                                    <span className="text-xs font-medium truncate" title={test.test_id}>{test.test_id}</span>
                                </div>
                            </div>
                            
                            {expandedItems[test.test_id] && (
                                <div className="px-3 pb-2 pt-1 border-t border-[var(--vscode-panel-border)] bg-[var(--vscode-textBlockQuote-background)]">
                                     {test.file_path ? (
                                        <div 
                                            className="text-xs flex items-center gap-1.5 text-[var(--vscode-textLink-foreground)] hover:text-[var(--vscode-textLink-activeForeground)] cursor-pointer mb-2 group"
                                            onClick={() => handleOpenFile(test.file_path, test.first_line)}
                                            title={test.file_path}
                                        >
                                            <FileText size={12} className="group-hover:scale-110 transition-transform" />
                                            <span className="truncate underline decoration-dotted underline-offset-2">{test.file_path.split(/[/\\]/).pop()} {test.first_line ? `:${test.first_line}` : ''}</span>
                                        </div>
                                    ) : <div className="text-[10px] text-[var(--vscode-descriptionForeground)] italic mb-2">File not found</div>}
                                    
                                    {test.interface_ids && test.interface_ids.length > 0 && (
                                        <div className="text-[10px]">
                                            <span className="font-semibold text-[var(--vscode-descriptionForeground)] opacity-70">Covers:</span>
                                            <div className="ml-1 flex flex-wrap gap-1 mt-0.5">
                                                {test.interface_ids.map(i => <span key={i} className="bg-[var(--vscode-badge-background)] text-[var(--vscode-badge-foreground)] px-1.5 py-0.5 rounded-sm">{i}</span>)}
                                            </div>
                                        </div>
                                    )}
                                    {test.req_id && (
                                        <div className="text-[10px] mt-1">
                                            <span className="font-semibold text-[var(--vscode-descriptionForeground)] opacity-70">Requirement:</span>
                                            <span
                                                onClick={() => handleOpenRequirementById(test.req_id)}
                                                className="ml-1 bg-[var(--vscode-badge-background)] text-[var(--vscode-badge-foreground)] px-1.5 py-0.5 rounded-sm cursor-pointer hover:opacity-85"
                                                title={`Open and highlight ${test.req_id}`}
                                            >
                                                {test.req_id}
                                            </span>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    ))}
                    {data.tests.length === 0 && <div className="text-xs text-[var(--vscode-descriptionForeground)] italic px-1">No tests linked.</div>}
                </div>
            </CollapsibleSection>
            )}
        </div>
    );
};

export default function PropertiesPanel({ node, onUpdate, onDelete, onClose, onClearSelection }: PropertiesPanelProps) {
    const [formData, setFormData] = useState<RequirementNode>(node || { id: '', name: '' });
    const [panelWidth, setPanelWidth] = useState(350); 
    const [isResizing, setIsResizing] = useState(false);
    const [activeTab, setActiveTab] = useState<'properties' | 'traceability'>('properties');

    // VS Code Style Classes
    const inputClasses = "w-full px-2 py-1 text-xs bg-[var(--vscode-input-background)] text-[var(--vscode-input-foreground)] border border-[var(--vscode-input-border)] rounded-sm focus:outline-none focus:border-[var(--vscode-focusBorder)] placeholder-[var(--vscode-input-placeholderForeground)]";
    const labelClasses = "block mb-1 text-xs text-[var(--vscode-foreground)] opacity-80 font-medium";
    const iconButtonClasses = "p-1 rounded-sm text-[var(--vscode-icon-foreground)] hover:bg-[var(--vscode-toolbar-hoverBackground)] cursor-pointer transition-colors";
    const selectClasses = "w-full px-1 py-0.5 text-[10px] font-bold bg-[var(--vscode-dropdown-background)] text-[var(--vscode-dropdown-foreground)] border border-[var(--vscode-dropdown-border)] rounded-sm focus:outline-none focus:border-[var(--vscode-focusBorder)] cursor-pointer";

    useEffect(() => {
        setFormData(node || { id: '', name: '' });
    }, [node]);

    // Debounced sync to backend: local typing is instant, persistence is batched.
    useEffect(() => {
        if (!node || !formData) return;
        if (formData.id !== node.id) return;
        if (JSON.stringify(formData) === JSON.stringify(node)) return;
        const timer = window.setTimeout(() => {
            onUpdate(node.id, formData);
        }, 180);
        return () => window.clearTimeout(timer);
    }, [formData, node, onUpdate]);

    // Resizing Logic
    useEffect(() => {
        const handleMouseMove = (e: MouseEvent) => {
            if (!isResizing) return;
            const newWidth = document.body.clientWidth - e.clientX;
            if (newWidth > 200 && newWidth < 800) {
                setPanelWidth(newWidth);
            }
        };

        const handleMouseUp = () => {
            setIsResizing(false);
            document.body.style.cursor = 'default';
        };

        if (isResizing) {
            window.addEventListener('mousemove', handleMouseMove);
            window.addEventListener('mouseup', handleMouseUp);
            document.body.style.cursor = 'col-resize';
        }

        return () => {
            window.removeEventListener('mousemove', handleMouseMove);
            window.removeEventListener('mouseup', handleMouseUp);
            document.body.style.cursor = 'default';
        };
    }, [isResizing]);

    const handleChange = (field: string, value: any) => {
        const newData = { ...formData, [field]: value };
        setFormData(newData);
    };

    const hasSelection = !!node;

    return (
        <div 
            className="flex flex-col h-full bg-[var(--vscode-sideBar-background)] border-l border-[var(--vscode-panel-border)] text-[var(--vscode-foreground)] relative select-none"
            style={{ width: panelWidth, minWidth: 200 }}
        >
            {/* Resizer Handle */}
            <div
                onMouseDown={(e) => {
                    e.preventDefault();
                    setIsResizing(true);
                }}
                className={cn(
                    "absolute left-0 top-0 bottom-0 w-1 cursor-col-resize z-50 hover:bg-[var(--vscode-focusBorder)] transition-colors",
                    isResizing && "bg-[var(--vscode-focusBorder)]"
                )}
            />

            {/* Header with Tabs */}
            <div className="flex flex-col border-b border-[var(--vscode-panel-border)] bg-[var(--vscode-sideBarSectionHeader-background)]">
                <div className="flex items-center justify-between px-4 py-2">
                    <div className="flex items-center gap-2 overflow-hidden">
                        <span className="font-semibold text-xs uppercase text-[var(--vscode-sideBarSectionHeader-foreground)] truncate" title={formData.id}>
                            {hasSelection ? formData.id : 'No Selection'}
                        </span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button onClick={onClose} className={iconButtonClasses} title="Close Panel">
                            <X size={16} />
                        </button>
                    </div>
                </div>
                
                <div className="flex px-2 gap-1">
                    <button 
                        onClick={() => setActiveTab('properties')}
                        className={cn(
                            "flex-1 py-1.5 text-xs font-medium border-b-2 transition-colors",
                            activeTab === 'properties' 
                                ? "border-[var(--vscode-panelTitle-activeBorder)] text-[var(--vscode-panelTitle-activeForeground)]" 
                                : "border-transparent text-[var(--vscode-panelTitle-inactiveForeground)] hover:text-[var(--vscode-panelTitle-activeForeground)]"
                        )}
                    >
                        Properties
                    </button>
                    <button 
                        onClick={() => setActiveTab('traceability')}
                        className={cn(
                            "flex-1 py-1.5 text-xs font-medium border-b-2 transition-colors",
                            activeTab === 'traceability' 
                                ? "border-[var(--vscode-panelTitle-activeBorder)] text-[var(--vscode-panelTitle-activeForeground)]" 
                                : "border-transparent text-[var(--vscode-panelTitle-inactiveForeground)] hover:text-[var(--vscode-panelTitle-activeForeground)]"
                        )}
                    >
                        Traceability
                    </button>
                </div>
            </div>

            {/* Content Scroll Area */}
            <div className="flex-1 overflow-y-auto custom-scrollbar relative">
                {activeTab === 'properties' ? (
                    hasSelection ? (
                    <>
                        {/* Basic Info Section */}
                        <CollapsibleSection title="General" defaultOpen={true}>
                            <div className="space-y-3">
                                <div>
                                    <label className={labelClasses}>ID</label>
                                    <input 
                                        type="text" 
                                        value={formData.id || ''} 
                                        disabled
                                        className={cn(inputClasses, "opacity-60 cursor-not-allowed")}
                                    />
                                </div>
                                <div>
                                    <label className={labelClasses}>Name</label>
                                    <input 
                                        type="text" 
                                        value={formData.name || ''} 
                                        onChange={(e) => handleChange('name', e.target.value)}
                                        className={inputClasses}
                                        placeholder="Requirement Name"
                                    />
                                </div>
                                <div>
                                    <div className="flex justify-between items-center mb-1">
                                        <label className={labelClasses}>Description</label>
                                    </div>
                                    <textarea 
                                        value={formData.description || ''} 
                                        onChange={(e) => handleChange('description', e.target.value)}
                                        rows={4}
                                        className={cn(inputClasses, "resize-y font-sans")}
                                        placeholder="Description..."
                                    />
                                     <p className="text-[10px] text-[var(--vscode-descriptionForeground)] mt-1 opacity-70">Supports Markdown.</p>
                                </div>
                            </div>
                        </CollapsibleSection>

                        {/* Dependencies Section */}
                        <CollapsibleSection title="Dependencies" onAdd={() => {
                            const currentDeps = formData.dependencies || [];
                            handleChange('dependencies', [...currentDeps, '']);
                        }}>
                            <div className="space-y-2">
                                {(formData.dependencies || []).length === 0 && (
                                    <div className="text-xs text-[var(--vscode-descriptionForeground)] italic py-1">No dependencies.</div>
                                )}
                                {(formData.dependencies || []).map((dep: string, idx: number) => (
                                    <div key={idx} className="flex gap-2 group items-center">
                                        <input 
                                            type="text" 
                                            value={dep}
                                            onChange={(e) => {
                                                const newDeps = [...(formData.dependencies || [])];
                                                newDeps[idx] = e.target.value;
                                                handleChange('dependencies', newDeps);
                                            }}
                                            className={inputClasses}
                                            placeholder="REQ-ID"
                                        />
                                        <button onClick={() => {
                                            const newDeps = [...(formData.dependencies || [])];
                                            newDeps.splice(idx, 1);
                                            handleChange('dependencies', newDeps);
                                        }} className="opacity-0 group-hover:opacity-100 text-[var(--vscode-errorForeground)] hover:bg-[var(--vscode-list-hoverBackground)] p-1 rounded transition-opacity">
                                            <Trash2 size={14} />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        </CollapsibleSection>

                        {/* Scenario Section */}
                        <CollapsibleSection title="Scenario" defaultOpen={true}>
                            <div className="space-y-4">
                                <div className="border border-[var(--vscode-panel-border)] rounded-sm bg-[var(--vscode-editor-background)] overflow-hidden">
                                    <div className="p-2">
                                        {/* Steps */}
                                        <div>
                                            <div className="flex justify-between items-center mb-1">
                                                <label className="text-[10px] font-medium text-[var(--vscode-descriptionForeground)] uppercase tracking-wider">Gherkin Steps</label>
                                                <button onClick={() => {
                                                    const currentScenario = formData.scenario || [];
                                                    handleChange('scenario', [...currentScenario, { keyword: 'GIVEN', content: '' }]);
                                                }} className="text-[var(--vscode-textLink-foreground)] hover:text-[var(--vscode-textLink-activeForeground)] cursor-pointer" title="Add Step">
                                                    <Plus size={12} />
                                                </button>
                                            </div>
                                            
                                            <div className="space-y-2">
                                                {(formData.scenario || []).map((step, stepIdx) => (
                                                    <div key={stepIdx} className="bg-[var(--vscode-input-background)]/30 p-1.5 rounded-sm border border-[var(--vscode-panel-border)] group relative">
                                                        <button 
                                                            onClick={() => {
                                                                const newScenario = [...(formData.scenario || [])];
                                                                newScenario.splice(stepIdx, 1);
                                                                handleChange('scenario', newScenario);
                                                            }} 
                                                            className="absolute right-1 top-1 opacity-0 group-hover:opacity-100 text-[var(--vscode-descriptionForeground)] hover:text-[var(--vscode-errorForeground)] p-0.5 transition-opacity"
                                                        >
                                                            <X size={12} />
                                                        </button>
                                                        <div className="flex gap-2 items-start">
                                                            <div className="w-16 shrink-0">
                                                                <select 
                                                                    value={step.keyword}
                                                                    onChange={(e) => {
                                                                        const newScenario = [...(formData.scenario || [])];
                                                                        newScenario[stepIdx] = { ...newScenario[stepIdx], keyword: e.target.value };
                                                                        handleChange('scenario', newScenario);
                                                                    }}
                                                                    className={selectClasses}
                                                                >
                                                                    <option value="GIVEN">GIVEN</option>
                                                                    <option value="WHEN">WHEN</option>
                                                                    <option value="THEN">THEN</option>
                                                                    <option value="AND">AND</option>
                                                                    <option value="BUT">BUT</option>
                                                                </select>
                                                            </div>
                                                            <textarea 
                                                                value={step.content} 
                                                                onChange={(e) => {
                                                                    const newScenario = [...(formData.scenario || [])];
                                                                    newScenario[stepIdx] = { ...newScenario[stepIdx], content: e.target.value };
                                                                    handleChange('scenario', newScenario);
                                                                }}
                                                                className={cn(inputClasses, "py-1 min-h-[2.5rem] resize-y")}
                                                                placeholder="Step description..."
                                                                rows={2}
                                                            />
                                                        </div>
                                                    </div>
                                                ))}
                                                {(formData.scenario || []).length === 0 && (
                                                    <div className="text-[10px] text-[var(--vscode-descriptionForeground)] italic opacity-60">No scenario steps defined</div>
                                                )}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </CollapsibleSection>

                        {/* Actions Footer */}
                        <div className="p-4 border-t border-[var(--vscode-panel-border)] mt-4 space-y-2">
                             <button 
                                onClick={() => onDelete(node.id)}
                                className="w-full py-1.5 text-xs bg-[var(--vscode-button-background)] text-[var(--vscode-button-foreground)] hover:bg-[var(--vscode-button-hoverBackground)] border-none rounded-sm flex items-center justify-center gap-2 transition-colors"
                            >
                                <Trash2 size={14} />
                                Delete Requirement
                            </button>
                        </div>
                    </>
                    ) : (
                        <div className="p-4 text-xs text-[var(--vscode-descriptionForeground)]">
                            Select a requirement node on canvas to edit properties.
                        </div>
                    )
                ) : (
                    <TraceabilityTab selectedNodeId={hasSelection ? formData.id : undefined} onClearReqFilter={onClearSelection} />
                )}
            </div>
        </div>
    );
}
