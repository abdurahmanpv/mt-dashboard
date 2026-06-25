"""
build_dashboard.py
==================
Main orchestrator for the CEO Subscription Dashboard.

Execution flow:
  1. Load config thresholds from the existing ⚙️ Config sheet
     (preserves user edits across rebuilds)
  2. Read existing Monthly Active Snapshots from the current output file
  3. Compute all KPIs via kpi_engine
  4. Build each sheet via sheet builders
  5. Persist updated snapshot into the new workbook
  6. Save the workbook

Called by daily_refresh.py after MySQL data has been loaded.
Do NOT run standalone — always run via daily_refresh.py.
"""

import os
import argparse
import logging
from datetime import datetime, timezone
import openpyxl

# ── Local modules ──────────────────────────────────────────────────────────────
from data_loader   import (update_active_snapshot, write_snapshot_sheet)
from kpi_engine    import compute_all_kpis, YESTERDAY, MTD_START, PLAN_SEGMENTS, get_open_trial_rows, get_churned_rows
from sheet_executive import build_executive_dashboard
from sheet_builders  import (build_funnel_sheet, build_plan_performance_sheet,
                              build_retention_sheet, build_raw_data_sheet,
                              build_active_subscriptions_sheet,
                              build_trial_pipeline_sheet,
                              build_churn_raw_data_sheet)
from build_validation_guide  import build_validation_guide

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Default output path ────────────────────────────────────────────────────────
DEFAULT_OUTPUT = "CEO_Subscription_Dashboard.xlsx"

# ── Desired sheet order in the final workbook ──────────────────────────────────
SHEET_ORDER = [
    "Executive Dashboard",
    "Plan Performance",
    "Retention & Renewals",
    "Active Subscriptions",
    "Trial Pipeline",
    "Churn Raw Data",
    "Raw Data",
    "Monthly Active Snapshots",
]


