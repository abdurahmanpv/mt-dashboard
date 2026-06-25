"""
daily_refresh.py
================
Daily automation entry point for the CEO Subscription Dashboard.

Execution order:
  1. Connect to MySQL
  2. Execute the full production query (full reload — no incremental)
  3. Call build_dashboard.build(df=df) to compute all KPIs and rebuild Excel
  4. Upload to SharePoint (if SP_SHARING_URL env var is set)

Schedule:
  GitHub Actions — see .github/workflows/daily_refresh.yml
    Runs at 3 AM PST (11:00 UTC) on GitHub's cloud runners.
    No local machine required.

Usage:
  python daily_refresh.py                    # full run (MySQL -> Dashboard)
  python daily_refresh.py --dry-run          # validate config only, do not execute
  python daily_refresh.py --output out.xlsx  # write dashboard to custom path
"""

import os
import sys
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── Try loading .env for credentials ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # pip install python-dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── CONFIGURATION (override via .env or environment variables) ─────────────────
DB_HOST     = os.getenv("DB_HOST",     "localhost")
DB_PORT     = int(os.getenv("DB_PORT", "3306"))
DB_USER     = os.getenv("DB_USER",     "your_db_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "your_db_password")
DB_NAME     = os.getenv("DB_NAME",     "WAY_SUBSCRIPTIONS")

OUTPUT_FILE = os.getenv("OUTPUT_FILE", "CEO_Subscription_Dashboard.xlsx")
SQL_FILE    = os.getenv("SQL_FILE",    "mileage_tracker_subscriptions.sql")


def load_sql(sql_file: str) -> str:
    """
    Load the production SQL query from the .sql file.
    Using an external .sql file means you can update the query without
    changing Python code.
    """
    path = Path(sql_file)
    if not path.exists():
        raise FileNotFoundError(
            f"SQL file not found: {sql_file}\n"
            f"Expected at: {path.resolve()}"
        )
    return path.read_text(encoding="utf-8")


def run(output: str):
    """
    Full refresh pipeline: MySQL -> KPIs -> Dashboard Excel.

    Parameters
    ----------
    output  : path to write finished dashboard
    """
    start = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("  CEO DASHBOARD DAILY REFRESH")
    log.info(f"  Run started: {start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("=" * 60)

    # ── Step 1: Fetch from MySQL ───────────────────────────────────────────────
    log.info("Step 1/3 — Fetching data from MySQL ...")
    from data_loader import load_from_mysql

    query = load_sql(SQL_FILE)
    log.info(f"  SQL loaded from {SQL_FILE} ({len(query)} chars)")

    df = load_from_mysql(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, query=query,
    )
    log.info(f"  Fetched {len(df):,} rows, {df['user_id'].nunique():,} unique users")

    # ── Step 2: Build dashboard ────────────────────────────────────────────────
    log.info("Step 2/3 — Building dashboard ...")
    from build_dashboard import build
    out = build(df=df, output=output)

    # ── Step 3: Upload to SharePoint ──────────────────────────────────────────
    sp_url = os.getenv("SP_SHARING_URL")
    if sp_url:
        log.info("Step 3/3 — Uploading to SharePoint ...")
        from sharepoint_upload import upload_to_sharepoint
        upload_to_sharepoint(out)
    else:
        log.info("Step 3/3 — SP_SHARING_URL not set, skipping SharePoint upload.")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("=" * 60)
    log.info(f"  REFRESH COMPLETE  ->  {out}")
    log.info(f"  Total elapsed: {elapsed:.1f}s")
    log.info("=" * 60)


def main():
    p = argparse.ArgumentParser(description="CEO Dashboard Daily Refresh")
    p.add_argument("--output",   default=OUTPUT_FILE,
                   help=f"Path to write the output dashboard (default: {OUTPUT_FILE})")
    p.add_argument("--skip-db",  action="store_true",
                   help="(Deprecated) skip-db is no longer supported without a source file.")
    p.add_argument("--dry-run",  action="store_true",
                   help="Print config and exit without running")
    args = p.parse_args()

    if args.skip_db:
        log.error(
            "--skip-db is no longer supported. The ref Excel file has been removed. "
            "Run a full refresh (python daily_refresh.py) to rebuild from MySQL."
        )
        sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN — config check only")
        log.info(f"  DB_HOST    : {DB_HOST}")
        log.info(f"  DB_PORT    : {DB_PORT}")
        log.info(f"  DB_NAME    : {DB_NAME}")
        log.info(f"  DB_USER    : {DB_USER}")
        log.info(f"  OUTPUT     : {args.output}")
        log.info(f"  SQL_FILE   : {SQL_FILE}")
        try:
            sql = load_sql(SQL_FILE)
            log.info(f"  SQL length : {len(sql)} chars  OK")
        except FileNotFoundError as e:
            log.error(str(e))
        return

    run(output=args.output)


if __name__ == "__main__":
    main()
