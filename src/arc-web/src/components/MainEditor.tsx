import { useState, useEffect, useRef } from 'react';
import RequirementCanvas from './RequirementCanvas';
import PropertiesPanel from './PropertiesPanel';

export default function MainEditor() {
  const [rootNode, setRootNode] = useState<any>(null);
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [nodeStatuses, setNodeStatuses] = useState<Record<string, string>>({});
  const wsRef = useRef<WebSocket | null>(null);

  const updateNodeInTree = (tree: any, nodeId: string, updates: any): any => {
      if (!tree) return tree;
      if (tree.id === nodeId) {
          return { ...tree, ...updates };
      }
      if (!tree.children || tree.children.length === 0) {
          return tree;
      }
      return {
          ...tree,
          children: tree.children.map((child: any) => updateNodeInTree(child, nodeId, updates)),
      };
  };

  // Initialize and Listen for Messages (VS Code & WebSocket)
  useEffect(() => {
    // 1. VS Code Messages
    const handleMessage = (event: MessageEvent) => {
        const message = event.data;
        if (message.command === 'setNode') {
            setSelectedNode(message.node);
        } else if (message.command === 'updateProject') {
            setRootNode(message.data);
            
            // Sync selected node with fresh data
            setSelectedNode((currentSelected: any) => {
                if (currentSelected) {
                    const findNode = (node: any): any => {
                        if (node.id === currentSelected.id) return node;
                        if (node.children) {
                            for (const child of node.children) {
                                const found = findNode(child);
                                if (found) return found;
                            }
                        }
                        return null;
                    };
                    const freshNode = findNode(message.data);
                    return freshNode || currentSelected;
                }
                return currentSelected;
            });
        } else if (message.command === 'updateStatus') {
            // Update statuses from persistence layer
            setNodeStatuses(message.status);
        }
    };
    window.addEventListener('message', handleMessage);

    // 2. WebSocket Connection for Real-time Status Updates
    const connectWs = () => {
        const ws = new WebSocket('ws://127.0.0.1:8000/ws/compiler');
        wsRef.current = ws;

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'node_update') {
                    setNodeStatuses(prev => ({
                        ...prev,
                        [data.nodeId]: data.status
                    }));
                } else if (data.command === 'start') {
                    // Reset statuses on start (if we received a start signal, though usually sent BY us)
                     setNodeStatuses({});
                }
            } catch (e) {
                console.error("Failed to parse WS message", e);
            }
        };

        ws.onerror = (e) => console.log("WS Error in MainEditor", e);
        ws.onclose = () => {
             // Optional: reconnect logic
        };
    };

    connectWs();
    
    // Request initial project data once on mount
    if (window.vscode) {
        window.vscode.postMessage({ command: 'requestProject' });
    }

    return () => {
        window.removeEventListener('message', handleMessage);
        if (wsRef.current) wsRef.current.close();
    };
  }, []); // Run once on mount

  const handleNodeSelect = (nodeId: string) => {
      // Find node in tree
      const findNode = (node: any): any => {
          if (node.id === nodeId) return node;
          if (node.children) {
              for (const child of node.children) {
                  const found = findNode(child);
                  if (found) return found;
              }
          }
          return null;
      };
      
      if (rootNode) {
          const node = findNode(rootNode);
          setSelectedNode(node);
      }
  };

  const handleAddNode = (targetId: string, type: 'child' | 'sibling') => {
      if (window.vscode) {
          window.vscode.postMessage({
              command: 'addNode',
              targetId: targetId,
              type: type
          });
      }
  };

  const handleUpdateNode = (id: string, updates: any) => {
      // Optimistic local update: keep document panel and canvas synced instantly
      setRootNode((prev: any) => updateNodeInTree(prev, id, updates));
      if (window.vscode) {
          window.vscode.postMessage({
              command: 'updateNode',
              nodeId: id,
              updates: updates
          });
      }
      setSelectedNode((prev: any) => (prev?.id === id ? { ...prev, ...updates } : prev));
  };

  const handleDeleteNode = (id: string) => {
      if (window.vscode) {
          window.vscode.postMessage({
              command: 'deleteNode',
              nodeId: id
          });
      }
  };

  const handleCanvasDeselect = () => {
      setSelectedNode(null);
  };

  return (
    <div className="h-screen w-full flex bg-[var(--vscode-editor-background)] text-[var(--vscode-editor-foreground)] overflow-hidden">
      {/* Canvas Area */}
      <div className="flex-1 relative h-full w-full">
        {rootNode ? (
             <RequirementCanvas 
                rootNode={rootNode} 
                onNodeSelect={handleNodeSelect} 
                onCanvasDeselect={handleCanvasDeselect}
                selectedNodeId={selectedNode?.id}
                nodeStatuses={nodeStatuses}
                onAddNode={handleAddNode}
                onDeleteNode={handleDeleteNode}
                onUpdateNode={handleUpdateNode}
             />
        ) : (
            <div className="flex items-center justify-center h-full opacity-50">
                Loading Requirement Graph...
            </div>
        )}
      </div>

      {/* Right Sidebar: Properties Panel */}
      <PropertiesPanel 
        node={selectedNode} 
        onUpdate={handleUpdateNode} 
        onDelete={handleDeleteNode}
        onClose={() => setSelectedNode(null)}
        onClearSelection={() => setSelectedNode(null)}
      />
    </div>
  );
}
