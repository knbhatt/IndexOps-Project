"""Seed Postgres tickets from KameronB/synthetic-it-callcenter-tickets.

Downloads the CSV into data/raw/ on first run, maps their categories onto the
IndexOps contract (Network/Hardware/Software/Access/Email), then inserts a
stratified sample so demos are not Software-only.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from indexops.contract import (  # noqa: E402
    CATEGORIES,
    PRIORITIES,
    TEAMS_BY_CATEGORY,
)

RAW_DIR = Path(__file__).resolve().parent / "raw"
DEFAULT_CSV = RAW_DIR / "kameronb_sitcc.csv"
HF_CSV_URL = (
    "https://huggingface.co/datasets/KameronB/synthetic-it-callcenter-tickets"
    "/resolve/main/synthetic-it-call-center-tickets.csv"
)

STATUSES = ["todo", "in_progress", "done"]
STATUS_WEIGHTS = [0.45, 0.35, 0.20]

# Direct map from KameronB `category` → IndexOps contract.
CATEGORY_MAP = {
    "SOFTWARE": "Software",
    "ACCOUNT": "Access",
    "PIV CARD": "Access",
    "EMAIL": "Email",
    "SECURITY": "Access",
    "PRINTER": "Hardware",
    "NETWORK": "Network",
    "HARDWARE": "Hardware",
}

# Keyword overrides (applied first) recover Network/Hardware/Email/Access from
# the SOFTWARE-heavy source distribution.
_EMAIL_KW = (
    "outlook", "exchange", "mailbox", "email", "o365", "office 365", "smtp",
    "mail client", "inbox",
)
_NETWORK_KW = (
    "vpn", "wifi", "wi-fi", "network", "dns", "firewall", "lan", "wan",
    "proxy", "connectivity", "ethernet", "ip address",
)
_HARDWARE_KW = (
    "laptop", "keyboard", "mouse", "monitor", "dock", "printer", "headset",
    "webcam", "battery", "desktop", "hardware", "docking station",
)
_ACCESS_KW = (
    "password", "mfa", "sso", "okta", "login", "locked out", "permission",
    "access denied", "piv", "badge", "unlock account", "reset password",
    "two-factor", "2fa",
)


def map_category(raw_category: str | None, subcategory: str | None, text: str) -> str:
    """Map a KameronB row onto one of the five IndexOps categories."""
    blob = (text or "").lower()
    if any(k in blob for k in _EMAIL_KW):
        return "Email"
    if any(k in blob for k in _NETWORK_KW):
        return "Network"
    if any(k in blob for k in _HARDWARE_KW):
        return "Hardware"
    if any(k in blob for k in _ACCESS_KW):
        return "Access"

    mapped = CATEGORY_MAP.get((raw_category or "").strip().upper())
    if mapped:
        return mapped
    if (subcategory or "").strip().upper() == "ACCESS":
        return "Access"
    return "Software"


def ensure_csv(path: Path) -> Path:
    """Download the KameronB CSV into data/raw/ if it is not already present."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 1_000_000:
        print(f"Using cached KameronB CSV ({path.stat().st_size / 1e6:.1f} MB) -> {path}")
        return path
    print(f"Downloading KameronB dataset -> {path} ...")
    urllib.request.urlretrieve(HF_CSV_URL, path)
    print(f"Downloaded {path.stat().st_size / 1e6:.1f} MB")
    return path


def load_mapped_rows(csv_path: Path) -> dict[str, list[dict]]:
    """Load CSV and bucket rows by mapped IndexOps category."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    with csv_path.open(encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            short = (row.get("short_description") or "").strip()
            content = (row.get("content") or "").strip()
            if not short and not content:
                continue
            text = f"{short} {content}".strip()
            category = map_category(row.get("category"), row.get("subcategory"), text)
            # Prefer readable board text: short description, with body for embeddings.
            if short and content:
                description = f"{short}\n\n{content}"
            else:
                description = short or content
            # Cap extreme bodies so embed/index stays snappy.
            if len(description) > 1200:
                description = description[:1197] + "..."
            buckets[category].append(
                {
                    "description": description,
                    "category": category,
                    "assigned_team": TEAMS_BY_CATEGORY[category],
                }
            )
    return buckets


def stratified_sample(buckets: dict[str, list[dict]], count: int, rng: random.Random) -> list[dict]:
    """Draw ~equal shares per category; oversample minorities if needed."""
    if count <= 0:
        return []
    per = count // len(CATEGORIES)
    remainder = count % len(CATEGORIES)
    picked: list[dict] = []
    for i, cat in enumerate(CATEGORIES):
        need = per + (1 if i < remainder else 0)
        pool = buckets.get(cat) or []
        if not pool:
            # Extremely unlikely after keyword mapping; fall back to Software.
            pool = buckets.get("Software") or next(iter(buckets.values()))
        if need <= len(pool):
            picked.extend(rng.sample(pool, need))
        else:
            picked.extend(rng.choices(pool, k=need))
    rng.shuffle(picked)
    return picked


def insert_tickets(rows: list[dict], truncate: bool) -> int:
    conn = psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        user=os.getenv("PG_USER", "airflow"),
        password=os.getenv("PG_PASSWORD", "airflow"),
        dbname=os.getenv("PG_DB", "airflow"),
    )
    cur = conn.cursor()
    if truncate:
        cur.execute("TRUNCATE tickets RESTART IDENTITY CASCADE")
    for row in rows:
        status = random.choices(STATUSES, weights=STATUS_WEIGHTS, k=1)[0]
        priority = random.choice(PRIORITIES)
        cur.execute(
            """INSERT INTO tickets (description, category, priority, assigned_team, status)
               VALUES (%s, %s, %s, %s, %s)""",
            (row["description"], row["category"], priority, row["assigned_team"], status),
        )
    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed tickets from KameronB synthetic IT dataset")
    p.add_argument("--count", type=int, default=3000, help="Number of tickets to insert (default 3000)")
    p.add_argument("--truncate", action="store_true", help="Wipe tickets table before insert")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to KameronB CSV")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for reproducible sampling")
    p.add_argument(
        "--no-stratify",
        action="store_true",
        help="Sample uniformly from all mapped rows (Software-heavy)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.count < 1:
        raise SystemExit("--count must be >= 1")

    random.seed(args.seed)
    rng = random.Random(args.seed)

    csv_path = ensure_csv(args.csv)
    buckets = load_mapped_rows(csv_path)
    total_source = sum(len(v) for v in buckets.values())
    print("Mapped source rows by category:")
    for cat in CATEGORIES:
        print(f"  {cat}: {len(buckets.get(cat, []))}")

    if args.no_stratify:
        flat = [row for cat in CATEGORIES for row in buckets.get(cat, [])]
        if args.count <= len(flat):
            rows = rng.sample(flat, args.count)
        else:
            rows = rng.choices(flat, k=args.count)
        rng.shuffle(rows)
    else:
        rows = stratified_sample(buckets, args.count, rng)

    n = insert_tickets(rows, truncate=args.truncate)
    print(
        f"Inserted {n} tickets from KameronB "
        f"(source={total_source}, stratify={not args.no_stratify}, truncate={args.truncate})."
    )


if __name__ == "__main__":
    main()
