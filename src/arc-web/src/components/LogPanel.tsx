import { useState, useRef, useEffect } from 'react';
import { Terminal, ChevronRight, ChevronDown } from 'lucide-react';

interface LogMessage {
  agent: string;
  text: string;
  type?: 'normal' | 'db-event' | 'error-event' | 'success-event' | 'system-event';
  timestamp?: string;
}

const LogEntry = ({ log, getLogStyle }: { log: LogMessage, getLogStyle: any }) => {
    const [expanded, setExpanded] = useState(false);
    const MAX_LENGTH = 150;
    const isLong = log.text.length > MAX_LENGTH || log.text.includes('\n');
    
    // Truncate text logic: show first line or first MAX_LENGTH chars
    let displayText = log.text;
    if (!expanded && isLong) {
        const firstLine = log.text.split('\n')[0];
        displayText = firstLine.length > MAX_LENGTH ? firstLine.substring(0, MAX_LENGTH) + '...' : firstLine + (log.text.includes('\n') ? '...' : '');
    }

    return (
        <div 
            className={`flex items-start font-mono text-sm leading-snug cursor-pointer hover:bg-[var(--vscode-list-hoverBackground)] ${getLogStyle(log.type)}`}
            onClick={() => isLong && setExpanded(!expanded)}
        >
            <div className="shrink-0 flex items-center w-[16px] pt-[2px]">
                {isLong && (
                    <span className="opacity-70">
                        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </span>
                )}
            </div>
            <div className="flex-1 min-w-0 break-words whitespace-pre-wrap py-[2px]">
                <span className="text-[var(--vscode-terminal-ansiBrightBlack)] mr-2">[{log.timestamp}]</span>
                {log.agent !== 'System' && <span className="font-bold mr-2">[{log.agent}]</span>}
                <span className={log.type === 'system-event' ? 'opacity-80' : ''}>{displayText}</span>
            </div>
        </div>
    );
};

export default function LogPanel() {
  const [logs, setLogs] = useState<LogMessage[]>([]);
  // workspacePath state removed as we rely on ref for the closure fix and don't need to render it
  const workspacePathRef = useRef<string>(''); // Ref to hold latest path
  const wsRef = useRef<WebSocket | null>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);

  const appendLog = (agent: string, text: string, type: 'normal' | 'db-event' | 'error-event' | 'success-event' | 'system-event' = 'normal') => {
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
    setLogs(prev => [...prev, { agent, text, type, timestamp }]);
  };

  const connectAndStart = (isRestart = false) => {
    // Use ref to ensure latest path is used even inside event listener closure
    const projectPath = workspacePathRef.current || 'CURRENT_WORKSPACE'; 
    const requirementPath = projectPath + '/requirements/requirements.yaml';
    const cmd = isRestart ? 'restart' : 'start';

    // If already connected and open, just send the start command
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ command: cmd, projectPath: projectPath, requirementPath: requirementPath }));
      return;
    }

    // If connecting or closed, establish connection
    if (!wsRef.current || wsRef.current.readyState === WebSocket.CLOSED) {
        appendLog('System', 'Initializing ARC Backend connection...', 'system-event');
        const ws = new WebSocket('ws://127.0.0.1:8000/ws/compiler');
        wsRef.current = ws;

        ws.onopen = () => {
          appendLog('System', 'Connected to backend. Starting compilation process...', 'success-event');
          ws.send(JSON.stringify({ command: cmd, projectPath: projectPath, requirementPath: requirementPath }));
        };

        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.type === 'clear-logs') {
              setLogs([]);
          } else if (data.type === 'db_update') {
            appendLog(data.agent, `[DB Write] Table ${data.data.table}: +${data.data.items} items`, 'db-event');
          } else if (data.type === 'node_update' && data.status === 'completed') {
             appendLog(data.agent, data.message || `Node ${data.nodeId} completed.`, 'success-event');
          } else if (data.type === 'error-event') {
             appendLog(data.agent, data.message, 'error-event');
          } else if (data.type === 'log') {
            appendLog(data.agent, data.message, 'normal');
          }
        };

        ws.onerror = () => {
          appendLog('System', 'WebSocket connection failed. Please ensure the backend server is running.', 'error-event');
        };

        ws.onclose = () => {
          appendLog('System', 'Connection to backend closed.', 'system-event');
          wsRef.current = null;
        };
    }
  };

  // Listen for messages from the extension
  useEffect(() => {
      const handleMessage = (event: MessageEvent) => {
          const message = event.data;
          if (message.command === 'setContext') {
              workspacePathRef.current = message.workspacePath; // Update ref immediately
          } else if (message.command === 'startCompilation') {
              // Just ensure connected and start
              connectAndStart(false);
          } else if (message.command === 'restartCompilation') {
              // Explicitly clear and restart
              setLogs([]); 
              // If connected, close first to ensure fresh start (optional, but good for "Restart")
              if (wsRef.current) {
                  wsRef.current.close();
                  wsRef.current = null;
              }
              // Allow small delay for cleanup then start
              setTimeout(() => connectAndStart(true), 100);
          }
      };
      
      window.addEventListener('message', handleMessage);
      return () => window.removeEventListener('message', handleMessage);
  }, []); // Empty deps is fine now because we use ref for dynamic values
  
  // Auto-scroll to bottom
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);
  
  const getLogStyle = (type: string | undefined) => {
      switch(type) {
          case 'error-event': return 'text-[var(--vscode-terminal-ansiRed)]';
          case 'db-event': return 'text-[var(--vscode-terminal-ansiBlue)]';
          case 'success-event': return 'text-[var(--vscode-terminal-ansiGreen)]';
          case 'system-event': return 'text-[var(--vscode-terminal-ansiBrightBlack)] italic';
          default: return 'text-[var(--vscode-terminal-foreground)]';
      }
  };

  return (
    <div className="flex flex-col h-full bg-[var(--vscode-panel-background)] text-[var(--vscode-terminal-foreground)] p-0 relative">
      <div className="flex-1 overflow-y-auto bg-[var(--vscode-terminal-background)] p-4 font-mono text-sm">
        {/* Top Spacer */}
        <div className="h-4"></div>

        {logs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-[var(--vscode-descriptionForeground)] opacity-60 space-y-4">
                <Terminal size={48} strokeWidth={1} />
                <div className="italic">Ready to compile. Use "ARC: Start Compilation" to begin.</div>
            </div>
        )}
        
        <div className="pb-16"> {/* Removed space-y-1 for tighter native terminal look */}
            {logs.map((log, index) => (
                <LogEntry key={index} log={log} getLogStyle={getLogStyle} />
            ))}
            <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}
