import { useState, useEffect } from 'react';
import { X, Plus, Trash2, Image as ImageIcon, ChevronDown } from 'lucide-react';

interface ScenarioStep {
    given: string;
    when: string;
    then: string;
}

interface Scenario {
    id: string;
    name: string;
    prerequisites: string[];
    steps: ScenarioStep[];
}

interface PropertiesPanelProps {
    node: any;
    onUpdate: (id: string, updates: any) => void;
    onDelete: (id: string) => void;
    onClose?: () => void;
}

export default function PropertiesPanel({ node, onUpdate, onDelete, onClose }: PropertiesPanelProps) {
    const [formData, setFormData] = useState(node || {});
    const [panelWidth, setPanelWidth] = useState(400); 
    const [isResizing, setIsResizing] = useState(false);
    
    useEffect(() => {
        setFormData(node || {});
    }, [node]);

    // Resizing Logic
    useEffect(() => {
        const handleMouseMove = (e: MouseEvent) => {
            if (!isResizing) return;
            const newWidth = document.body.clientWidth - e.clientX;
            if (newWidth > 300 && newWidth < 800) {
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
        onUpdate(node.id, newData); // Auto-save on change
    };

    // --- Dependencies Logic ---
    const handleAddDependency = () => {
        const currentDeps = formData.dependencies || [];
        handleChange('dependencies', [...currentDeps, '']);
    };

    const handleUpdateDependency = (index: number, value: string) => {
        const newDeps = [...(formData.dependencies || [])];
        newDeps[index] = value;
        handleChange('dependencies', newDeps);
    };

    const handleRemoveDependency = (index: number) => {
        const newDeps = [...(formData.dependencies || [])];
        newDeps.splice(index, 1);
        handleChange('dependencies', newDeps);
    };

    // --- Scenarios Logic ---
    const handleAddScenario = () => {
        const newScenario: Scenario = {
            id: `${node.id}:SCE-${(formData.scenarios || []).length}`,
            name: 'New Scenario',
            prerequisites: [],
            steps: []
        };
        handleChange('scenarios', [...(formData.scenarios || []), newScenario]);
    };

    const handleUpdateScenario = (index: number, field: keyof Scenario, value: any) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[index] = { ...newScenarios[index], [field]: value };
        handleChange('scenarios', newScenarios);
    };

    const handleRemoveScenario = (index: number) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios.splice(index, 1);
        handleChange('scenarios', newScenarios);
    };

    // --- Scenario Prerequisites ---
    const handleAddPrerequisite = (scenarioIndex: number) => {
        const newScenarios = [...(formData.scenarios || [])];
        const currentPrereqs = newScenarios[scenarioIndex].prerequisites || [];
        newScenarios[scenarioIndex].prerequisites = [...currentPrereqs, ''];
        handleChange('scenarios', newScenarios);
    };

    const handleUpdatePrerequisite = (scenarioIndex: number, prereqIndex: number, value: string) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[scenarioIndex].prerequisites[prereqIndex] = value;
        handleChange('scenarios', newScenarios);
    };

    const handleRemovePrerequisite = (scenarioIndex: number, prereqIndex: number) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[scenarioIndex].prerequisites.splice(prereqIndex, 1);
        handleChange('scenarios', newScenarios);
    };

    // --- Steps ---
    const handleAddStep = (scenarioIndex: number) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[scenarioIndex].steps.push({ given: '', when: '', then: '' });
        handleChange('scenarios', newScenarios);
    };

    const handleUpdateStep = (scenarioIndex: number, stepIndex: number, field: keyof ScenarioStep, value: string) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[scenarioIndex].steps[stepIndex][field] = value;
        handleChange('scenarios', newScenarios);
    };

    const handleRemoveStep = (scenarioIndex: number, stepIndex: number) => {
        const newScenarios = [...(formData.scenarios || [])];
        newScenarios[scenarioIndex].steps.splice(stepIndex, 1);
        handleChange('scenarios', newScenarios);
    };

    if (!node) return null;

    return (
        <div 
            className="border-l border-[var(--vscode-panel-border)] bg-[var(--vscode-sideBar-background)] flex flex-col h-full relative text-[var(--vscode-foreground)] shadow-xl"
            style={{ width: panelWidth }}
        >
            {/* Resizer Handle */}
            <div
                onMouseDown={(e) => {
                    e.preventDefault();
                    setIsResizing(true);
                }}
                className={`absolute left-0 top-0 bottom-0 w-1 cursor-col-resize z-20 hover:bg-[var(--vscode-focusBorder)] transition-colors ${isResizing ? 'bg-[var(--vscode-focusBorder)]' : 'bg-transparent'}`}
            />

            {/* Header */}
            <div className="px-6 py-5 border-b border-[var(--vscode-panel-border)] flex justify-between items-center bg-[var(--vscode-sideBar-background)]">
                <h2 className="text-xl font-bold truncate pr-4 text-[var(--vscode-foreground)] tracking-tight">{formData.id}</h2>
                <div className="flex gap-2 shrink-0">
                    <button onClick={onClose} className="p-2 hover:bg-[var(--vscode-toolbar-hoverBackground)] rounded-lg text-[var(--vscode-descriptionForeground)] hover:text-[var(--vscode-foreground)] transition-colors" title="Close">
                        <X size={18} />
                    </button>
                </div>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-6 space-y-8">
                
                {/* Name */}
                <div className="space-y-3">
                    <label className="text-xs font-bold text-[var(--vscode-descriptionForeground)] uppercase tracking-wider ml-1">Name</label>
                    <input 
                        type="text" 
                        value={formData.name || ''} 
                        onChange={(e) => handleChange('name', e.target.value)}
                        className="w-full px-4 py-2.5 text-sm bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-lg focus:outline-none focus:ring-2 focus:ring-[var(--vscode-focusBorder)] focus:border-transparent transition-all shadow-sm"
                        placeholder="Requirement Name"
                    />
                </div>

                {/* Description */}
                <div className="space-y-3">
                    <div className="flex justify-between items-center ml-1">
                        <label className="text-xs font-bold text-[var(--vscode-descriptionForeground)] uppercase tracking-wider">Description</label>
                        <ImageIcon size={14} className="text-[var(--vscode-descriptionForeground)] opacity-60" />
                    </div>
                    <div className="relative group">
                        <textarea 
                            value={formData.description || ''} 
                            onChange={(e) => handleChange('description', e.target.value)}
                            rows={6}
                            className="w-full px-4 py-3 text-sm bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-lg focus:outline-none focus:ring-2 focus:ring-[var(--vscode-focusBorder)] focus:border-transparent font-sans resize-y transition-all shadow-sm"
                            placeholder="Description..."
                        />
                    </div>
                    <p className="text-xs text-[var(--vscode-descriptionForeground)] opacity-60 ml-1">Use [image](path) for images.</p>
                </div>

                {/* Dependencies */}
                <div className="space-y-3">
                    <label className="text-xs font-bold text-[var(--vscode-descriptionForeground)] uppercase tracking-wider ml-1">Dependencies</label>
                    <button 
                        onClick={handleAddDependency}
                        className="w-full flex items-center justify-between px-4 py-2.5 text-sm bg-[var(--vscode-button-secondaryBackground)] text-[var(--vscode-button-secondaryForeground)] border border-transparent rounded-lg hover:bg-[var(--vscode-button-secondaryHoverBackground)] text-left transition-colors"
                    >
                        <span className="font-medium">+ Add Dependency</span>
                        <ChevronDown size={14} className="opacity-70" />
                    </button>
                    
                    {(formData.dependencies || []).length > 0 && (
                        <div className="space-y-3 mt-3">
                            {(formData.dependencies || []).map((dep: string, idx: number) => (
                                <div key={idx} className="flex gap-3 group">
                                    <input 
                                        type="text" 
                                        value={dep}
                                        onChange={(e) => handleUpdateDependency(idx, e.target.value)}
                                        className="flex-1 px-4 py-2 text-sm bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-lg focus:ring-2 focus:ring-[var(--vscode-focusBorder)] focus:border-transparent focus:outline-none transition-all"
                                        placeholder="REQ-ID"
                                    />
                                    <button onClick={() => handleRemoveDependency(idx)} className="text-[var(--vscode-descriptionForeground)] hover:text-red-400 p-2 rounded-lg hover:bg-[var(--vscode-list-hoverBackground)] transition-colors opacity-0 group-hover:opacity-100">
                                        <X size={16} />
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}
                </div>

                {/* Scenarios Header */}
                <div className="space-y-4 pt-4 border-t border-[var(--vscode-panel-border)]">
                    <div className="flex justify-between items-center ml-1">
                        <label className="text-xs font-bold text-[var(--vscode-descriptionForeground)] uppercase tracking-wider">Scenarios</label>
                    </div>
                    <button 
                        onClick={handleAddScenario}
                        className="w-full py-3 flex items-center justify-center gap-2 bg-[var(--vscode-button-background)] text-[var(--vscode-button-foreground)] hover:bg-[var(--vscode-button-hoverBackground)] rounded-lg text-sm font-semibold transition-colors shadow-sm"
                    >
                        <Plus size={16} />
                        Add Scenario
                    </button>
                </div>

                {/* Scenario List */}
                <div className="space-y-6">
                    {(formData.scenarios || []).map((scenario: Scenario, sIdx: number) => (
                        <div key={sIdx} className="border border-[var(--vscode-panel-border)] rounded-xl bg-[var(--vscode-editor-background)] shadow-sm overflow-hidden transition-shadow hover:shadow-md">
                            {/* Scenario Header */}
                            <div className="p-4 border-b border-[var(--vscode-panel-border)] flex justify-between items-start bg-[var(--vscode-list-hoverBackground)]/20">
                                <div className="flex-1 space-y-1.5 mr-4">
                                    <input 
                                        type="text" 
                                        value={scenario.name}
                                        onChange={(e) => handleUpdateScenario(sIdx, 'name', e.target.value)}
                                        className="w-full bg-transparent border-b border-transparent hover:border-[var(--vscode-input-border)] focus:border-[var(--vscode-focusBorder)] text-sm font-bold focus:outline-none px-1 py-0.5 placeholder-[var(--vscode-input-placeholderForeground)] transition-colors"
                                        placeholder="Scenario Name"
                                    />
                                    <div className="font-mono text-[10px] text-[var(--vscode-descriptionForeground)] opacity-60 pl-1">{scenario.id}</div>
                                </div>
                                <button onClick={() => handleRemoveScenario(sIdx)} className="bg-transparent hover:bg-red-50 text-[var(--vscode-descriptionForeground)] hover:text-red-500 p-2 rounded-lg transition-all duration-200" title="Delete Scenario">
                                    <Trash2 size={14} />
                                </button>
                            </div>

                            <div className="p-5 space-y-6">
                                {/* Prerequisites */}
                                <div className="space-y-3">
                                    <label className="text-xs font-semibold text-[var(--vscode-descriptionForeground)] block ml-1 opacity-80">Prerequisites</label>
                                    <button 
                                        onClick={() => handleAddPrerequisite(sIdx)}
                                        className="w-full flex items-center justify-between px-3 py-2 text-xs bg-[var(--vscode-dropdown-background)] border border-[var(--vscode-dropdown-border)] rounded-lg hover:bg-[var(--vscode-list-hoverBackground)] text-left opacity-80 transition-colors"
                                    >
                                        <span>+ Add Prerequisite</span>
                                        <ChevronDown size={12} className="opacity-60" />
                                    </button>
                                    {(scenario.prerequisites || []).length > 0 && (
                                        <div className="space-y-2 mt-2">
                                            {scenario.prerequisites.map((req, rIdx) => (
                                                <div key={rIdx} className="flex gap-2 group">
                                                    <input 
                                                        type="text"
                                                        value={req}
                                                        onChange={(e) => handleUpdatePrerequisite(sIdx, rIdx, e.target.value)}
                                                        className="flex-1 px-3 py-1.5 text-xs bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-md focus:outline-none focus:ring-1 focus:ring-[var(--vscode-focusBorder)] focus:border-transparent transition-all"
                                                        placeholder="Condition / Requirement"
                                                    />
                                                    <button onClick={() => handleRemovePrerequisite(sIdx, rIdx)} className="text-[var(--vscode-descriptionForeground)] hover:text-red-400 p-1 opacity-0 group-hover:opacity-100 transition-opacity">
                                                        <X size={12} />
                                                    </button>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </div>

                                {/* Steps */}
                                <div className="space-y-3">
                                    <div className="flex justify-between items-center border-l-2 border-[var(--vscode-focusBorder)] pl-3 py-1">
                                        <label className="text-xs font-semibold text-[var(--vscode-descriptionForeground)] opacity-80">Steps</label>
                                        <button 
                                            onClick={() => handleAddStep(sIdx)}
                                            className="text-xs text-[var(--vscode-textLink-foreground)] hover:text-[var(--vscode-textLink-activeForeground)] flex items-center gap-1.5 font-medium px-2 py-1 rounded hover:bg-[var(--vscode-list-hoverBackground)] transition-colors"
                                        >
                                            <Plus size={12} /> Add Step
                                        </button>
                                    </div>
                                    
                                    <div className="space-y-3 mt-3">
                                        {scenario.steps.map((step, stepIdx) => (
                                            <div key={stepIdx} className="grid grid-cols-[1fr_1fr_1fr_auto] gap-2 items-center group">
                                                <div className="col-span-3 grid grid-cols-3 gap-0 bg-[var(--vscode-input-background)] border border-[var(--vscode-input-border)] rounded-lg overflow-hidden shadow-sm">
                                                    <input 
                                                        placeholder="Given" 
                                                        value={step.given} 
                                                        onChange={(e) => handleUpdateStep(sIdx, stepIdx, 'given', e.target.value)}
                                                        className="bg-transparent text-xs border-r border-[var(--vscode-input-border)] focus:outline-none focus:bg-[var(--vscode-editor-background)] px-3 py-2 transition-colors"
                                                    />
                                                    <input 
                                                        placeholder="When" 
                                                        value={step.when} 
                                                        onChange={(e) => handleUpdateStep(sIdx, stepIdx, 'when', e.target.value)}
                                                        className="bg-transparent text-xs border-r border-[var(--vscode-input-border)] focus:outline-none focus:bg-[var(--vscode-editor-background)] px-3 py-2 transition-colors"
                                                    />
                                                    <input 
                                                        placeholder="Then" 
                                                        value={step.then} 
                                                        onChange={(e) => handleUpdateStep(sIdx, stepIdx, 'then', e.target.value)}
                                                        className="bg-transparent text-xs focus:outline-none focus:bg-[var(--vscode-editor-background)] px-3 py-2 transition-colors"
                                                    />
                                                </div>
                                                <button onClick={() => handleRemoveStep(sIdx, stepIdx)} className="text-[var(--vscode-descriptionForeground)] hover:text-red-400 p-1.5 rounded-md hover:bg-[var(--vscode-list-hoverBackground)] opacity-0 group-hover:opacity-100 transition-all">
                                                    <X size={14} />
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        </div>
                    ))}
                </div>

                <div className="pt-8 pb-4 flex justify-center border-t border-[var(--vscode-panel-border)]">
                     <button onClick={() => onDelete(node.id)} className="flex items-center gap-2 text-xs font-medium text-red-400 hover:text-red-500 opacity-70 hover:opacity-100 transition-all px-4 py-2.5 rounded-lg hover:bg-red-50/10">
                        <Trash2 size={14} />
                        Delete Requirement
                    </button>
                </div>

            </div>
        </div>
    );
}
