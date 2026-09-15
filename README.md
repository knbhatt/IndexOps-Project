# IndexOps Project

**Intelligent IndexOps** — an Airflow → OpenSearch ticket-indexing pipeline with an AI agent that investigates silent data-quality failures, backed by MCP tools, RAG over runbooks, a Streamlit dashboard, and a human-approval gate for remediation.

> Core thesis: **Airflow can report SUCCESS while indexed data is wrong.** This project detects that gap, explains it with evidence, and lets a human approve the fix.

---

## Scenario

An IT service desk indexes tickets into OpenSearch so teams can search and route work. The pipeline:

1. Extracts tickets from Postgres  
2. Validates and transforms them (category → assigned team)  
3. Generates embeddings (`all-MiniLM-L6-v2`)  
4. Indexes documents into OpenSearch  
5. Computes data-quality metrics  

**The failure mode:** a bad category→team mapping (or missing fields / duplicates) can still produce a green Airflow run. Tickets land in the wrong team bucket; nobody notices from the DAG status alone.

This project lets you **inject** that failure on demand, raises an **alert** when `mismatch_pct > 5%`, then runs an **AI agent** that:

- Pulls run metrics and pipeline config  
- Diffs Postgres (source of truth) vs OpenSearch  
- Retrieves matching runbooks / past incidents via RAG  
- Submits a structured incident report  
- Stages remediation for **human approval** (nothing mutates the index until Approve)

### Correct category → team mapping (data contract)

| Category | Team |
|----------|------|
| Network | NetOps |
| Hardware | Field Support |
| Software | App Support |
| Access | IAM Team |
| Email | Messaging Team |

---

## Architecture

```text
┌─────────────────┐     extract / transform / embed      ┌──────────────┐
│  Postgres       │ ───────────────────────────────────► │  OpenSearch  │
│  (tickets +     │                                      │  tickets +   │
│   metrics +     │ ◄──── metrics / alerts / incidents ──│  knowledge_base│
│   incidents)    │                                      └──────────────┘
└────────┬────────┘                                              ▲
         │                                                       │
         │  DAG tasks                                            │ index
         ▼                                                       │
┌─────────────────┐                                              │
│  Airflow        │  indexops_pipeline                           │
│  web + scheduler│ ─────────────────────────────────────────────┘
└────────┬────────┘
         │
         │  alerts (mismatch / dropped / duplicates)
         ▼
┌─────────────────┐     MCP (stdio)      ┌─────────────────┐
│  FastAPI /      │ ◄──────────────────► │  mcp_server.py  │
│  Streamlit      │                      │  ~10 tools      │
│  agent loop     │ ───────────────────► │  (Postgres /    │
└────────┬────────┘   tool calls         │   Airflow API / │
         │                               │   OpenSearch)   │
         │  Gemini (tool calling)        └─────────────────┘
         ▼
┌─────────────────┐
│  Human Approve  │ ──► reindex tickets / trigger clean DAG re-run
│  (Streamlit)    │
└─────────────────┘
```

### Pipeline tasks (Airflow DAG `indexops_pipeline`)

`extract_data` → `validate_data` → `transform_data` → `embed_and_index` → `validate_index` → `calculate_metrics` → `analyze_index_health`

Failure injection is controlled by Airflow Variables:

| Variable | Example | Effect |
|----------|---------|--------|
| `inject_failure_type` | `category_mapping_drift` | Wrong teams on N% of tickets |
| | `missing_fields` | Blank required fields → rows dropped |
| | `duplicate_tickets` | Duplicate `ticket_id`s in the batch |
| `inject_failure_pct` | `0.2` | Fraction of tickets affected |

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Orchestration | Apache Airflow 2.9 (Docker, LocalExecutor) |
| Database | PostgreSQL 15 (shared: app tables + Airflow metadata) |
| Search / vectors | OpenSearch 2.11 (`knn_vector`, dim 384) |
| Embeddings | `sentence-transformers` / `all-MiniLM-L6-v2` |
| Agent LLM | Google Gemini via OpenAI-compatible API (`gemini-3.5-flash-lite` recommended on free tier) |
| Tool protocol | MCP (Model Context Protocol) over stdio |
| Agent API | FastAPI (spawns MCP subprocess) |
| Dashboard | Streamlit |
| Infra | Docker Compose + custom Airflow Dockerfile (CPU torch baked in) |
| Demo reset | `demo.ps1` (PowerShell; random or pinned `-Count` / `-InjectPct`) |
| Ticket seed data | KameronB synthetic IT call-center tickets (CSV cached in `data/raw/`) |

