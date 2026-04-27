/**
 * Tiny presentational helpers shared across pages. Keep them dumb — no
 * data fetching, no router awareness — so they're trivially composable.
 *
 * Pure-function helpers live in ``lib/format.ts`` so this file only exports
 * components (react-refresh requirement).
 */
import type { ReactNode } from "react";

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-zinc-500">
        {title}
      </h2>
      {children}
    </section>
  );
}

const STATUS_COLOURS: Record<string, string> = {
  // Job statuses
  queued: "bg-yellow-900/40 text-yellow-300",
  cancelled: "bg-zinc-800 text-zinc-400",
  // Analysis statuses
  pending: "bg-zinc-800 text-zinc-300",
  // Shared
  running: "bg-blue-900/40 text-blue-300",
  completed: "bg-green-900/40 text-green-300",
  failed: "bg-red-900/40 text-red-300",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-[10px] ${STATUS_COLOURS[status] ?? "bg-zinc-800"}`}
    >
      {status}
    </span>
  );
}
