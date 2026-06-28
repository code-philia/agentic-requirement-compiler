import { useState, useRef, useEffect } from 'react';
import { Terminal } from 'lucide-react';

interface LogMessage {
  agent: string;
  text: string;
  level?: 'info' | 'success' | 'warn' | 'error' | 'system';
  type?: 'normal' | 'db-event' | 'error-event' | 'success-event' | 'system-event';
  timestamp?: string;
}

const LogEntry = ({ log, getLogStyle }: { log: LogMessage, getLogStyle: any }) => {
  return (
    <div className={`font-mono text-[12px] leading-5 px-1 ${getLogStyle(log.type, log.level)}`}>
      <span className="text-[var(--vscode-terminal-ansiBrightBlack)]">[{log.timestamp}]</span>
      <span className="ml-2 opacity-90">[{log.agent}]</span>
      <span className="ml-2 whitespace-pre-wrap break-words">{log.text}</span>
    </div>
  );
};

export default function LogPanel() {
  const [logs, setLogs] = useState<LogMessage[]>([]);
  // workspacePath state removed as we rely on ref for the closure fix and don't need to render it
  const workspacePathRef = useRef<string>(''); // Ref to hold latest path
  const wsRef = useRef<WebSocket | null>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);

  const appendLog = (
    agent: string,
    text: string,
    type: 'normal' | 'db-event' | 'error-event' | 'success-event' | 'system-event' = 'normal',
    level: 'info' | 'success' | 'warn' | 'error' | 'system' = 'info'
  ) => {
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
    setLogs(prev => {
      const last = prev[prev.length - 1];
      if (last && last.agent === agent && last.text === text && last.type === type) {
        return prev;
      }
      const next = [...prev, { agent, text, type, level, timestamp }];
      if (next.length > 500) {
        return next.slice(next.length - 500);
      }
      return next;
    });
  };

  const isDebugLog = (text: string) => text.startsWith('[DEBUG]');
  const compactOneLine = (text: string) => {
    const single = text.replace(/\s+/g, ' ').trim();
    return single.length > 180 ? `${single.slice(0, 180)}...` : single;
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
        appendLog('System', 'Initializing ARC backend connection...', 'system-event', 'system');
        const ws = new WebSocket('ws://127.0.0.1:8000/ws/compiler');
        wsRef.current = ws;

        ws.onopen = () => {
          appendLog('System', 'Connected. Starting compilation...', 'success-event', 'success');
          ws.send(JSON.stringify({ command: cmd, projectPath: projectPath, requirementPath: requirementPath }));
        };

        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.type === 'clear-logs') {
              setLogs([]);
          } else if (data.type === 'db_update') {
            appendLog(data.agent || 'DB', `DB ${data.data.table} +${data.data.items}`, 'db-event', 'info');
          } else if (data.type === 'node_update') {
             const status = data.status || 'updated';
             const nodeLabel = data.nodeId ? `Node ${data.nodeId}` : 'Node';
             const msg = data.message || `${nodeLabel} -> ${status}`;
             const type = status === 'completed' ? 'success-event' : status === 'error' ? 'error-event' : 'system-event';
             const level = status === 'completed' ? 'success' : status === 'error' ? 'error' : 'system';
             appendLog('Workflow', compactOneLine(msg), type, level);
          } else if (data.type === 'error-event') {
             appendLog(data.agent || 'System', compactOneLine(data.message || 'Unknown error'), 'error-event', 'error');
          } else if (data.type === 'log') {
            const raw = String(data.message || '');
            if (!isDebugLog(raw)) {
              appendLog(data.agent || 'System', compactOneLine(raw), 'normal', 'info');
            }
          }
        };

        ws.onerror = () => {
          appendLog('System', 'WebSocket failed. Ensure backend is running.', 'error-event', 'error');
        };

        ws.onclose = () => {
          appendLog('System', 'Connection closed.', 'system-event', 'system');
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
  
  const getLogStyle = (type: string | undefined, level?: string) => {
      if (level === 'error') return 'text-[var(--vscode-terminal-ansiRed)]';
      if (level === 'success') return 'text-[var(--vscode-terminal-ansiGreen)]';
      if (level === 'warn') return 'text-[var(--vscode-terminal-ansiYellow)]';
      switch(type) {
          case 'error-event': return 'text-[var(--vscode-terminal-ansiRed)]';
          case 'db-event': return 'text-[var(--vscode-terminal-ansiBlue)]';
          case 'success-event': return 'text-[var(--vscode-terminal-ansiGreen)]';
          case 'system-event': return 'text-[var(--vscode-terminal-ansiBrightBlack)]';
          default: return 'text-[var(--vscode-terminal-foreground)]';
      }
  };

  return (
    <div className="flex flex-col h-full bg-[var(--vscode-panel-background)] text-[var(--vscode-terminal-foreground)] p-0 relative">
      <div className="flex-1 overflow-y-auto bg-[var(--vscode-terminal-background)] p-2 font-mono text-sm">

        {logs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-[var(--vscode-descriptionForeground)] opacity-60 space-y-4">
                <Terminal size={48} strokeWidth={1} />
                <div className="italic">Ready. Use `ARC: Start Compilation`.</div>
            </div>
        )}
        
        <div className="pb-8">
            {logs.map((log, index) => (
                <LogEntry key={index} log={log} getLogStyle={getLogStyle} />
            ))}
            <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}
