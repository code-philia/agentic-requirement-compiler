import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, Database, Server, Settings2, Sparkles } from 'lucide-react';

type ArcTechStack = {
  backend: 'nodejs' | 'python_flask';
  frontend: 'react';
  database: 'sqlite';
};

type ArcStackProfile = {
  frontend: {
    framework: string;
    language: string;
    styling: string;
    http: string;
    testing: string;
  };
  backend: {
    runtime: string;
    framework: string;
    database: string;
    testing: string[];
  };
};

type SettingsInitData = {
  envKeys: string[];
  envValues: Record<string, string>;
  stack: ArcTechStack;
};

export default function SettingsPanel() {
  const [envKeys, setEnvKeys] = useState<string[]>([]);
  const [envValues, setEnvValues] = useState<Record<string, string>>({});
  const [stack, setStack] = useState<ArcTechStack>({
    backend: 'nodejs',
    frontend: 'react',
    database: 'sqlite',
  });
  const [status, setStatus] = useState<string>('');

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      const message = event.data;
      if (message?.command === 'arcSettingsInit') {
        const data = message.data as SettingsInitData;
        setEnvKeys(data.envKeys || []);
        setEnvValues(data.envValues || {});
        setStack(
          data.stack || {
            backend: 'nodejs',
            frontend: 'react',
            database: 'sqlite',
          },
        );
      }
    };
    window.addEventListener('message', handleMessage);
    window.vscode?.postMessage({ command: 'requestArcSettingsInit' });
    return () => window.removeEventListener('message', handleMessage);
  }, []);

  const filledCount = useMemo(
    () => envKeys.filter(key => (envValues[key] || '').trim().length > 0).length,
    [envKeys, envValues],
  );
  const stackProfile = useMemo<ArcStackProfile>(() => ({
    frontend: {
      framework: 'React 18+ (Vite)',
      language: 'JavaScript (ES6+)',
      styling: 'Tailwind CSS v4',
      http: 'Axios (Must use Interceptors for global error handling)',
      testing: 'None in frontend directory (Verified via backend E2E)',
    },
    backend: {
      runtime: 'Node.js (LTS)',
      framework: 'Express.js',
      database: 'SQLite3 (sqlite3 driver, file-based)',
      testing: [
        'Vitest: Unit and Integration testing',
        'Supertest: API route testing with Vitest',
        'Playwright: E2E testing in backend/test-e2e',
      ],
    },
  }), []);

  const onSave = () => {
    setStatus('Saving...');
    window.vscode?.postMessage({
      command: 'saveArcSettings',
      payload: {
        envValues,
        stack,
        profile: stackProfile,
      },
    });
  };

  const onCancel = () => {
    window.vscode?.postMessage({ command: 'cancelArcSettings' });
  };

  return (
    <div className="h-full w-full overflow-auto bg-[radial-gradient(circle_at_top_right,var(--vscode-textBlockQuote-background),transparent_45%)]">
      <div className="mx-auto max-w-4xl p-6">
        <div className="mb-5 rounded-xl border border-[var(--vscode-panel-border)] bg-[var(--vscode-editor-background)]/90 p-5 shadow-[0_8px_30px_rgba(0,0,0,0.12)]">
          <div className="flex items-center gap-3">
            <div className="rounded-lg bg-[var(--vscode-button-background)]/15 p-2">
              <Settings2 size={18} className="text-[var(--vscode-button-background)]" />
            </div>
            <div>
              <h1 className="text-lg font-semibold leading-6">ARC Settings</h1>
              <p className="text-xs opacity-80">
                Configure ARC Agent environment variables and target project metadata metadata
              </p>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-3 text-xs opacity-80">
            <span className="rounded-md border border-[var(--vscode-panel-border)] px-2 py-1">
              Env Fields: {filledCount}/{envKeys.length}
            </span>
            <span className="rounded-md border border-[var(--vscode-panel-border)] px-2 py-1">
              Metadata: .arc/metadata.md
            </span>
          </div>
        </div>

        <div className="grid gap-5 md:grid-cols-2">
          <section className="rounded-xl border border-[var(--vscode-panel-border)] bg-[var(--vscode-editor-background)]/90 p-4">
            <div className="mb-3 flex items-center gap-2">
              <Sparkles size={16} className="text-[var(--vscode-terminal-ansiBlue)]" />
              <h2 className="text-sm font-semibold">ARC Agent .env</h2>
            </div>
            <div className="space-y-3">
              {envKeys.map(key => (
                <div key={key} className="grid gap-1">
                  <label className="text-xs opacity-80">{key}</label>
                  <input
                    className="rounded-md border border-[var(--vscode-input-border)] bg-[var(--vscode-input-background)] px-3 py-2 text-sm text-[var(--vscode-input-foreground)] outline-none focus:border-[var(--vscode-focusBorder)]"
                    type="text"
                    value={envValues[key] ?? ''}
                    onChange={e =>
                      setEnvValues(prev => ({
                        ...prev,
                        [key]: e.target.value,
                      }))
                    }
                    placeholder={`Enter ${key}`}
                  />
                </div>
              ))}
            </div>
          </section>

          <section className="rounded-xl border border-[var(--vscode-panel-border)] bg-[var(--vscode-editor-background)]/90 p-4">
            <div className="mb-3 flex items-center gap-2">
              <Server size={16} className="text-[var(--vscode-terminal-ansiGreen)]" />
              <h2 className="text-sm font-semibold">Target Project Stack Metadata</h2>
            </div>
            <div className="space-y-3">
              <div className="grid gap-1">
                <label className="text-xs opacity-80">Backend</label>
                <select
                  className="rounded-md border border-[var(--vscode-input-border)] bg-[var(--vscode-dropdown-background)] px-3 py-2 text-sm outline-none focus:border-[var(--vscode-focusBorder)]"
                  value={stack.backend}
                  onChange={e =>
                    setStack(prev => ({ ...prev, backend: e.target.value as ArcTechStack['backend'] }))
                  }
                >
                  <option value="nodejs">Node.js</option>
                </select>
                <p className="text-[11px] opacity-75">
                  Runtime: {stackProfile.backend.runtime} | Framework: {stackProfile.backend.framework}
                </p>
                <p className="text-[11px] opacity-75">Testing: {stackProfile.backend.testing.join(' / ')}</p>
              </div>

              <div className="grid gap-1">
                <label className="text-xs opacity-80">Frontend</label>
                <div className="flex items-center justify-between rounded-md border border-[var(--vscode-input-border)] bg-[var(--vscode-input-background)] px-3 py-2 text-sm">
                  <span>React</span>
                  <CheckCircle2 size={14} className="opacity-80" />
                </div>
                <p className="text-[11px] opacity-75">
                  {stackProfile.frontend.language} | {stackProfile.frontend.styling}
                </p>
                <p className="text-[11px] opacity-75">
                  HTTP: {stackProfile.frontend.http}
                </p>
              </div>

              <div className="grid gap-1">
                <label className="text-xs opacity-80">Database</label>
                <div className="flex items-center justify-between rounded-md border border-[var(--vscode-input-border)] bg-[var(--vscode-input-background)] px-3 py-2 text-sm">
                  <span className="inline-flex items-center gap-2">
                    <Database size={14} />
                    SQLite
                  </span>
                  <CheckCircle2 size={14} className="opacity-80" />
                </div>
                <p className="text-[11px] opacity-75">{stackProfile.backend.database}</p>
              </div>
            </div>
          </section>
        </div>

        <div className="mt-5 flex items-center justify-between rounded-xl border border-[var(--vscode-panel-border)] bg-[var(--vscode-editor-background)]/90 p-4">
          <p className="text-xs opacity-80">{status || 'Saving will update arc-agent/.env and .arc/metadata.md'}</p>
          <div className="flex items-center gap-2">
            <button
              onClick={onCancel}
              className="rounded-md border border-[var(--vscode-button-border)] px-4 py-2 text-sm hover:bg-[var(--vscode-list-hoverBackground)]"
            >
              Cancel
            </button>
            <button
              onClick={onSave}
              className="rounded-md bg-[var(--vscode-button-background)] px-4 py-2 text-sm text-[var(--vscode-button-foreground)] hover:brightness-110"
            >
              Save
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
