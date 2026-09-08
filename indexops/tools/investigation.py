"""Investigation tools: diff source vs index, sample evidence, read Airflow logs."""
import requests
from requests.auth import HTTPBasicAuth

from indexops.config import AIRFLOW_BASE_URL, AIRFLOW_USER, AIRFLOW_PASSWORD, AIRFLOW_DAG_ID
from indexops.contract import TICKETS_INDEX, TEAMS_BY_CATEGORY, expected_team
from indexops.db import fetch_all, fetch_one, os_client


def _indexed_docs() -> dict[int, dict]:
    """Pull every ticket doc (minus embeddings) from OpenSearch keyed by ticket_id."""
    client = os_client()
    docs = {}
    resp = client.search(index=TICKETS_INDEX, scroll="2m", size=1000,
                         body={"query": {"match_all": {}}, "_source": ["ticket_id", "category", "assigned_team", "priority"]})
    while True:
        hits = resp["hits"]["hits"]
        if not hits:
            break
        for h in hits:
            docs[int(h["_id"])] = h["_source"]
        resp = client.scroll(scroll_id=resp["_scroll_id"], scroll="2m")
    return docs


def compare_source_vs_index() -> dict:
    """Compare Postgres tickets (source of truth) with the OpenSearch tickets index.

    Returns counts plus the ticket_ids that are mismatched (wrong team), missing
    from the index, or present in the index but not in the source.
    """
    source = {r["ticket_id"]: r for r in fetch_all("SELECT ticket_id, category, assigned_team FROM tickets")}
    indexed = _indexed_docs()

    mismatched, missing = [], []
    wrong_team_breakdown: dict[str, dict[str, int]] = {}
    for tid, src in source.items():
        doc = indexed.get(tid)
        if doc is None:
            missing.append(tid)
            continue
        expected = expected_team(src["category"])
        if doc.get("assigned_team") != expected:
            mismatched.append(tid)
            wrong_team_breakdown.setdefault(src["category"], {})
            wrong_team_breakdown[src["category"]][doc.get("assigned_team")] = \
                wrong_team_breakdown[src["category"]].get(doc.get("assigned_team"), 0) + 1
    orphans = sorted(set(indexed) - set(source))

    total = max(len(source), 1)
    return {
        "source_count": len(source),
        "indexed_count": len(indexed),
        "mismatched_count": len(mismatched),
        "mismatched_pct": round(len(mismatched) / total * 100, 2),
        "missing_from_index_count": len(missing),
        "orphan_in_index_count": len(orphans),
        "wrong_team_by_category": wrong_team_breakdown,
        "expected_mapping": TEAMS_BY_CATEGORY,
        "mismatched_ticket_ids": sorted(mismatched),
        "missing_ticket_ids": sorted(missing)[:200],
        "orphan_ticket_ids": orphans[:200],
    }


def sample_mismatched_tickets(limit: int = 10) -> dict:
    """Return concrete examples of mismatched tickets: source row vs indexed doc, side by side."""
    source = {r["ticket_id"]: r for r in fetch_all(
        "SELECT ticket_id, description, category, priority, assigned_team FROM tickets")}
    indexed = _indexed_docs()
    samples = []
    for tid in sorted(source):
        doc = indexed.get(tid)
        if not doc:
            continue
        expected = expected_team(source[tid]["category"])
        if doc.get("assigned_team") != expected:
            samples.append({
                "ticket_id": tid,
                "description": source[tid]["description"],
                "category": source[tid]["category"],
                "source_assigned_team": source[tid]["assigned_team"],
                "expected_team": expected,
                "indexed_assigned_team": doc.get("assigned_team"),
            })
            if len(samples) >= limit:
                break
    return {"samples": samples, "returned": len(samples)}


def _airflow_get(path: str, **params):
    r = requests.get(f"{AIRFLOW_BASE_URL}/api/v1{path}", params=params,
                     auth=HTTPBasicAuth(AIRFLOW_USER, AIRFLOW_PASSWORD), timeout=30)
    r.raise_for_status()
    return r


def get_airflow_task_logs(run_id: int, task_id: str | None = None, tail_lines: int = 60) -> dict:
    """Task states for an Airflow DAG run, plus the log tail of one task (or of every task).

    `run_id` is the pipeline_runs.run_id; it is resolved to the Airflow dag_run_id.
    Uses the Airflow REST API (basic auth).
    """
    run = fetch_one("SELECT dag_run_id FROM pipeline_runs WHERE run_id = %s", (run_id,))
    if not run:
        return {"error": f"run_id {run_id} not found"}
    dag_run_id = run["dag_run_id"]

    try:
        tis = _airflow_get(f"/dags/{AIRFLOW_DAG_ID}/dagRuns/{dag_run_id}/taskInstances").json()["task_instances"]
    except requests.RequestException as e:
        return {"error": f"Airflow API call failed: {e}", "dag_run_id": dag_run_id,
                "hint": "Is AIRFLOW__API__AUTH_BACKENDS set to basic_auth on the webserver?"}

    tasks = [{"task_id": t["task_id"], "state": t["state"], "try_number": t["try_number"],
              "duration_s": t["duration"], "start": t["start_date"], "end": t["end_date"]}
             for t in sorted(tis, key=lambda t: t["start_date"] or "")]

    logs = {}
    targets = [task_id] if task_id else [t["task_id"] for t in tasks]
    for tid in targets:
        try_no = next((t["try_number"] for t in tasks if t["task_id"] == tid), 1) or 1
        try:
            text = _airflow_get(f"/dags/{AIRFLOW_DAG_ID}/dagRuns/{dag_run_id}/taskInstances/{tid}/logs/{try_no}",
                                full_content="true").text
            lines = [ln for ln in text.splitlines() if ln.strip()]
            logs[tid] = "\n".join(lines[-tail_lines:])
        except requests.RequestException as e:
            logs[tid] = f"<log fetch failed: {e}>"

    return {"dag_run_id": dag_run_id, "task_instances": tasks, "logs": logs}
