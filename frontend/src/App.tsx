/**
 * App shell: persistent header (title + health badge) plus the route
 * outlet. Page-level state lives inside each route's component so we
 * never have to thread props through here.
 */
import { useEffect, useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { api, type Health } from "./api";
import { LibraryPage } from "./pages/LibraryPage";
import { TrackDetailPage } from "./pages/TrackDetailPage";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function tick() {
      try {
        const h = await api.health();
        if (active) {
          setHealth(h);
          setHealthErr(null);
        }
      } catch (e) {
        if (active) setHealthErr((e as Error).message);
      }
    }
    void tick();
    const t = setInterval(tick, 5000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="mx-auto min-h-screen w-full max-w-5xl space-y-8 p-6 text-zinc-200">
      <header className="flex items-center justify-between border-b border-zinc-800 pb-4">
        <Link to="/" className="block">
          <h1 className="text-2xl font-semibold text-zinc-100">aidj</h1>
          <p className="text-sm text-zinc-500">Phase 1 — analyzer</p>
        </Link>
        <HealthBadge health={health} err={healthErr} />
      </header>
      <Routes>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/track/:hash" element={<TrackDetailPage />} />
      </Routes>
    </div>
  );
}

function HealthBadge({ health, err }: { health: Health | null; err: string | null }) {
  if (err) {
    return (
      <span className="rounded-full bg-red-900/40 px-3 py-1 font-mono text-xs text-red-300">
        backend offline
      </span>
    );
  }
  if (!health) {
    return (
      <span className="rounded-full bg-zinc-800 px-3 py-1 font-mono text-xs text-zinc-400">
        connecting…
      </span>
    );
  }
  return (
    <span className="rounded-full bg-green-900/40 px-3 py-1 font-mono text-xs text-green-300">
      backend ok · v{health.version}
    </span>
  );
}
