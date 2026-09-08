"""Monitoring tools: read-only views over pipeline_runs / metrics / OpenSearch."""
from indexops.contract import TICKETS_INDEX, KNOWLEDGE_BASE_INDEX
from indexops.db import fetch_all, fetch_one, os_client


def get_recent_runs(limit: int = 10) -> dict:
    """List the most recent pipeline runs with their headline data-quality numbers."""
    rows = fetch_all(
        """
        SELECT r.run_id, r.dag_run_id, r.status, r.inject_type, r.inject_pct,
               r.started_at, r.ended_at,
               m.total_processed, m.total_indexed, m.mismatch_count,
               ROUND(m.mismatch_pct::numeric, 2) AS mismatch_pct, m.duplicate_count,
               (SELECT COUNT(*) FROM alerts a WHERE a.run_id = r.run_id) AS alert_count
        FROM pipeline_runs r
        LEFT JOIN data_quality_metrics m ON m.run_id = r.run_id
        ORDER BY r.run_id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {"runs": rows, "count": len(rows)}


def get_run_metrics(run_id: int) -> dict:
    """Full detail for one run: run row, data-quality metrics, index metrics, alerts."""
    run = fetch_one("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
    if not run:
        return {"error": f"run_id {run_id} not found"}
    dq = fetch_one("SELECT * FROM data_quality_metrics WHERE run_id = %s ORDER BY metric_id DESC", (run_id,))
    idx = fetch_all("SELECT * FROM index_metrics WHERE run_id = %s ORDER BY metric_id", (run_id,))
    alerts = fetch_all("SELECT * FROM alerts WHERE run_id = %s ORDER BY alert_id", (run_id,))
    baseline = fetch_one(
        """
        SELECT r.run_id, ROUND(m.mismatch_pct::numeric, 2) AS mismatch_pct, m.total_indexed
        FROM pipeline_runs r JOIN data_quality_metrics m ON m.run_id = r.run_id
        WHERE r.run_id < %s AND r.inject_type = 'none'
        ORDER BY r.run_id DESC LIMIT 1
        """,
        (run_id,),
    )
    derived = {}
    if dq:
        total = max(dq.get("total_processed") or 0, 1)
        derived = {
            # extracted rows = unique docs indexed + duplicates + rows dropped by validate_data
            "dropped_count": (dq["total_processed"] or 0) - (dq["duplicate_count"] or 0) - (dq["total_indexed"] or 0),
            "duplicate_pct": round((dq["duplicate_count"] or 0) / total * 100, 2),
        }
    return {
        "run": run,
        "data_quality": dq,
        "index_metrics": idx,
        "alerts": alerts,
        "derived": derived,
        "last_clean_baseline": baseline,
    }


def get_pipeline_config(run_id: int | None = None) -> dict:
    """Current failure-injection Airflow Variables, plus the values recorded for a given run.

    Reads Airflow's own `variable` table directly (no REST auth needed).
    """
    rows = fetch_all(
        "SELECT key, val, is_encrypted FROM variable WHERE key IN ('inject_failure_type', 'inject_failure_pct')"
    )
    current = {}
    for r in rows:
        current[r["key"]] = "<encrypted>" if r["is_encrypted"] else r["val"]
    current.setdefault("inject_failure_type", "none (unset)")
    current.setdefault("inject_failure_pct", "0 (unset)")

    result = {"current_variables": current}
    if run_id is not None:
        run = fetch_one(
            "SELECT run_id, dag_run_id, inject_type, inject_pct, started_at FROM pipeline_runs WHERE run_id = %s",
            (run_id,),
        )
        result["recorded_for_run"] = run or {"error": f"run_id {run_id} not found"}
    result["note"] = (
        "inject_type != 'none' means the transform/extract step was deliberately misconfigured "
        "for that run. This is the pipeline configuration in effect, not a code defect."
    )
    return result


def get_index_stats(index_name: str = TICKETS_INDEX) -> dict:
    """Doc count, size, health and team/category distribution of an OpenSearch index."""
    client = os_client()
    if not client.indices.exists(index=index_name):
        return {"error": f"index '{index_name}' does not exist"}
    count = client.count(index=index_name)["count"]
    stats = client.indices.stats(index=index_name)["indices"][index_name]["primaries"]
    health = client.cluster.health(index=index_name)
    out = {
        "index": index_name,
        "doc_count": count,
        "store_size_bytes": stats["store"]["size_in_bytes"],
        "health": health["status"],
    }
    if index_name == TICKETS_INDEX:
        aggs = client.search(index=index_name, body={
            "size": 0,
            "aggs": {
                "by_team": {"terms": {"field": "assigned_team", "size": 20}},
                "by_category": {"terms": {"field": "category", "size": 20}},
                "team_per_category": {
                    "terms": {"field": "category", "size": 20},
                    "aggs": {"teams": {"terms": {"field": "assigned_team", "size": 20}}},
                },
            },
        })["aggregations"]
        out["by_team"] = {b["key"]: b["doc_count"] for b in aggs["by_team"]["buckets"]}
        out["by_category"] = {b["key"]: b["doc_count"] for b in aggs["by_category"]["buckets"]}
        out["teams_per_category"] = {
            b["key"]: {t["key"]: t["doc_count"] for t in b["teams"]["buckets"]}
            for b in aggs["team_per_category"]["buckets"]
        }
    if index_name == KNOWLEDGE_BASE_INDEX:
        titles = client.search(index=index_name, body={"size": 50, "_source": ["title"]})["hits"]["hits"]
        out["documents"] = [h["_source"]["title"] for h in titles]
    return out
