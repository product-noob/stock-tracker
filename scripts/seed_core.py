"""Seed core.companies + instruments + listings + aliases + whitelist + source
partitions into the stock_market database.

Idempotent: safe to re-run. Existing rows are detected by natural keys and
skipped; only missing rows are inserted. Everything runs in ONE transaction --
any failure rolls back the whole run.

Usage:
    python scripts/seed_core.py

Requires DATABASE_URL in .env (postgresql://stock_market_app:...@localhost:5432/stock_market).
"""
import json
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent))
from companies import COMPANIES

ROOT = Path(__file__).parent.parent

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def load_database_url() -> str:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    url = env.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL missing from .env")
    return url


def validate_registry():
    """Catch bad rows before anything touches the database."""
    errors, seen_isin, seen_nse, seen_bse = [], {}, {}, {}
    for slug, co in COMPANIES.items():
        if not ISIN_RE.fullmatch(co.isin):
            errors.append(f"{slug}: bad ISIN {co.isin!r}")
        if co.nse_symbol != co.nse_symbol.upper() or " " in co.nse_symbol:
            errors.append(f"{slug}: NSE symbol must be uppercase, no spaces ({co.nse_symbol!r})")
        if not re.fullmatch(r"\d{6}", co.bse_scrip):
            errors.append(f"{slug}: BSE scrip must be 6 digits ({co.bse_scrip!r})")
        for key, seen in (("ISIN", seen_isin), ("NSE", seen_nse), ("BSE", seen_bse)):
            val = {"ISIN": co.isin, "NSE": co.nse_symbol, "BSE": co.bse_scrip}[key]
            if val in seen:
                errors.append(f"{slug}: {key} {val} already used by {seen[val]}")
            seen[val] = slug
    if errors:
        for e in errors:
            print(f"VALIDATION ERROR: {e}")
        raise SystemExit("registry validation failed; nothing was written")