def build(df=None, output: str = DEFAULT_OUTPUT) -> str:
    """
    Full dashboard build pipeline.

    Parameters
    ----------
    df     : pd.DataFrame of raw subscription data (required).
             Pass None only if you intentionally want to see the error.
    output : path to write the finished dashboard workbook

    Returns
    -------
    str : path to the saved output file
    """
    import pandas as pd

    if df is None:
        raise ValueError(
            "build() requires a DataFrame — run via daily_refresh.py. "
            "Standalone execution without a source file is no longer supported."
        )

    start = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info("  CEO SUBSCRIPTION DASHBOARD  —  BUILD STARTED")
    log.info("=" * 60)

    existing_config = {}

    # ── Step 1: (snapshot is fully regenerated each run — no prior read needed) ─

    # ── Step 2a: Exclude QA test subscriptions from all metrics ──────────────
    # Test instances are identified by any order whose refund_reason contains
    # the word "test" (case-insensitive). The entire subscription is excluded —
    # not just the tagged order — so test accounts don't bleed into active counts,
    # churn, revenue, etc. Raw Data is unaffected (still uses the full df).
    if "refund_reason" in df.columns:
        test_sub_ids = set(
            df[df["refund_reason"].fillna("").str.contains("test", case=False, na=False)]
            ["subscription_id"].unique()
        )
        if test_sub_ids:
            log.info(f"  Excluding {len(test_sub_ids)} QA test subscription(s) from KPI calculations")
        df_kpi = df[~df["subscription_id"].isin(test_sub_ids)].copy()
    else:
        df_kpi = df.copy()

    # ── Step 2b: Restrict to defined plan segments only ──────────────────────
    # Only Free Monthly, Lite Monthly, Lite Yearly, Unlimited Monthly, and
    # Unlimited Yearly are tracked in the dashboard. Any order whose plan_segment
    # falls outside this list (e.g. legacy or promotional plans) is excluded from
    # all KPI calculations. Raw Data is unaffected (still uses the full df).
    # Trial orders inherit their plan_segment from subscription_name + duration —
    # a Lite Monthly trial has plan_segment = "Lite Monthly" and is kept; a trial
    # for a plan not in this list is excluded.
    if "plan_segment" in df_kpi.columns:
        before = len(df_kpi)
        df_kpi = df_kpi[df_kpi["plan_segment"].isin(PLAN_SEGMENTS)].copy()
        excluded = before - len(df_kpi)
        if excluded:
            log.info(f"  Excluding {excluded:,} order row(s) for out-of-scope plan segments")

    # ── Step 3: Compute all KPIs ───────────────────────────────────────────────
    log.info(f"Step 2/5 — Computing KPIs  "
             f"({len(df_kpi):,} rows | {df_kpi['user_id'].nunique():,} users | "
             f"{df_kpi['subscribed_date'].min()} → {df_kpi['subscribed_date'].max()}) ...")
    kpi = compute_all_kpis(df_kpi, existing_config)
    log.info(f"  Active subs: {kpi['active']['active_now']:,}  |  "
             f"MTD new subs: {kpi['subscription_types']['new_subs']['mtd']:,}  |  "
             f"MTD cash revenue: ${kpi['revenue']['mtd']['cash']:,.2f}  |  "
             f"Paid churn rate MTD: {kpi['paid_churn']['mtd']['rate']:.1%}")

    # ── Step 4: Regenerate snapshot (all months, fresh every run) ─────────────
    log.info("Step 3/5 — Regenerating active snapshot ...")
    # Filter to Confirmed/Completed only — consistent with compute_all_kpis (df_clean).
    df_kpi_clean = df_kpi[df_kpi["order_status"].isin(["Confirmed", "Completed"])].copy() if "order_status" in df_kpi.columns else df_kpi.copy()
    updated_snapshot_df = update_active_snapshot(df_kpi_clean, MTD_START)

    # ── Step 5: Build workbook ─────────────────────────────────────────────────
    log.info("Step 4/5 — Building workbook ...")
    wb = openpyxl.Workbook()
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    log.info("  Building Executive Dashboard ...")
    build_executive_dashboard(wb, kpi)

    log.info("  Building Plan Performance ...")
    build_plan_performance_sheet(wb, kpi)

    log.info("  Building Retention & Renewals ...")
    build_retention_sheet(wb, kpi)

    log.info("  Building Active Subscriptions ...")
    build_active_subscriptions_sheet(wb, kpi)

    log.info("  Building Trial Pipeline ...")
    trial_pipeline_df = get_open_trial_rows(df_kpi)
    log.info(f"  Trial Pipeline: {len(trial_pipeline_df)} open trial(s)")
    build_trial_pipeline_sheet(wb, trial_pipeline_df)

    log.info("  Building Churn Raw Data ...")
    churn_data = get_churned_rows(df_kpi)
    log.info(f"  Churn Raw Data: {len(churn_data['paid'])} paid churned, "
             f"{len(churn_data['trial'])} trial churned (MTD)")
    build_churn_raw_data_sheet(wb, churn_data)

    log.info(f"  Building Raw Data ({len(df):,} rows) ...")
    df_raw = df[df["subscribed_date"] <= YESTERDAY].copy()
    log.info(f"  Raw Data: {len(df_raw):,} rows after capping at {YESTERDAY}")
    build_raw_data_sheet(wb, df_raw)

    log.info("  Writing Monthly Active Snapshots sheet ...")
    write_snapshot_sheet(wb, updated_snapshot_df)

    # ── Step 6: Save dashboard ─────────────────────────────────────────────────
    log.info(f"Step 5/5 — Saving to {output} ...")
    wb.save(output)

    # ── Step 7: Rebuild validation guide ──────────────────────────────────────
    log.info("  Rebuilding validation guide ...")
    guide_path = build_validation_guide(output)
    log.info(f"  Validation guide -> {guide_path}")

    # ── Step 8: Build HTML dashboard ─────────────────────────────────────────
    html_output = str(output).replace(".xlsx", ".html")
    try:
        from build_html_dashboard import build_html
        html_path = build_html(
            df_kpi_clean=df_kpi_clean,
            kpi=kpi,
            trial_pipeline_df=trial_pipeline_df,
            churn_data=churn_data,
            output_path=html_output,
            df_raw=df_raw,
        )
        log.info(f"  HTML dashboard   -> {html_path}")
    except Exception as exc:
        import traceback
        log.warning(f"  HTML dashboard generation failed (non-fatal): {exc}")
        log.warning(traceback.format_exc())

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("=" * 60)
    log.info(f"  DONE  ->  {output}  [{elapsed:.1f}s]")
    log.info("=" * 60)

    return output


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build CEO Subscription Dashboard")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help=f"Output path (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args()
    print("build_dashboard.py cannot be run standalone. Use daily_refresh.py.")
