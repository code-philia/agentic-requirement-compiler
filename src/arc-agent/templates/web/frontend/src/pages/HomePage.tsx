import { Link } from 'react-router-dom';

const quickLinks = [
  { to: '/login', label: 'Sign in' },
  { to: '/register', label: 'Create account' },
];

function HomePage() {
  return (
    <main className="min-h-screen bg-slate-950 text-slate-50">
      <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col justify-center px-6 py-16 sm:px-10 lg:px-12">
        <section className="grid gap-10 overflow-hidden rounded-[2rem] border border-white/10 bg-white/5 p-8 shadow-2xl shadow-slate-950/40 backdrop-blur sm:p-10 lg:grid-cols-[1.4fr_0.9fr] lg:p-14">
          <div className="space-y-6">
            <span className="inline-flex rounded-full border border-cyan-400/30 bg-cyan-400/10 px-3 py-1 text-xs font-semibold uppercase tracking-[0.3em] text-cyan-200">
              ARC Web Template
            </span>
            <div className="space-y-4">
              <h1 className="max-w-3xl text-4xl font-black tracking-tight text-white sm:text-5xl">
                Build the frontend once. Serve it from the backend on one stable port.
              </h1>
              <p className="max-w-2xl text-base leading-7 text-slate-300 sm:text-lg">
                This starter is intentionally wired for ARC: Vite builds the React app into
                <code className="mx-1 rounded bg-white/10 px-2 py-1 text-sm text-cyan-100">frontend/dist</code>
                and Express hosts it on the final runtime origin.
              </p>
            </div>
            <div className="flex flex-wrap gap-3">
              {quickLinks.map((link) => (
                <Link
                  key={link.to}
                  to={link.to}
                  className="inline-flex items-center rounded-full border border-white/15 bg-white px-5 py-3 text-sm font-semibold text-slate-950 transition hover:bg-cyan-300"
                >
                  {link.label}
                </Link>
              ))}
            </div>
          </div>

          <aside className="rounded-[1.5rem] border border-white/10 bg-slate-900/80 p-6">
            <p className="text-sm font-semibold uppercase tracking-[0.3em] text-slate-400">
              Frontend defaults
            </p>
            <dl className="mt-6 space-y-4 text-sm text-slate-200">
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <dt className="text-slate-400">Language</dt>
                <dd className="mt-1 font-medium">TypeScript + TSX</dd>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <dt className="text-slate-400">Styling</dt>
                <dd className="mt-1 font-medium">Tailwind CSS utility classes</dd>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <dt className="text-slate-400">Runtime</dt>
                <dd className="mt-1 font-medium">Backend-hosted single origin</dd>
              </div>
            </dl>
          </aside>
        </section>
      </div>
    </main>
  );
}

export default HomePage;
