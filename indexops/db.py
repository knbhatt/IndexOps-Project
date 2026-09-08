"""Small helpers for Postgres and OpenSearch access shared by all tools."""
import json
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
from opensearchpy import OpenSearch

from indexops.config import PG, OPENSEARCH


@contextmanager
def pg_cursor(commit: bool = False):
    """Yield a RealDictCursor; commit on success if requested."""
    conn = psycopg2.connect(**PG)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def fetch_all(sql: str, params=None) -> list[dict]:
    with pg_cursor() as cur:
        cur.execute(sql, params)
        return [jsonable(dict(r)) for r in cur.fetchall()]


def fetch_one(sql: str, params=None) -> dict | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=None) -> dict | None:
    """Run a write statement; returns the first row if the statement RETURNs one."""
    with pg_cursor(commit=True) as cur:
        cur.execute(sql, params)
        try:
            row = cur.fetchone()
        except psycopg2.ProgrammingError:
            row = None
        return jsonable(dict(row)) if row else None


def os_client() -> OpenSearch:
    return OpenSearch(hosts=[OPENSEARCH], use_ssl=False, timeout=30)


def jsonable(obj):
    """Make DB rows JSON-serialisable (datetimes, Decimals, nested dicts)."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def dumps(obj) -> str:
    return json.dumps(jsonable(obj), default=str)
