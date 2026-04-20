import { useEffect, useMemo, useState, useCallback } from 'react';
import { ReactFlow, Controls, Background, useNodesState, useEdgesState, MiniMap, Handle, Position, MarkerType, Panel, type Connection } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

const NODE_WIDTH = 160;
const NODE_HEIGHT = 48; // Increased height to make it more oval-like
const X_OFFSET = 220;
const Y_GAP = 80;

function RequirementNode({ data, selected }: { data: any, selected?: boolean }) {
  const normalizeStatus = (status: string | undefined) => {
    if (!status) return 'idle';
    if (status === 'analyzing' || status === 'designed' || status === 'completed') {
      return status;
    }
    return 'idle';
  };
  const normalizedStatus = normalizeStatus(data.status);
  // Determine style based on status
  let statusClasses = 'bg-[var(--vscode-editor-background)] border-[var(--vscode-editorWidget-border)]';
  if (normalizedStatus === 'designed') {
      statusClasses = 'bg-[var(--vscode-charts-orange)] border-[var(--vscode-charts-orange)] text-[var(--vscode-editor-background)]';
  } else if (normalizedStatus === 'completed') {
      statusClasses = 'bg-[var(--vscode-charts-green)] border-[var(--vscode-charts-green)] text-[var(--vscode-editor-background)]';
  } else if (normalizedStatus === 'analyzing') {
      statusClasses = 'bg-[var(--vscode-charts-blue)] border-[var(--vscode-charts-blue)] text-[var(--vscode-editor-background)]';
  }
  
  // Extract name and ID
  const labelParts = data.label.split(':');
  const reqName = labelParts.length > 1 ? labelParts.slice(1).join(':').trim() : data.label;
  const reqId = labelParts[0];

  return (
    <div 
        className={`w-full h-full flex flex-col items-center justify-center border-2 shadow-sm px-4 py-1 text-xs text-center transition-all duration-300 relative group cursor-pointer
        ${statusClasses}
        ${selected ? 'ring-2 ring-[var(--vscode-focusBorder)] shadow-md scale-105' : ''}
        ${normalizedStatus === 'analyzing' ? 'animate-pulse' : ''}`}
        style={{ 
            color: normalizedStatus !== 'idle' ? 'var(--vscode-editor-background)' : 'var(--vscode-editor-foreground)',
            borderRadius: '50%', // Make it an ellipse
        }}
        title={`${reqId}: ${reqName}`} // Native tooltip for full name
    >
      <Handle type="target" position={Position.Left} id="t-left" className="opacity-0 group-hover:opacity-100 transition-opacity w-2.5 h-2.5 bg-[var(--vscode-editor-foreground)]" />
      <Handle type="target" position={Position.Right} id="t-right" className="opacity-0 group-hover:opacity-100 transition-opacity w-2.5 h-2.5 bg-[var(--vscode-editor-foreground)]" />
      <Handle type="source" position={Position.Left} id="s-left" className="opacity-0 group-hover:opacity-100 transition-opacity w-2.5 h-2.5 bg-[var(--vscode-editor-foreground)]" />
      <Handle type="source" position={Position.Right} id="s-right" className="opacity-0 group-hover:opacity-100 transition-opacity w-2.5 h-2.5 bg-[var(--vscode-editor-foreground)]" />
      
      <div className="w-full truncate font-bold text-[10px] leading-tight select-none pointer-events-none mb-0.5 opacity-70">{reqId}</div>
      <div className="w-full truncate font-bold text-[11px] leading-tight select-none pointer-events-none">{reqName}</div>
    </div>
  );
}

interface RequirementCanvasProps {
    rootNode: any;
    onNodeSelect: (nodeId: string) => void;
    selectedNodeId?: string;
    nodeStatuses?: Record<string, string>;
    onAddNode: (targetId: string, type: 'child' | 'sibling') => void;
    onDeleteNode: (id: string) => void;
    onUpdateNode: (id: string, updates: any) => void;
}

