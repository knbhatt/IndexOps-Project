# Troubleshooting and operating notes

## Local-only demo boundary
Postgres (5432), OpenSearch (9200/9600) and Airflow (8080) are bound to `127.0.0.1` in
`docker-compose.yml`. The FastAPI `/approve` route and the Streamlit approval page have **no
authentication**; do not expose them beyond localhost. Add authentication before any shared deployment.
Demo passwords (`airflow/airflow`, `admin/admin`) are for local use only.

## Approval rules (what blocks an approval)
- The incident must be for the **latest pipeline run** (the index is rebuilt every run).
- The incident report must be **validated** (`validation_passed = true`) and its `affected_ticket_count`
  must equal the queued action's ticket count. A manual override exists in the dashboard for unvalidated
  incidents; stale incidents can never be overridden.

## Common errors
| Symptom | Cause / fix |
|---|---|
| `GEMINI_API_KEY is not set` | `Copy-Item .env.example .env`, then put your key in `.env` |
| 429 / quota errors | Free tier limit. Use `gemini-3.5-flash-lite` (set `LLM_MODEL`), wait, retry |
| `unable to get image ... dockerDesktopLinuxEngine` | Docker Desktop is not running; start it and wait for the whale icon |
| Airflow API errors in `get_airflow_task_logs` | Webserver not ready; check `docker ps`, wait ~60 s |
| MCP startup hangs | Run `python -m pip install -r requirements.txt`; check `python mcp_server.py` starts |
| Alert says "older pipeline run" | Investigate the newest alert/run; old runs cannot be diagnosed reliably |
| Approval says "not validated" | Reflection did not pass; re-investigate, or use the explicit manual override |

## Example operator questions (Ask the agent)
- "Why are so many tickets in the wrong team?"
- "Did the latest run lose any tickets?"
- "Is the index healthy right now?" (expect NO_ISSUE on a clean run)

## Tests
`python -m pytest`