### MCP tools (agent)

**Monitoring:** `get_recent_runs`, `get_run_metrics`, `get_pipeline_config`, `get_index_stats`  
**Investigation:** `compare_source_vs_index`, `sample_mismatched_tickets`, `get_airflow_task_logs`  
**RAG:** `search_knowledge_base` (kNN + BM25 fallback)  
**Remediation (queue only):** `reindex_affected_tickets`, `trigger_pipeline_rerun`  
**Terminal tool (agent loop):** `submit_incident_report`

Remediation tools only insert `pending` rows. Execution happens solely through `indexops.approval.approve(action_id)`.

---

## Prerequisites

- **Docker Desktop** (≈6–8 GB RAM allocated recommended)
- **Python 3.11+** on the host
- **Gemini API key** from [Google AI Studio](https://aistudio.google.com)
- **PowerShell** (for `demo.ps1` on Windows)

---

## Quick start

### 1. Clone and enter the project

```powershell
git clone https://github.com/knbhatt/IndexOps-Project.git
cd IndexOps-Project
```

### 2. Configure environment

```powershell
Copy-Item .env.example .env
# Edit .env and set GEMINI_API_KEY=
```

Recommended free-tier model:

```env
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.5-flash-lite
GEMINI_API_KEY=your-key-here
```

> Prefer `gemini-3.5-flash-lite` (~500 requests/day). Full `*-flash` free tier is often only ~20/day.

### 3. Install host Python packages

```powershell
python -m pip install -r requirements.txt
```

If `torch` fails via `requirements.txt`, install CPU torch first:

```powershell
python -m pip install torch --extra-index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
```

### 4. Start the stack

```powershell
docker compose up -d --build
```

Wait until these are running:

| Container | Purpose |
|-----------|---------|
| `indexops-postgres` | Tickets, metrics, alerts, incidents |
| `indexops-opensearch` | `tickets` + `knowledge_base` indexes |
| `indexops-airflow-web` | Airflow UI |
| `indexops-airflow-scheduler` | Runs the DAG |

`indexops-airflow-init` exits after DB migrate + admin user creation — that is expected.

### 5. One-click demo seed + failure injection

Ticket text comes from **[KameronB/synthetic-it-callcenter-tickets](https://huggingface.co/datasets/KameronB/synthetic-it-callcenter-tickets)** (~27k synthetic IT tickets). The CSV is auto-downloaded once into `data/raw/` (gitignored) and **reused offline** on later runs. Categories are mapped onto the IndexOps contract (Network / Hardware / Software / Access / Email) with stratified sampling.

```powershell
# General testing — random -Count and -InjectPct each run
.\demo.ps1 -SkipInvestigate

# Demo day — FIXED, known values
.\demo.ps1 -Count 2500 -InjectPct 0.2 -SkipInvestigate

# Clean baseline (no faults) — expect mismatch_pct ≈ 0
.\demo.ps1 -Count 800 -InjectPct 0 -InjectType none -SkipInvestigate
```

This will:

1. Start the stack if needed  
2. Clear previous alerts / incidents / remediations  
3. Reseed **KameronB** IT tickets (`-Count`, or a random 1500–4200 if omitted)  
4. Index **5** knowledge-base docs if missing  
5. Inject the chosen failure (`-InjectPct`, or a random 9–31% if omitted)  
6. Trigger the DAG and wait for SUCCESS  
7. Print the new **alert id** (skipped when inject is `none` / `0`)  

### 6. Open the dashboard

```powershell
python -m streamlit run dashboard/app.py
```

Open **http://localhost:8501**

Demo click path:

1. **Alerts** — see the high-severity mismatch alert  
2. **Investigate** — start the agent from that alert  
3. **Incident & Steps** — watch tool calls stream in (~2s poll)  
4. **Approve Remediation** — Approve; mismatch % returns to ~0  

---

## Access points

| Service | URL | Credentials |
|---------|-----|-------------|
| Airflow UI | http://localhost:8080 | `admin` / `admin` |
| OpenSearch | http://localhost:9200 | none (security disabled for local demo) |
| Streamlit | http://localhost:8501 | none |
| FastAPI (optional) | http://localhost:8000/docs | none |
| Postgres | `localhost:5432` | `airflow` / `airflow` / db `airflow` |

Useful checks:

```powershell
Invoke-RestMethod http://localhost:9200/tickets/_count
Invoke-RestMethod http://localhost:9200/knowledge_base/_count
docker exec indexops-postgres psql -U airflow -d airflow -c "SELECT COUNT(*) FROM tickets;"
```

---

## Optional: FastAPI + CLI investigation

```powershell
python -m uvicorn api.main:app --port 8000
```

CLI agent (uses the alert id from `demo.ps1`):

```powershell
python -u scripts/run_investigation.py --alert 1
```

---

## Data source

Tickets are seeded from **KameronB/synthetic-it-callcenter-tickets** (Hugging Face, Apache-2.0-friendly synthetic IT helpdesk text).

```powershell
python data/generate_tickets.py --count 2500 --truncate
```

- First run downloads `data/raw/kameronb_sitcc.csv` (~36 MB); later runs reuse the cache (works offline).
- Rows are mapped onto Network / Hardware / Software / Access / Email and stratified so demos are not Software-only.
- Source rows in Postgres are **clean**; faults are injected only during the Airflow pipeline when `inject_failure_*` Variables are set.

---

## Manual setup (without `demo.ps1`)

```powershell
docker compose up -d --build
python data/generate_tickets.py --count 2500 --truncate
docker exec indexops-airflow-scheduler python /opt/airflow/data/index_knowledge_base.py

docker exec indexops-airflow-scheduler bash -c "airflow variables set inject_failure_type category_mapping_drift; airflow variables set inject_failure_pct 0.2"
docker exec indexops-airflow-scheduler bash -c "airflow dags unpause indexops_pipeline; airflow dags trigger indexops_pipeline -r manual_demo_1"
docker exec indexops-airflow-scheduler bash -c "airflow variables set inject_failure_type none; airflow variables set inject_failure_pct 0"
```

---

## Project structure

```text
IndexOps-Project/
├── docker-compose.yml          # Postgres, OpenSearch, Airflow
├── Dockerfile                  # Airflow image + torch / sentence-transformers
├── demo.ps1                    # One-click demo reset
├── .env.example                # LLM + infra template
├── requirements.txt            # Host-side Python deps
├── dags/indexops_dag.py        # Pipeline DAG
├── init-db/init.sql            # App schema
├── data/
│   ├── generate_tickets.py     # Seed N KameronB IT tickets (--count)
│   ├── raw/                    # Auto-downloaded KameronB CSV (gitignored)
│   └── index_knowledge_base.py # Embed + load RAG docs (run in container)
├── knowledge_base/             # Data contract, runbooks, past incidents
├── indexops/
│   ├── contract.py             # Single source of truth for mapping
│   ├── llm_client.py           # Sole LLM entry point (Gemini)
│   ├── mcp_client.py           # Stdio MCP client
│   ├── approval.py             # Human-approval execution gate
│   ├── agent/                  # System prompt + tool-calling loop
│   └── tools/                  # Plain tool functions wrapped by MCP
├── mcp_server.py               # MCP server (stdio)
├── api/main.py                 # FastAPI + MCP lifespan
├── dashboard/app.py            # Streamlit UI
└── scripts/
    ├── run_investigation.py
    └── smoke_agent.py
```

---

## Day-to-day restart

After a PC reboot:

```powershell
# Start Docker Desktop first, then:
docker compose up -d
.\demo.ps1 -SkipInvestigate
python -m streamlit run dashboard/app.py
```

Full wipe (delete volumes and reseed):

```powershell
docker compose down -v
docker compose up -d --build
.\demo.ps1 -SkipInvestigate
```

---

## Design notes

- **Ticket corpus** — KameronB synthetic IT call-center tickets (not Faker); mapped to the five-category contract.  
- **Green pipeline ≠ healthy data** — alerts are written by `analyze_index_health` inside the DAG.  
- **Prompt caching** — static system prompt + tool defs stay at the front of every LLM request (Gemini implicit prefix cache).  
- **Live “streaming” investigation** — each tool call commits to `investigation_steps` immediately; Streamlit polls every ~2 seconds (no SSE).  
- **Safety** — remediation never auto-executes; only `approve(action_id)` may mutate OpenSearch or trigger a DAG re-run.  
- **Secrets** — `.env` is gitignored; commit only `.env.example`.

---

## License / status

Student / MVP demo project. Built for a short (≈3-day) end-to-end demonstration, not production hardening.