export default function RequirementCanvas({ rootNode, onNodeSelect, selectedNodeId, nodeStatuses, onAddNode, onDeleteNode, onUpdateNode }: RequirementCanvasProps) {
    const [nodes, setNodes, onNodesChange] = useNodesState<any>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<any>([]);
    const [showDependencies, setShowDependencies] = useState(false);
    const [contextMenu, setContextMenu] = useState<{ x: number, y: number, nodeId: string } | null>(null);

    const nodeTypes = useMemo(() => ({
        reqNode: RequirementNode
    }), []);
    const statusLegend = useMemo(() => ([
        { key: 'analyzing', label: 'Analyzing', color: 'var(--vscode-charts-blue)' },
        { key: 'designed', label: 'Designed', color: 'var(--vscode-charts-orange)' },
        { key: 'completed', label: 'Completed', color: 'var(--vscode-charts-green)' },
        { key: 'idle', label: 'Pending', color: 'var(--vscode-editorWidget-border)' },
    ]), []);
    const statusCounts = useMemo(() => {
        const counts: Record<string, number> = { analyzing: 0, designed: 0, completed: 0, idle: 0 };
        const walk = (node: any) => {
            if (!node) return;
            const rawStatus = nodeStatuses?.[node.id];
            if (rawStatus === 'analyzing' || rawStatus === 'designed' || rawStatus === 'completed') {
                counts[rawStatus] += 1;
            } else {
                counts.idle += 1;
            }
            (node.children || []).forEach((child: any) => walk(child));
        };
        walk(rootNode);
        return counts;
    }, [rootNode, nodeStatuses]);

    useEffect(() => {
        if (!rootNode) return;

        // 1. Calculate Layout
        const { nodes: layoutNodes, edges: layoutEdges } = getTreeLayout(rootNode);
        
        // 2. Add Dependency Edges if enabled
        if (showDependencies) {
            layoutNodes.forEach(node => {
                const original = node.data.originalNode;
                if (original.dependencies) {
                    original.dependencies.forEach((depId: string) => {
                        // Check if target exists in current map (might be filtered or not loaded, but usually full tree)
                        // Note: dependency targets might be anywhere in the tree.
                        // We rely on node IDs being unique.
                        layoutEdges.push({
                            id: `dep-${node.id}-${depId}`,
                            source: node.id,
                            target: depId,
                            sourceHandle: 's-right', // Reuse existing handles or define new ones if needed. 
                            // Ideally dependencies might flow differently, but for now reuse.
                            targetHandle: 't-left',
                            type: 'default',
                            animated: true,
                            style: { stroke: 'red', strokeDasharray: '5,5' },
                            markerEnd: {
                                type: MarkerType.ArrowClosed,
                                width: 20,
                                height: 20,
                                color: 'red',
                            },
                        });
                    });
                }
            });
        }

        // 3. Apply Selection and Status State
        const nodesWithState = layoutNodes.map(node => {
            const status = nodeStatuses?.[node.id];
            
            return {
                ...node,
                selected: node.id === selectedNodeId,
                data: {
                    ...node.data,
                    status: status
                },
                style: {
                    ...node.style,
                    borderColor: status === 'completed' ? 'var(--vscode-charts-green)' :
                               status === 'designed' ? 'var(--vscode-charts-orange)' :
                               status === 'analyzing' ? 'var(--vscode-charts-blue)' : 'var(--vscode-editorWidget-border)',
                }
            };
        });

        setNodes(nodesWithState);
        setEdges(layoutEdges);
    }, [rootNode, selectedNodeId, nodeStatuses, showDependencies, setNodes, setEdges]); 

    const onNodeClick = (_: any, node: any) => {
        setContextMenu(null);
        onNodeSelect(node.id);
    };

    const onNodeContextMenu = (event: React.MouseEvent, node: any) => {
        event.preventDefault();
        setContextMenu({
            x: event.clientX,
            y: event.clientY,
            nodeId: node.id
        });
    };

    const onPaneClick = () => {
        setContextMenu(null);
    };

    const onConnect = useCallback((params: Connection) => {
        // Handle creating dependency
        if (params.source && params.target) {
            // We need to update the source node's dependencies
            // Find the node in the tree to get current dependencies
            const findNode = (node: any): any => {
                if (node.id === params.source) return node;
                if (node.children) {
                    for (const child of node.children) {
                        const found = findNode(child);
                        if (found) return found;
                    }
                }
                return null;
            };
            
            const sourceNode = findNode(rootNode);
            if (sourceNode) {
                const currentDeps = sourceNode.dependencies || [];
                if (!currentDeps.includes(params.target)) {
                    onUpdateNode(params.source, {
                        dependencies: [...currentDeps, params.target]
                    });
                }
            }
        }
    }, [rootNode, onUpdateNode]);

    return (
        <div className="w-full h-full relative">
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                onNodeContextMenu={onNodeContextMenu}
                onPaneClick={onPaneClick}
                onConnect={onConnect}
                nodeTypes={nodeTypes}
                fitView
                attributionPosition="bottom-right"
                proOptions={{ hideAttribution: true }}
            >
                <Background color="#ccc" gap={20} />
                <Controls showInteractive={false} />
                <MiniMap 
                    nodeStrokeWidth={3} 
                    nodeColor={(node: any) => {
                        const status = node.data?.status;
                        if (status === 'completed') return 'var(--vscode-charts-green)';
                        if (status === 'designed') return 'var(--vscode-charts-orange)';
                        if (status === 'analyzing') return 'var(--vscode-charts-blue)';
                        return '#e0e0e0'; // Light gray for default nodes
                    }}
                />
                <Panel position="top-left" className="bg-[var(--vscode-editor-background)] p-2 rounded shadow-md border border-[var(--vscode-widget-border)]">
                    <label className="flex items-center space-x-2 text-xs cursor-pointer select-none text-[var(--vscode-foreground)]">
                        <input 
                            type="checkbox" 
                            checked={showDependencies} 
                            onChange={(e) => setShowDependencies(e.target.checked)}
                            className="rounded bg-[var(--vscode-checkbox-background)] border-[var(--vscode-checkbox-border)] text-[var(--vscode-checkbox-foreground)] focus:ring-[var(--vscode-focusBorder)]"
                        />
                        <span>Show Dependencies</span>
                    </label>
                    <div className="mt-2 border-t border-[var(--vscode-widget-border)] pt-2 space-y-1">
                        <div className="text-[10px] uppercase tracking-wide opacity-80">Status Legend</div>
                        {statusLegend.map(item => (
                            <div key={item.key} className="flex items-center justify-between gap-3 text-[11px]">
                                <div className="flex items-center gap-2">
                                    <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ backgroundColor: item.color }} />
                                    <span>{item.label}</span>
                                </div>
                                <span className="opacity-80">{statusCounts[item.key] ?? 0}</span>
                            </div>
                        ))}
                    </div>
                </Panel>
            </ReactFlow>

            {contextMenu && (
                <div 
                    className="fixed z-50 bg-[var(--vscode-menu-background)] text-[var(--vscode-menu-foreground)] border border-[var(--vscode-menu-border)] shadow-lg rounded-md py-1 min-w-[160px]"
                    style={{ top: contextMenu.y, left: contextMenu.x }}
                >
                    <button 
                        className="w-full text-left px-4 py-2 text-xs hover:bg-[var(--vscode-menu-selectionBackground)] hover:text-[var(--vscode-menu-selectionForeground)] focus:outline-none transition-colors"
                        onClick={() => {
                            onAddNode(contextMenu.nodeId, 'child');
                            setContextMenu(null);
                        }}
                    >
                        Add Child Node
                    </button>
                    <button 
                        className="w-full text-left px-4 py-2 text-xs hover:bg-[var(--vscode-menu-selectionBackground)] hover:text-[var(--vscode-menu-selectionForeground)] focus:outline-none transition-colors"
                        onClick={() => {
                            onAddNode(contextMenu.nodeId, 'sibling');
                            setContextMenu(null);
                        }}
                    >
                        Add Sibling Node
                    </button>
                    <div className="h-px bg-[var(--vscode-menu-separatorBackground)] my-1"></div>
                    <button 
                        className="w-full text-left px-4 py-2 text-xs hover:bg-[var(--vscode-menu-selectionBackground)] hover:text-[var(--vscode-menu-selectionForeground)] focus:outline-none transition-colors text-red-400"
                        onClick={() => {
                            onDeleteNode(contextMenu.nodeId);
                            setContextMenu(null);
                        }}
                    >
                        Delete Node
                    </button>
                </div>
            )}
        </div>
    );
}

