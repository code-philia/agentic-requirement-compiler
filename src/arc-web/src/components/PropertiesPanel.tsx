import { useState, useEffect } from 'react'
import { X, Plus, Trash2, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '../lib/utils'

interface Step {
  keyword: string
  content: string
}

interface Scenario {
  id: string
  name: string
  steps: Step[]
}

interface RequirementNode {
  id: string
  name: string
  description?: string
  dependencies?: string[]
  scenarios?: Scenario[]
  [key: string]: any
}

interface PropertiesPanelProps {
  node: RequirementNode
  onUpdate: (id: string, updates: any) => void
  onDelete: (id: string) => void
  onClose?: () => void
}

const CollapsibleSection = ({
  title,
  children,
  defaultOpen = true,
  onAdd
}: {
  title: string
  children: React.ReactNode
  defaultOpen?: boolean
  onAdd?: () => void
}) => {
  const [isOpen, setIsOpen] = useState(defaultOpen)

  return (
    <div className="border-b border-[var(--vscode-panel-border)] last:border-0">
      <div
        className="flex items-center justify-between px-4 py-2 text-xs font-semibold tracking-wide
        hover:bg-[var(--vscode-list-hoverBackground)]
        cursor-pointer select-none group transition-colors"
        onClick={() => setIsOpen(!isOpen)}
      >
        <div className="flex items-center gap-1 text-[var(--vscode-sideBarTitle-foreground)]">
          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          {title}
        </div>

        {onAdd && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onAdd()
            }}
            className="opacity-0 group-hover:opacity-100 p-1.5 rounded
            text-[var(--vscode-icon-foreground)]
            hover:bg-[var(--vscode-toolbar-hoverBackground)]
            transition-all"
          >
            <Plus size={14} />
          </button>
        )}
      </div>

      {isOpen && <div className="px-4 pb-4 pt-1">{children}</div>}
    </div>
  )
}

