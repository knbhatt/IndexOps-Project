from airflow.decorators import dag, task
from datetime import datetime
import psycopg2
import random

from indexops.contract import (
    TEAMS_BY_CATEGORY,
    TICKETS_INDEX,
    EMBEDDING_MODEL,
    EMBEDDING_DIM,
    MISMATCH_ALERT_THRESHOLD_PCT,
    expected_team,
)

PG_CONN = dict(host="postgres", port=5432, user="airflow", password="airflow", dbname="airflow")
OS_HOST = {"host": "opensearch", "port": 9200}


def get_injection_config():
    """Read the failure-injection switch from Airflow Variables."""
    from airflow.models import Variable
    inject_type = Variable.get("inject_failure_type", default_var="none")
    inject_pct = float(Variable.get("inject_failure_pct", default_var="0"))
    return inject_type, inject_pct


@dag(schedule=None, start_date=datetime(2025, 1, 1), catchup=False, tags=["indexops"])
def indexops_pipeline():

    @task
    def extract_data(**context):
        """Read tickets from Postgres and open a pipeline_runs row for this DAG run.

        Source-side failures (missing_fields, duplicate_tickets) are injected here,
        because they must exist *before* validate_data sees the data.
        """
        inject_type, inject_pct = get_injection_config()
        dag_run_id = context["dag_run"].run_id

        conn = psycopg2.connect(**PG_CONN)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO pipeline_runs (dag_run_id, status, inject_type, inject_pct, started_at)
               VALUES (%s, %s, %s, %s, NOW()) RETURNING run_id""",
            (dag_run_id, "RUNNING", inject_type, inject_pct),
        )
        run_id = cur.fetchone()[0]
        conn.commit()

        cur.execute("SELECT ticket_id, description, category, priority, assigned_team FROM tickets")
        rows = cur.fetchall()
        conn.close()

        tickets = [{"ticket_id": r[0], "description": r[1], "category": r[2],
                    "priority": r[3], "assigned_team": r[4]} for r in rows]

        n_to_break = int(len(tickets) * inject_pct) if inject_pct > 0 else 0

        if inject_type == "missing_fields" and n_to_break:
            # Simulate a bad upstream form / bulk import: required fields arrive blank.
            for t in random.sample(tickets, n_to_break):
                t["category"] = None
                t["description"] = ""

        if inject_type == "duplicate_tickets" and n_to_break:
            # Simulate a retry loop without an idempotency check: same ticket_id twice.
            tickets.extend(dict(t) for t in random.sample(tickets, n_to_break))

        return {"run_id": run_id, "tickets": tickets}

    @task
    def validate_data(payload: dict):
        tickets = payload["tickets"]
        valid = [t for t in tickets if t["description"] and t["category"]]
        return {
            "run_id": payload["run_id"],
            "extracted_count": len(tickets),
            "dropped_count": len(tickets) - len(valid),
            "tickets": valid,
        }

    @task
    def transform_data(payload: dict):
        inject_type, inject_pct = get_injection_config()
        tickets = payload["tickets"]

        for t in tickets:
            t["indexed_team"] = expected_team(t["category"])

        if inject_type == "category_mapping_drift" and inject_pct > 0:
            n_to_break = int(len(tickets) * inject_pct)
            for t in random.sample(tickets, n_to_break):
                # Route to a real but wrong team, mimicking a bad mapping edit.
                wrong = [team for team in TEAMS_BY_CATEGORY.values() if team != t["indexed_team"]]
                t["indexed_team"] = random.choice(wrong)

        payload["tickets"] = tickets
        return payload
    
    @task
    def embed_and_index(payload: dict):
        """Generate embeddings and bulk-index in one task so vectors never hit XCom.

        The index is rebuilt on every run so it always reflects the current run's
        output (a full-refresh batch load).
        """
        from sentence_transformers import SentenceTransformer
        from opensearchpy import OpenSearch, helpers

        tickets = payload["tickets"]
        client = OpenSearch(hosts=[OS_HOST], use_ssl=False)

        if client.indices.exists(index=TICKETS_INDEX):
            client.indices.delete(index=TICKETS_INDEX)
        client.indices.create(index=TICKETS_INDEX, body={
            "mappings": {
                "properties": {
                    "ticket_id": {"type": "integer"},
                    "description": {"type": "text"},
                    "category": {"type": "keyword"},
                    "priority": {"type": "keyword"},
                    "assigned_team": {"type": "keyword"},
                    "embedding": {"type": "knn_vector", "dimension": EMBEDDING_DIM},
                }
            },
            "settings": {"index.knn": True},
        })

        model = SentenceTransformer(EMBEDDING_MODEL)
        vectors = model.encode([t["description"] for t in tickets], batch_size=64).tolist()

        actions = (
            {
                "_index": TICKETS_INDEX,
                "_id": t["ticket_id"],
                "_source": {
                    "ticket_id": t["ticket_id"],
                    "description": t["description"],
                    "category": t["category"],
                    "priority": t["priority"],
                    "assigned_team": t["indexed_team"],
                    "embedding": v,
                },
            }
            for t, v in zip(tickets, vectors)
        )
        success, _ = helpers.bulk(client, actions, chunk_size=500, request_timeout=120)
        client.indices.refresh(index=TICKETS_INDEX)
        return success

    @task
    def validate_index(payload: dict, indexed_count: int):
        """Ask OpenSearch how many docs really landed and record it in index_metrics."""
        from opensearchpy import OpenSearch
        client = OpenSearch(hosts=[OS_HOST], use_ssl=False)
        doc_count = client.count(index=TICKETS_INDEX)["count"]

        conn = psycopg2.connect(**PG_CONN)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO index_metrics (run_id, index_name, doc_count) VALUES (%s, %s, %s)",
            (payload["run_id"], TICKETS_INDEX, doc_count),
        )
        conn.commit()
        conn.close()
        return {"doc_count": doc_count, "bulk_indexed": indexed_count}

    @task
    def calculate_metrics(payload: dict, index_result: dict):
        run_id = payload["run_id"]
        tickets = payload["tickets"]

        mismatches = sum(1 for t in tickets if t["indexed_team"] != expected_team(t["category"]))
        unique_ids = {t["ticket_id"] for t in tickets}
        duplicate_count = len(tickets) - len(unique_ids)
        total_processed = payload["extracted_count"]
        mismatch_pct = (mismatches / len(tickets) * 100) if tickets else 0.0

        conn = psycopg2.connect(**PG_CONN)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO data_quality_metrics
               (run_id, total_processed, total_indexed, mismatch_count, mismatch_pct, duplicate_count)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, total_processed, index_result["doc_count"], mismatches, mismatch_pct, duplicate_count),
        )
        cur.execute(
            "UPDATE pipeline_runs SET status = %s, ended_at = NOW() WHERE run_id = %s",
            ("SUCCESS", run_id),
        )
        conn.commit()
        conn.close()

        return {
            "run_id": run_id,
            "total_processed": total_processed,
            "total_indexed": index_result["doc_count"],
            "dropped_count": payload["dropped_count"],
            "duplicate_count": duplicate_count,
            "mismatch_count": mismatches,
            "mismatch_pct": mismatch_pct,
        }

    @task
    def analyze_index_health(metrics: dict):
        """Raise alerts when data quality is bad even though the DAG itself succeeded."""
        run_id = metrics["run_id"]
        total = max(metrics["total_processed"], 1)
        dropped_pct = metrics["dropped_count"] / total * 100
        duplicate_pct = metrics["duplicate_count"] / total * 100

        print(f"Run {run_id}: mismatch_pct={metrics['mismatch_pct']:.2f}% "
              f"dropped={dropped_pct:.2f}% duplicates={duplicate_pct:.2f}%")

        alerts = []
        if metrics["mismatch_pct"] > MISMATCH_ALERT_THRESHOLD_PCT:
            severity = "critical" if metrics["mismatch_pct"] > 20 else "high"
            alerts.append((severity,
                           f"Category/team mismatch at {metrics['mismatch_pct']:.1f}% "
                           f"({metrics['mismatch_count']} tickets) exceeds "
                           f"{MISMATCH_ALERT_THRESHOLD_PCT:.0f}% threshold; pipeline reported SUCCESS."))
        if dropped_pct > MISMATCH_ALERT_THRESHOLD_PCT:
            alerts.append(("high",
                           f"{metrics['dropped_count']} tickets ({dropped_pct:.1f}%) dropped in "
                           f"validate_data for missing required fields."))
        if duplicate_pct > MISMATCH_ALERT_THRESHOLD_PCT:
            alerts.append(("medium",
                           f"{metrics['duplicate_count']} duplicate ticket_ids ({duplicate_pct:.1f}%) "
                           f"detected in the extracted batch."))

        if alerts:
            conn = psycopg2.connect(**PG_CONN)
            cur = conn.cursor()
            for severity, message in alerts:
                cur.execute(
                    "INSERT INTO alerts (run_id, severity, message, status) VALUES (%s, %s, %s, 'open')",
                    (run_id, severity, message),
                )
            conn.commit()
            conn.close()
            print(f"Raised {len(alerts)} alert(s) for run {run_id}.")
        else:
            print(f"Run {run_id} healthy; no alerts raised.")

    extracted = extract_data()
    validated = validate_data(extracted)
    transformed = transform_data(validated)
    indexed_count = embed_and_index(transformed)
    index_result = validate_index(transformed, indexed_count)
    metrics = calculate_metrics(transformed, index_result)
    analyze_index_health(metrics)


indexops_pipeline()
