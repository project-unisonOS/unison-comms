# unison-comms

Communications service for UnisonOS. Provides intent-centric comms endpoints (`comms.check`, `comms.summarize`, `comms.reply`, `comms.compose`) behind a normalized message shape, ready to feed the orchestrator and Operating Surface.

## Status
New service skeleton (active) — stubbed HTTP API and health checks; adapters to be implemented next.

## Run
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8080
```

## Testing
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -c ../constraints.txt -r requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 OTEL_SDK_DISABLED=true python -m pytest
```

## Endpoints (stubbed)
- `GET /health`, `GET /readyz`
- `POST /comms/check` — returns normalized messages + dashboard-friendly cards
- `POST /comms/summarize` — returns a summary and summary cards
- `POST /comms/reply` — validates identifiers, returns confirmation
- `POST /comms/compose` — validates recipients/subject, returns confirmation

## Docs

Full docs at https://project-unisonos.github.io
