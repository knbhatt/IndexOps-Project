"""HTTP helpers with retry/backoff for Airflow REST calls (L2 review: should-fix).

Retries connection errors for all methods. GET also retries read timeouts and 502/503/504.
POST never retries a read timeout or 5xx, because the server may already have acted
(e.g. triggered a DAG run) and a retry could duplicate it.
"""
from __future__ import annotations

import time

import requests

RETRY_STATUS = {502, 503, 504}


def request_with_retry(method: str, url: str, *, retries: int = 3, backoff: float = 1.0, **kwargs):
    kwargs.setdefault("timeout", 30)
    is_get = method.upper() == "GET"
    for attempt in range(retries + 1):
        last = attempt == retries
        try:
            r = requests.request(method, url, **kwargs)
        except requests.ConnectionError:
            if last:
                raise
        except requests.Timeout:
            if last or not is_get:
                raise
        else:
            if not (is_get and r.status_code in RETRY_STATUS and not last):
                return r
        time.sleep(backoff * (2 ** attempt))


def http_get(url: str, **kwargs):
    return request_with_retry("GET", url, **kwargs)


def http_post(url: str, **kwargs):
    return request_with_retry("POST", url, **kwargs)