def fetch_one(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchone()


def seed(conn):
    stats = {"companies": 0, "instruments": 0, "listings": 0,
             "aliases": 0, "whitelist": 0, "partitions": 0}
    skipped_prefix = "  exists:"
    company_ids = {}

    # Global feed-wide partitions (not tied to any one company)
    global_parts = [
        ("nse_rss", "Online_announcements.xml"),
        ("bse_rss", "corporate-filings.rss"),
    ]
    for source, key in global_parts:
        row = fetch_one(cur, "SELECT 1 FROM ingestion.source_partitions WHERE source=%s AND partition_key=%s", (source, key))
        if row is None:
            cur.execute(
                """INSERT INTO ingestion.source_partitions (source, partition_key, description)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (source, key, f"Exchange-wide RSS stream: {key}"))
            stats["partitions"] += 1
            print(f"+ partition {source}/{key}")
        else:
            print(f"{skipped_prefix} partition {source}/{key}")

    for slug, co in COMPANIES.items():
        # ---- company ------------------------------------------------------
        row = fetch_one(cur, "SELECT company_id FROM core.companies WHERE lower(canonical_name)=lower(%s)", (co.name,))
        if row:
            company_id = row[0]
            print(f"{skipped_prefix} company {co.name} (id={company_id})")
        else:
            cur.execute("INSERT INTO core.companies (canonical_name) VALUES (%s) RETURNING company_id", (co.name,))
            company_id = cur.fetchone()[0]
            stats["companies"] += 1
            print(f"+ company {co.name} (id={company_id})")
        company_ids[slug] = company_id

        # ---- instrument ---------------------------------------------------
        row = fetch_one(cur, "SELECT instrument_id FROM core.instruments WHERE isin=%s", (co.isin,))
        if row:
            instrument_id = row[0]
            print(f"{skipped_prefix} instrument ISIN {co.isin} (id={instrument_id})")
        else:
            cur.execute(
                """INSERT INTO core.instruments (company_id, security_name, instrument_type, isin)
                   VALUES (%s, %s, 'equity', %s) RETURNING instrument_id""",
                (company_id, co.name, co.isin))
            instrument_id = cur.fetchone()[0]
            stats["instruments"] += 1
            print(f"+ instrument ISIN {co.isin} (id={instrument_id})")

        # ---- listings (one per exchange venue) ----------------------------
        row = fetch_one(
            cur,
            "SELECT listing_id FROM core.listings WHERE exchange='NSE' AND exchange_symbol=%s AND valid_to IS NULL",
            (co.nse_symbol,))
        if row:
            print(f"{skipped_prefix} NSE listing {co.nse_symbol}")
        else:
            cur.execute(
                """INSERT INTO core.listings
                       (instrument_id, company_id, exchange, exchange_symbol, currency, provider_symbols)
                   VALUES (%s, %s, 'NSE', %s, 'INR', %s)""",
                (instrument_id, company_id, co.nse_symbol,
                 json.dumps({"yfinance": co.yahoo_symbol})))
            stats["listings"] += 1
            print(f"+ NSE listing {co.nse_symbol}")

        row = fetch_one(
            cur,
            "SELECT listing_id FROM core.listings WHERE exchange='BSE' AND exchange_code=%s AND valid_to IS NULL",
            (co.bse_scrip,))
        if row:
            print(f"{skipped_prefix} BSE listing {co.bse_scrip}")
        else:
            cur.execute(
                """INSERT INTO core.listings
                       (instrument_id, company_id, exchange, exchange_code, currency)
                   VALUES (%s, %s, 'BSE', %s, 'INR')""",
                (instrument_id, company_id, co.bse_scrip))
            stats["listings"] += 1
            print(f"+ BSE listing {co.bse_scrip}")

        # ---- aliases (common names + the NSE symbol itself) ---------------
        alias_rows = [(a, "common_name") for a in co.aliases] + [(co.nse_symbol, "symbol")]
        for raw, kind in alias_rows:
            value = raw.strip().strip('"')
            normalized = " ".join(value.lower().split())
            row = fetch_one(
                cur,
                """SELECT 1 FROM core.company_aliases
                   WHERE company_id=%s AND alias_kind=%s AND normalized_value=%s""",
                (company_id, kind, normalized))
            if row:
                continue
            cur.execute(
                """INSERT INTO core.company_aliases (company_id, alias_value, normalized_value, alias_kind, source)
                   VALUES (%s, %s, %s, %s, 'seed_core.py')""",
                (company_id, value, normalized, kind))
            stats["aliases"] += 1
        print(f"  aliases ok ({slug}: {len(alias_rows)} checked)")

        # ---- whitelist membership ----------------------------------------
        row = fetch_one(
            cur,
            "SELECT 1 FROM core.whitelist_memberships WHERE company_id=%s AND removed_at IS NULL",
            (company_id,))
        if row:
            print(f"{skipped_prefix} whitelist membership {slug}")
        else:
            cur.execute(
                "INSERT INTO core.whitelist_memberships (company_id, reason) VALUES (%s, %s)",
                (company_id, "SENSEX 30 initial whitelist"))
            stats["whitelist"] += 1
            print(f"+ whitelist membership {slug}")

        # ---- per-company source partitions ---------------------------------
        parts = [
            ("nse_api", f"symbol:{co.nse_symbol}", f"NSE announcements API: {co.nse_symbol}"),
            ("bse_api", f"scrip:{co.bse_scrip}", f"BSE announcements API scrip {co.bse_scrip} ({slug})"),
            ("google_news", f"company:{company_id}", f"Google News RSS query for company id {company_id} ({slug})"),
        ]
        for source, key, desc in parts:
            row = fetch_one(
                cur,
                "SELECT 1 FROM ingestion.source_partitions WHERE source=%s AND partition_key=%s",
                (source, key))
            if row:
                continue
            cur.execute(
                """INSERT INTO ingestion.source_partitions (source, partition_key, description, company_id)
                   VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                (source, key, desc, company_id))
            stats["partitions"] += 1

    return stats


if __name__ == "__main__":
    validate_registry()
    url = load_database_url()
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            stats = seed(cur)
        conn.commit()
    print("\n=== SEED COMPLETE ===")
    print(json.dumps(stats, indent=2))
