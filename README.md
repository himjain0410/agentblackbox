# AgentBlackBox — AI Agent Flight Recorder (MVP)

A local-first prototype that records agent execution events, displays a timeline, flags likely verification gaps, and exports an incident trace as JSON.

## Requirements
- Python 3.10+
- Internet access only for the first-time pip install

## Run locally

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000 and click **Run demo incident**.

## What works in this MVP
- FastAPI event ingestion API
- SQLite persistence for runs and events
- Execution timeline and event detail inspection
- Basic evidence-linked, deterministic failure analysis
- Best-effort redaction for common secret-like field names and strings
- Incident JSON export
- Python recorder helper (`recorder.py`)
- Example instrumentation script (`example_agent.py`)

## API quick start
- `GET /api/health`
- `GET /api/runs`
- `POST /api/runs` with `{"name":"Example run","agent":"Python Agent"}`
- `POST /api/runs/{run_id}/events`
- `POST /api/runs/{run_id}/finish`
- `GET /api/runs/{run_id}`
- `POST /api/demo`
- `DELETE /api/runs` (clears local demo data)

Interactive API docs are available at http://127.0.0.1:8000/docs.

## Current limitations
- The demo incident is illustrative, not a real analysis of your machine.
- This version records events only when instrumented; it does not automatically intercept every action of arbitrary agents.
- The analyzer uses deterministic heuristics, not a trained causal model.
- Redaction is best-effort. Never use real secrets or sensitive production data in this prototype.
- No authentication, multi-user isolation, or production security hardening yet.
- Event replay is visual/trace replay only. Arbitrary commands are not automatically re-executed.

## Next milestone
Add a controlled tool-execution wrapper that records command arguments, exit codes, stdout/stderr summaries, file diffs, and test results in an isolated workspace.
