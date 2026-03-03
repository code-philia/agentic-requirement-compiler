import { useState, useRef, useEffect } from 'react';
import { Terminal, Database, AlertCircle, CheckCircle2, Cpu } from 'lucide-react';

interface LogMessage {
  agent: string;
  text: string;
  type?: 'normal' | 'db-event' | 'error-event' | 'success-event' | 'system-event';
  timestamp?: string;
}

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

  const connectAndStart = () => {
    // Use ref to ensure latest path is used even inside event listener closure
    const projectPath = workspacePathRef.current || 'CURRENT_WORKSPACE'; 

    // If already connected and open, just send the start command
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ command: 'start', project_path: projectPath }));
      return;
    }

    // If connecting or closed, establish connection
    if (!wsRef.current || wsRef.current.readyState === WebSocket.CLOSED) {
        appendLog('System', 'Initializing ARC Backend connection...', 'system-event');
        const ws = new WebSocket('ws://127.0.0.1:8000/ws/compiler');
        wsRef.current = ws;

        ws.onopen = () => {
          appendLog('System', 'Connected to backend. Starting compilation process...', 'success-event');
          ws.send(JSON.stringify({ command: 'start', project_path: projectPath }));
        };

        ws.onmessage = (event) => {
          const data = JSON.parse(event.data);
          if (data.type === 'db_update') {
            appendLog(data.agent, `[DB Write] Table ${data.data.table}: +${data.data.items} items`, 'db-event');
          } else if (data.type === 'node_update' && data.status === 'completed') {
             appendLog(data.agent, data.message, 'success-event');
          } else if (data.type === 'error-event') {
             appendLog(data.agent, data.message, 'error-event');
          } else {
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
              connectAndStart();
          } else if (message.command === 'restartCompilation') {
              // Explicitly clear and restart
              setLogs([]); 
              // If connected, close first to ensure fresh start (optional, but good for "Restart")
              if (wsRef.current) {
                  wsRef.current.close();
                  wsRef.current = null;
              }
              // Allow small delay for cleanup then start
              setTimeout(() => connectAndStart(), 100);
          }
      };
      
      window.addEventListener('message', handleMessage);
      return () => window.removeEventListener('message', handleMessage);
  }, []); // Empty deps is fine now because we use ref for dynamic values
  
  // Auto-scroll to bottom
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);
  
  const getIcon = (type: string | undefined, agent: string) => {
      if (type === 'error-event') return <AlertCircle size={14} className="text-red-400 mt-0.5 shrink-0" />;
      if (type === 'db-event') return <Database size={14} className="text-blue-400 mt-0.5 shrink-0" />;
      if (type === 'success-event') return <CheckCircle2 size={14} className="text-green-400 mt-0.5 shrink-0" />;
      if (agent === 'System') return <Terminal size={14} className="text-gray-400 mt-0.5 shrink-0" />;
      return <Cpu size={14} className="text-purple-400 mt-0.5 shrink-0" />;
  };

  const getLogStyle = (type: string | undefined) => {
      switch(type) {
          case 'error-event': return 'bg-red-500/10 border-l-2 border-red-500 text-red-200';
          case 'db-event': return 'bg-blue-500/5 border-l-2 border-blue-500 text-blue-100';
          case 'success-event': return 'bg-green-500/10 border-l-2 border-green-500 text-green-100';
          case 'system-event': return 'text-gray-400 italic border-l-2 border-transparent pl-2';
          default: return 'text-[var(--vscode-editor-foreground)] border-l-2 border-gray-700/30 hover:bg-[var(--vscode-list-hoverBackground)]';
      }
  };

  return (
    <div className="flex flex-col h-full bg-[var(--vscode-panel-background)] text-[var(--vscode-panel-foreground)] p-0 relative">
      <div className="flex-1 overflow-y-auto bg-[var(--vscode-editor-background)] p-4 font-mono text-sm">
        {/* Top Spacer */}
        <div className="h-4"></div>

        {logs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-[var(--vscode-descriptionForeground)] opacity-60 space-y-4">
                <Terminal size={48} strokeWidth={1} />
                <div className="italic">Ready to compile. Use "ARC: Start Compilation" to begin.</div>
            </div>
        )}
        
        <div className="space-y-1 pb-16"> {/* Added pb-16 for bottom margin */}
            {logs.map((log, index) => (
            <div key={index} className={`flex gap-3 px-3 py-2 rounded-sm transition-colors ${getLogStyle(log.type)}`}>
                <div className="font-mono text-xs text-gray-500 shrink-0 w-[60px] pt-0.5">{log.timestamp}</div>
                <div className="flex gap-2 items-start flex-1">
                    {getIcon(log.type, log.agent)}
                    <div className="flex-1 break-words">
                        {log.agent !== 'System' && <span className="font-bold mr-2 opacity-80">[{log.agent}]</span>}
                        <span className={log.type === 'system-event' ? 'opacity-80' : ''}>{log.text}</span>
                    </div>
                </div>
            </div>
            ))}
            <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}