export default function PropertiesPanel({
  node,
  onUpdate,
  onDelete,
  onClose
}: PropertiesPanelProps) {
  const [formData, setFormData] = useState<RequirementNode>(node || {})
  const [panelWidth, setPanelWidth] = useState(360)
  const [isResizing, setIsResizing] = useState(false)

  const inputClasses =
    'w-full px-2 py-1.5 text-xs rounded-sm ' +
    'bg-[var(--vscode-input-background)] ' +
    'text-[var(--vscode-input-foreground)] ' +
    'border border-transparent ' +
    'hover:border-[var(--vscode-input-border)] ' +
    'focus:border-[var(--vscode-focusBorder)] ' +
    'focus:outline-none ' +
    'placeholder-[var(--vscode-input-placeholderForeground)] ' +
    'transition-colors'

  const labelClasses =
    'block mb-1 text-xs text-[var(--vscode-foreground)] opacity-80 font-medium'

  const iconButtonClasses =
    'p-1.5 rounded text-[var(--vscode-icon-foreground)] ' +
    'hover:bg-[var(--vscode-toolbar-hoverBackground)] ' +
    'active:scale-95 transition-all'

  const selectClasses =
    'w-full px-1 py-1 text-[10px] font-semibold ' +
    'bg-[var(--vscode-dropdown-background)] ' +
    'text-[var(--vscode-dropdown-foreground)] ' +
    'border border-transparent ' +
    'hover:border-[var(--vscode-input-border)] ' +
    'focus:border-[var(--vscode-focusBorder)] ' +
    'rounded-sm focus:outline-none cursor-pointer'

  useEffect(() => {
    setFormData(node || {})
  }, [node])

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isResizing) return

      const newWidth = document.body.clientWidth - e.clientX

      if (newWidth > 220 && newWidth < 800) {
        setPanelWidth(newWidth)
      }
    }

    const handleMouseUp = () => {
      setIsResizing(false)
      document.body.style.cursor = 'default'
    }

    if (isResizing) {
      window.addEventListener('mousemove', handleMouseMove)
      window.addEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = 'col-resize'
    }

    return () => {
      window.removeEventListener('mousemove', handleMouseMove)
      window.removeEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = 'default'
    }
  }, [isResizing])

  const handleChange = (field: string, value: any) => {
    const newData = { ...formData, [field]: value }
    setFormData(newData)
    onUpdate(node.id, newData)
  }

  const handleAddDependency = () => {
    const deps = formData.dependencies || []
    handleChange('dependencies', [...deps, ''])
  }

  const handleUpdateDependency = (i: number, value: string) => {
    const deps = [...(formData.dependencies || [])]
    deps[i] = value
    handleChange('dependencies', deps)
  }

  const handleRemoveDependency = (i: number) => {
    const deps = [...(formData.dependencies || [])]
    deps.splice(i, 1)
    handleChange('dependencies', deps)
  }

  const handleAddScenario = () => {
    const newScenario: Scenario = {
      id: `${node.id}:SCE-${(formData.scenarios || []).length + 1}`,
      name: 'New Scenario',
      steps: []
    }

    handleChange('scenarios', [...(formData.scenarios || []), newScenario])
  }

  const handleUpdateScenario = (index: number, field: keyof Scenario, value: any) => {
    const scenarios = [...(formData.scenarios || [])]
    scenarios[index] = { ...scenarios[index], [field]: value }
    handleChange('scenarios', scenarios)
  }

  const handleRemoveScenario = (index: number) => {
    const scenarios = [...(formData.scenarios || [])]
    scenarios.splice(index, 1)
    handleChange('scenarios', scenarios)
  }

  const handleAddStep = (scenarioIndex: number) => {
    const scenarios = [...(formData.scenarios || [])]
    scenarios[scenarioIndex].steps.push({ keyword: 'GIVEN', content: '' })
    handleChange('scenarios', scenarios)
  }

  const handleUpdateStep = (
    scenarioIndex: number,
    stepIndex: number,
    field: keyof Step,
    value: string
  ) => {
    const scenarios = [...(formData.scenarios || [])]
    scenarios[scenarioIndex].steps[stepIndex][field] = value
    handleChange('scenarios', scenarios)
  }

  const handleRemoveStep = (scenarioIndex: number, stepIndex: number) => {
    const scenarios = [...(formData.scenarios || [])]
    scenarios[scenarioIndex].steps.splice(stepIndex, 1)
    handleChange('scenarios', scenarios)
  }

  if (!node) return <div className="hidden" />

  return (
    <div
      className="flex flex-col h-full bg-[var(--vscode-sideBar-background)]
      border-l border-[var(--vscode-panel-border)]
      shadow-sm relative select-none"
      style={{ width: panelWidth }}
    >
      {/* Resize Handle */}

      <div
        onMouseDown={(e) => {
          e.preventDefault()
          setIsResizing(true)
        }}
        className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize
        hover:bg-[var(--vscode-focusBorder)] transition-colors"
      />

      {/* Header */}

      <div
        className="flex items-center justify-between px-4 py-3
        border-b border-[var(--vscode-panel-border)]
        bg-[var(--vscode-sideBarSectionHeader-background)]"
      >
        <span
          className="font-semibold text-xs uppercase
        text-[var(--vscode-sideBarSectionHeader-foreground)]"
        >
          PROPERTIES
        </span>

        <button onClick={onClose} className={iconButtonClasses}>
          <X size={16} />
        </button>
      </div>

      {/* Content */}

      <div className="flex-1 overflow-y-auto custom-scrollbar">
        {/* General */}

        <CollapsibleSection title="General">
          <div className="space-y-3">
            <div>
              <label className={labelClasses}>ID</label>
              <input
                value={formData.id || ''}
                disabled
                className={cn(inputClasses, 'opacity-60 cursor-not-allowed')}
              />
            </div>

            <div>
              <label className={labelClasses}>Name</label>

              <input
                value={formData.name || ''}
                onChange={(e) => handleChange('name', e.target.value)}
                className={inputClasses}
              />
            </div>

            <div>
              <label className={labelClasses}>Description</label>

              <textarea
                rows={4}
                value={formData.description || ''}
                onChange={(e) => handleChange('description', e.target.value)}
                className={cn(inputClasses, 'resize-y')}
              />

              <p className="text-[10px] opacity-60 mt-1">
                Supports Markdown
              </p>
            </div>
          </div>
        </CollapsibleSection>

        {/* Dependencies */}

        <CollapsibleSection title="Dependencies" onAdd={handleAddDependency}>
          <div className="space-y-2">
            {(formData.dependencies || []).length === 0 && (
              <div className="text-xs opacity-60 italic">No dependencies</div>
            )}

            {(formData.dependencies || []).map((dep, i) => (
              <div key={i} className="flex gap-2 items-center group">
                <input
                  value={dep}
                  onChange={(e) => handleUpdateDependency(i, e.target.value)}
                  className={inputClasses}
                  placeholder="REQ-ID"
                />

                <button
                  onClick={() => handleRemoveDependency(i)}
                  className="opacity-0 group-hover:opacity-100 text-red-400 p-1 rounded transition-opacity"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>
        </CollapsibleSection>

        {/* Scenarios */}

        <CollapsibleSection title="Scenarios" onAdd={handleAddScenario}>
          <div className="space-y-4">
            {(formData.scenarios || []).map((scenario, sIdx) => (
              <div
                key={sIdx}
                className="rounded-md
                bg-[var(--vscode-editor-background)]
                border border-[var(--vscode-panel-border)]
                shadow-sm hover:shadow transition-shadow"
              >
                {/* Scenario Header */}

                <div
                  className="px-3 py-2 border-b
                border-[var(--vscode-panel-border)]
                flex justify-between items-center
                bg-[var(--vscode-editorWidget-background)]"
                >
                  <input
                    value={scenario.name}
                    onChange={(e) =>
                      handleUpdateScenario(sIdx, 'name', e.target.value)
                    }
                    className="bg-transparent text-xs font-semibold focus:outline-none"
                  />

                  <button
                    onClick={() => handleRemoveScenario(sIdx)}
                    className="text-[var(--vscode-descriptionForeground)]
                    hover:text-red-400"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>

                <div className="p-3 space-y-3">
                  {(scenario.steps || []).map((step, stepIdx) => (
                    <div
                      key={stepIdx}
                      className="bg-[var(--vscode-editor-background)]
                      border border-[var(--vscode-panel-border)]
                      hover:border-[var(--vscode-focusBorder)]
                      transition-colors rounded p-2 group relative"
                    >
                      <button
                        onClick={() => handleRemoveStep(sIdx, stepIdx)}
                        className="absolute right-1 top-1 opacity-0 group-hover:opacity-100"
                      >
                        <X size={12} />
                      </button>

                      <div className="flex gap-2">
                        <div className="w-16 shrink-0">
                          <select
                            value={step.keyword}
                            onChange={(e) =>
                              handleUpdateStep(
                                sIdx,
                                stepIdx,
                                'keyword',
                                e.target.value
                              )
                            }
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
                          rows={2}
                          onChange={(e) =>
                            handleUpdateStep(
                              sIdx,
                              stepIdx,
                              'content',
                              e.target.value
                            )
                          }
                          className={inputClasses}
                        />
                      </div>
                    </div>
                  ))}

                  <button
                    onClick={() => handleAddStep(sIdx)}
                    className="text-xs text-[var(--vscode-textLink-foreground)]
                    hover:underline flex items-center gap-1"
                  >
                    <Plus size={12} />
                    Add Step
                  </button>
                </div>
              </div>
            ))}
          </div>
        </CollapsibleSection>

        {/* Footer */}
        <button
          onClick={() => onDelete(node.id)}
          className="w-full py-2 text-xs font-medium
          bg-[var(--vscode-button-background)]
          hover:bg-[var(--vscode-button-hoverBackground)]
          text-[var(--vscode-button-foreground)]
          rounded flex items-center justify-center gap-2
          transition-colors"
        >
          <Trash2 size={14} />
          Delete Requirement
        </button>
      </div>
    </div>
  )
}