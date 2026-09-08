"""Runtime configuration for host-side code (MCP server, FastAPI, agent).

Everything is overridable via environment variables so the same code runs on
the Windows host (defaults) or inside a container (PG_HOST=postgres, ...).
"""
import os

from dotenv import load_dotenv

load_dotenv()

PG = dict(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    user=os.getenv("PG_USER", "airflow"),
    password=os.getenv("PG_PASSWORD", "airflow"),
    dbname=os.getenv("PG_DB", "airflow"),
)

OPENSEARCH = {"host": os.getenv("OS_HOST", "localhost"), "port": int(os.getenv("OS_PORT", "9200"))}

AIRFLOW_BASE_URL = os.getenv("AIRFLOW_BASE_URL", "http://localhost:8080")
AIRFLOW_USER = os.getenv("AIRFLOW_USER", "admin")
AIRFLOW_PASSWORD = os.getenv("AIRFLOW_PASSWORD", "admin")
AIRFLOW_DAG_ID = os.getenv("AIRFLOW_DAG_ID", "indexops_pipeline")

# LLM settings live in indexops/llm_client.py (the single LLM entry point).
