import { useEffect, useMemo } from 'react';
import { ReactFlow, Controls, Background, useNodesState, useEdgesState, MiniMap, Handle, Position, MarkerType } from '@xyflow/react';
import '@xyflow/react/dist/style.css';

const NODE_WIDTH = 220;
const X_OFFSET = 350;
const Y_GAP = 100;

function RequirementNode({ data, selected }: { data: any, selected?: boolean }) {
  // Determine style based on status
  let statusClasses = 'bg-white border-gray-400';
  if (data.status === 'designed') {
      statusClasses = 'bg-orange-100 border-orange-400 shadow-orange-100';
  } else if (data.status === 'completed') {
      statusClasses = 'bg-green-100 border-green-400 shadow-green-100';
  } else if (data.status === 'analyzing') {
      statusClasses = 'bg-blue-50 border-blue-300';
  }

  return (
    <div 
        className={`w-full h-full flex items-center justify-center border rounded shadow-sm px-2 text-xs text-center text-gray-800 transition-all duration-500
        ${statusClasses}
        ${selected ? 'ring-2 ring-blue-500 shadow-md' : ''}`}
    >
      <Handle type="target" position={Position.Left} id="t-left" className="opacity-0" />
      <Handle type="target" position={Position.Right} id="t-right" className="opacity-0" />
      <Handle type="source" position={Position.Left} id="s-left" className="opacity-0" />
      <Handle type="source" position={Position.Right} id="s-right" className="opacity-0" />
      <div className="w-full truncate font-medium">{data.label}</div>
    </div>
  );
}

export default function RequirementCanvas({ rootNode, onNodeSelect, selectedNodeId, nodeStatuses }: { rootNode: any, onNodeSelect: (nodeId: string) => void, selectedNodeId?: string, nodeStatuses?: Record<string, string> }) {
    const [nodes, setNodes, onNodesChange] = useNodesState<any>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<any>([]);

    const nodeTypes = useMemo(() => ({
        reqNode: RequirementNode
    }), []);

    useEffect(() => {
        if (!rootNode) return;

        // 1. Calculate Layout
        const { nodes: layoutNodes, edges: layoutEdges } = getTreeLayout(rootNode);
        
        // 2. Apply Selection and Status State
        // IMPORTANT: We need to recreate the nodes array to force React Flow to re-render the custom nodes with new data
        const nodesWithState = layoutNodes.map(node => {
            const status = nodeStatuses?.[node.id];
            
            return {
                ...node,
                selected: node.id === selectedNodeId,
                data: {
                    ...node.data,
                    status: status // Inject status from prop
                },
                // Use style override as a backup or for stronger visual cue if needed
                style: {
                    ...node.style,
                    backgroundColor: status === 'completed' ? '#dcfce7' : 
                                   status === 'designed' ? '#ffedd5' : 
                                   status === 'analyzing' ? '#eff6ff' : '#ffffff',
                    borderColor: status === 'completed' ? '#4ade80' :
                               status === 'designed' ? '#fb923c' :
                               status === 'analyzing' ? '#93c5fd' : '#9ca3af',
                }
            };
        });

        setNodes(nodesWithState);
        setEdges(layoutEdges);
    }, [rootNode, selectedNodeId, nodeStatuses, setNodes, setEdges]); 

    const onNodeClick = (_: any, node: any) => {
        onNodeSelect(node.id);
    };

    return (
        <div className="w-full h-full bg-gray-50">
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                nodeTypes={nodeTypes}
                fitView
                attributionPosition="bottom-right"
            >
                <Background color="#ccc" gap={20} />
                <Controls />
                <MiniMap />
            </ReactFlow>
        </div>
    );
}

// Layout Logic (Simplified from old_tool)
const getTreeLayout = (root: any) => {
    const nodes: any[] = [];
    const edges: any[] = [];
    
    if (!root) return { nodes, edges };

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

        nodes.push({
            id: node.id,
            data: { label: `${node.id}: ${node.name || ''}`, originalNode: node },
            type: 'reqNode',
            position: { x: level * X_OFFSET, y: nodeY },
            style: { width: NODE_WIDTH, height: 40 }, // ReactFlow style override
        });

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
    return { nodes, edges };
};
