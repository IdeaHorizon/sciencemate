# Running ScienceMate from source

This tree is the Personal edition. The installers in [Releases](../../releases) are built from it with the packagers in `scripts/package/`.

## Layout

| Path | What it is |
|---|---|
| `platform/backend/` | The application server (FastAPI, Python 3.12+, managed with [uv](https://docs.astral.sh/uv/)). The desktop app runs exactly this backend. |
| `platform/frontend/` | The web interface (Next.js, Node 22). The desktop app serves its static export. |
| `platform/desktop/` | The thin native shells (macOS Swift, Windows C#) that start the backend and open the interface. |
| `core/`, `nodes/`, `shared/`, `chat.py` | The research harness: the agent loop, the research nodes (literature, hypothesis, experiment, analysis, writing, …) and the CLI entry point. |
| `scripts/package/` | Packagers for macOS and Windows, the self-updating payload and the release publisher. |
| `tests/` | Harness tests; backend tests live in `platform/backend/tests/`, frontend tests next to the code as `*.test.ts`. |

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (it installs the right Python itself)
- Node.js 22 with npm
- git

## Run the application

Build the interface once (the backend serves the static export from `platform/frontend/out`):

```bash
cd platform/frontend
npm ci
PLATFORM_STATIC_EXPORT=1 npm run build
```

Start the backend; it opens the interface in your browser:

```bash
cd platform/backend
uv sync
uv run python -m app.launcher start
```

`uv run python -m app.launcher doctor` prints where the data root, interface, harness, git, model shell, PDF engine and sandbox live. Data lives under the platform data root (`PLATFORM_DATA_ROOT`, `~/.harness-framework` unless you set it) in SQLite; nothing is written into the source tree.

For interface development run `npm run dev` in `platform/frontend` against a running backend instead of rebuilding the static export.

## Use the harness on its own

The research harness also runs as a command-line agent without the application server:

```bash
uv sync
cp .env.example .env      # fill in LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
uv run python chat.py --project my-project
```

## Tests

```bash
uv run python -m pytest tests/ -q                       # harness
cd platform/backend && uv sync --extra dev && uv run pytest -q   # backend
cd platform/frontend && npm run typecheck && npm test   # frontend
```

## Build the installers

```bash
# macOS (Apple Silicon), run on a Mac with Xcode command line tools:
platform/backend/.venv/bin/python scripts/package/build_mac_app.py --edition personal

# Windows, run on Windows with the Visual Studio build tools:
uv run --python 3.12 python scripts/package/build_windows_app.py --with-ui --installer --edition personal
```

Both packagers install the package they built and run it once before they report success. They refuse to build the Personal edition from a tree that contains the Professional edition, so a package built here is exactly this tree.
