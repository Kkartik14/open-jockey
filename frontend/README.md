# open-jockey · frontend

Local web UI for open-jockey. Talks to the backend at `http://127.0.0.1:8000` through Vite's dev proxy.

## Stack

- **React 19** + **TypeScript**
- **Vite 6** (pinned — see [Why Vite 6](#why-vite-6))
- **Tailwind CSS v4** via the official `@tailwindcss/vite` plugin
- Plain `fetch` for the API client; no react-query, no zustand. Add them when the state model demands it, not before.

## Layout

```
frontend/
├── package.json
├── vite.config.ts              # React + Tailwind plugins, /api proxy → :8000
├── tsconfig.json + tsconfig.{app,node}.json
├── index.html
└── src/
    ├── main.tsx                # React root
    ├── App.tsx                 # the v0 page (health badge, plugins, ingest, tracks, jobs)
    ├── api.ts                  # typed API client + response types
    └── index.css               # tailwind import + a few base styles
```

The hand-written types in `src/api.ts` mirror the backend's Pydantic response models. If the API surface grows we'll switch to OpenAPI codegen.

## Install + run

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173
```

Make sure the backend is up on `:8000` first — the dev server proxies `/api/*` to it.

## Build

```bash
npm run build        # type-check + production bundle into dist/
npm run preview      # serve the built bundle locally
```

## Backend proxy

`vite.config.ts`:

```ts
server: {
  host: '127.0.0.1',
  port: 5173,
  strictPort: true,
  proxy: {
    '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
  },
},
```

This means the frontend can call `fetch('/api/health')` without CORS, and a single origin is used in dev. The backend still ships CORS for `localhost:5173` so direct cross-origin calls work too if you skip the proxy.

## Why Vite 6

Vite 7 and 8 require Node.js ≥ 20.19. Earlier Node versions are still common locally, so we pinned Vite to v6 so the project boots out-of-the-box on Node 20.15+. When you've upgraded to a newer Node:

```bash
npm install -D vite@latest @vitejs/plugin-react@latest
```

…and re-test. No code change needed.

`erasableSyntaxOnly` was removed from the tsconfigs because it requires TypeScript 5.8+; we pinned TS to ~5.6 to match the rest of the toolchain.

## Conventions

- Tailwind utility classes only — no CSS modules, no styled-components. Component styles are co-located with the JSX.
- API calls always go through `api.ts`. Nothing else may call `fetch('/api/…')` directly.
- Polling intervals are explicit (currently 5s in `App.tsx`). When real-time matters, switch the route to SSE — FastAPI supports it natively.