// Layout Logic
const getTreeLayout = (root: any) => {
    const nodes: any[] = [];
    const edges: any[] = [];
    const nodeMap = new Map(); // To help with lookups if needed
    
    if (!root) return { nodes, edges, nodeMap };

    let currentY = 0;

    const traverse = (node: any, level: number) => {
        let nodeY;
        
        if (!node.children || node.children.length === 0) {
            nodeY = currentY;
            currentY += Y_GAP;
        } else {
            let firstChildY: number | null = null;
            let lastChildY: number | null = null;
            
            node.children.forEach((child: any) => {
                 const childY = traverse(child, level + 1);
                 if (firstChildY === null) firstChildY = childY;
                 lastChildY = childY;
            });
            
            nodeY = (firstChildY! + lastChildY!) / 2;
        }

        const flowNode = {
            id: node.id,
            data: { label: `${node.id}: ${node.name || ''}`, originalNode: node },
            type: 'reqNode',
            position: { x: level * X_OFFSET, y: nodeY },
            style: { width: NODE_WIDTH, height: NODE_HEIGHT }, 
        };
        
        nodes.push(flowNode);
        nodeMap.set(node.id, flowNode);

        if (node.children) {
            node.children.forEach((child: any) => {
                edges.push({
                    id: `e-${node.id}-${child.id}`,
                    source: node.id,
                    target: child.id,
                    sourceHandle: 's-right',
                    targetHandle: 't-left',
                    type: 'smoothstep',
                    animated: false,
                    markerEnd: {
                        type: MarkerType.ArrowClosed,
                        width: 20,
                        height: 20,
                        color: '#b1b1b7',
                    },
                    style: { stroke: '#b1b1b7' }
                });
            });
        }
        
        return nodeY;
    };

    traverse(root, 0);
    return { nodes, edges, nodeMap };
};
