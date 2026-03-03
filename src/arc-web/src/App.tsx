import { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import LogPanel from './components/LogPanel';
import MainEditor from './components/MainEditor';
import './App.css';

// Type declaration for VS Code API
declare global {
  interface Window {
    vscode: any;
    acquireVsCodeApi: () => any;
  }
}

// Initialize VS Code API
if (typeof window.acquireVsCodeApi === 'function') {
  window.vscode = window.acquireVsCodeApi();
}

function App() {
  const [view, setView] = useState('sidebar');

  useEffect(() => {
    // 1. Check URL parameters
    const params = new URLSearchParams(window.location.search);
    const viewParam = params.get('view');
    if (viewParam) {
      setView(viewParam);
    }

    // 2. Listen for messages from VS Code extension
    const handleMessage = (event: MessageEvent) => {
      const message = event.data;
      if (message.command === 'setView') {
        setView(message.view);
      }
    };

    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, []);

  if (view === 'sidebar') return <Sidebar />;
  if (view === 'logs') return <LogPanel />;
  if (view === 'main') return <MainEditor />;
  
  return (
    <div className="p-4 text-center">
      <h3>Unknown View: {view}</h3>
      <p>Please specify ?view=sidebar|logs|main</p>
    </div>
  );
}

export default App;
