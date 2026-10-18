# AGENTS.md

## Project overview

This repository is a local Python demo for a security incident memory system called Mesh. The app persists case state, handoffs, and recalled memories in FalkorDB and serves a small web UI from the Python standard library.

Use the project docs as the source of truth:
- [README.md](README.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)

## Essential commands

Start the app:

```bash
docker compose up -d
python3 start.py
```

Run backend tests:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Run DOM tests:

```bash
npm ci
npm test
```

## Key architecture

- `start.py` launches the app
- `memory_mesh/server.py` handles routes and HTML/static assets
- `memory_mesh/service.py` owns workflow logic, FalkorDB reads/writes, and tenant-scoped memory logic
- `memory_mesh/static/` contains the UI assets (`index.html`, `app.css`, `app.js`)
- `tests/` contains service, HTTP, and interaction regressions

## Working conventions

- Keep the app simple: no frontend build system is required.
- Treat FalkorDB as required for production behavior; SQLite is only for explicit test-backed service setups.
- Keep tenant scoping in mind for all data access and UI state.
- Prefer small, focused changes around incident workflow logic or UI behavior rather than broad refactors.
- If the app fails to start with `OSError: [Errno 48] Address already in use`, stop the stale server using the existing port and restart it.

## Common pitfalls

- The default local URL is http://127.0.0.1:8000.
- The app persists data across restarts; it does not reset the production graph on startup.
- The demo is intentionally not production auth or a real incident-response system.
- UI requests and tenant selection must be treated as demo data, not authenticated identities.
