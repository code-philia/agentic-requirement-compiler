import { useState, useEffect } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';
import { cn } from '../lib/utils';
import ContextMenu from './ContextMenu';

interface RequirementNode {
  id: string;
  name: string;
  children?: RequirementNode[];
  [key: string]: any;
}

interface TreeNodeProps {
  node: RequirementNode;
  level?: number;
  onSelect?: (node: RequirementNode) => void;
  onContextMenu?: (e: React.MouseEvent, node: RequirementNode) => void;
}

const TreeNode = ({ node, level = 0, onSelect, onContextMenu }: TreeNodeProps) => {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = node.children && node.children.length > 0;

  const handleSelect = (e: React.MouseEvent) => {
    e.stopPropagation();
    onSelect?.(node);
  };

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    onContextMenu?.(e, node);
  };

  const toggleExpand = (e: React.MouseEvent) => {
    e.stopPropagation();
    setExpanded(!expanded);
  };

  return (
    <div className="select-none">
      <div 
        className={cn(
            "flex items-center py-0.5 cursor-pointer hover:bg-[var(--vscode-list-hoverBackground)] text-[var(--vscode-foreground)]",
            "transition-colors duration-100"
        )}
        style={{ paddingLeft: `${level * 16}px` }}
        onClick={handleSelect}
        onContextMenu={handleContextMenu}
      >
        <div 
            className={cn(
                "flex items-center justify-center w-5 h-5 mr-0.5 rounded-sm hover:bg-[var(--vscode-list-hoverForeground)]/10",
                !hasChildren && "invisible"
            )}
            onClick={toggleExpand}
        >
            {expanded ? <ChevronDown size={14} className="opacity-80" /> : <ChevronRight size={14} className="opacity-80" />}
        </div>
        
        <span className="text-[13px] leading-5 truncate opacity-90">{node.name || node.id}</span>
      </div>
      
      {hasChildren && expanded && (
        <div>
          {node.children!.map((child: any) => (
            <TreeNode key={child.id} node={child} level={level + 1} onSelect={onSelect} onContextMenu={onContextMenu} />
          ))}
        </div>
      )}
    </div>
  );
};

export default function Sidebar() {
  const [data, setData] = useState<RequirementNode | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number, y: number, node: RequirementNode } | null>(null);

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
        const message = event.data;
        if (message.command === 'updateData') {
            setData(message.data);
        }
    };
    window.addEventListener('message', handleMessage);
    
    // Request initial data
    if (window.vscode) {
        window.vscode.postMessage({ command: 'refresh' });
    }

    return () => window.removeEventListener('message', handleMessage);
  }, []);

  const handleSelect = (node: RequirementNode) => {
    if (window.vscode) {
        window.vscode.postMessage({
            command: 'openMainEditor',
            nodeId: node.id
        });
    }
  };

  const handleContextMenu = (e: React.MouseEvent, node: RequirementNode) => {
    setContextMenu({ x: e.clientX, y: e.clientY, node });
  };

  const handleAddChild = () => {
    if (contextMenu && window.vscode) {
        window.vscode.postMessage({
            command: 'addNode',
            targetId: contextMenu.node.id,
            type: 'child'
        });
    }
    setContextMenu(null);
  };

  const handleAddSibling = () => {
    if (contextMenu && window.vscode) {
        window.vscode.postMessage({
            command: 'addNode',
            targetId: contextMenu.node.id,
            type: 'sibling'
        });
    }
    setContextMenu(null);
  };

  if (!data) {
      return <div className="p-4 text-sm opacity-50">Loading...</div>;
  }

  return (
    <div 
        className="h-full w-full overflow-y-auto bg-[var(--vscode-sideBar-background)] text-[var(--vscode-sideBar-foreground)] pt-2" 
        onClick={() => setContextMenu(null)}
    >
      <TreeNode node={data} onSelect={handleSelect} onContextMenu={handleContextMenu} />
      
      {contextMenu && (
        <ContextMenu 
            x={contextMenu.x} 
            y={contextMenu.y} 
            onClose={() => setContextMenu(null)}
            onAddChild={handleAddChild}
            onAddSibling={handleAddSibling}
        />
      )}
    </div>
  );
}
