import { useEffect, useRef } from 'react';

interface ContextMenuProps {
  x: number;
  y: number;
  onClose: () => void;
  onAddChild: () => void;
  onAddSibling: () => void;
}

export default function ContextMenu({ x, y, onClose, onAddChild, onAddSibling }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="fixed z-50 bg-[var(--vscode-menu-background)] border border-[var(--vscode-menu-border)] shadow-lg rounded py-1 min-w-[150px] flex flex-col"
      style={{ top: y, left: x }}
    >
      <button
        onClick={onAddChild}
        className="text-left px-3 py-1.5 text-xs bg-transparent border-none cursor-pointer text-[var(--vscode-menu-foreground)] hover:bg-[var(--vscode-menu-selectionBackground)] hover:text-[var(--vscode-menu-selectionForeground)]"
      >
        Add Child Node
      </button>
      <button
        onClick={onAddSibling}
        className="text-left px-3 py-1.5 text-xs bg-transparent border-none cursor-pointer text-[var(--vscode-menu-foreground)] hover:bg-[var(--vscode-menu-selectionBackground)] hover:text-[var(--vscode-menu-selectionForeground)]"
      >
        Add Sibling Node
      </button>
    </div>
  );
}
