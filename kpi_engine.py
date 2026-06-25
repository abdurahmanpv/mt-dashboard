"""
kpi_engine.py
=============
Computes all KPIs for the CEO Subscription Dashboard.

This module is intentionally kept separate from display logic so you can:
  - Unit-test KPI logic independently
  - Reuse computations across multiple output formats
  - Modify a single metric without touching the Excel builder

All functions receive a pandas DataFrame (the full raw data from MySQL)
and return plain Python dicts, lists, or scalars — no Excel/openpyxl objects.

Confirmed business rules (as discussed):
  - Grain          : one row per order_identifier (billing event)
  - trial_check    : FREE | PAID | TRIAL  (from SQL)
  - subscription_type : Trial | New Subscription | Renewal  (from SQL v2)
  - plan_status    : Active | In-Active
  - Active logic   : USB_PaidTill >= today AND (USB_EndDateTime IS NULL OR >= today)
                     AND USB_Status = Active  (applied in SQL curr_status column)
  - Paid Churn     : paid subs (trial_check=PAID) with paid_till in window
                     AND curr_status=In-Active ÷ paid subs active at period start
  - Paid Retention : 1 − Paid Churn Rate
  - Trial→Paid     : paid orders in window on subscriptions with prior trial
                     ÷ trials whose trial_end (paid_till or +7d) falls in window
  - Trial Pipeline : trials started last 6 days not yet converted (point-in-time)
  - Free→Paid      : paid orders in window by users with prior FREE order
                     ÷ free users with paid_till >= start of the period
  - Revenue        : cash collected (total_amt) primary; MRR (mrr_amount) secondary
  - Time windows   : Yesterday, MTD, Prev Month Full, MTD vs Same Days Last Month,
                     YTD, Lifetime
  - Trend          : last 12 full months
"""

import logging
import pandas as pd
import numpy as np
from datetime import date, timedelta, datetime as _dt

log = logging.getLogger(__name__)


# ── PACIFIC TIMEZONE SETUP ─────────────────────────────────────────────────────
# All "today" references are pinned to America/Los_Angeles so the dashboard
# represents the last full Pacific calendar day correctly regardless of:
#   - The server's local clock (UTC on most Linux/cloud hosts)
#   - DST transitions (PST = UTC-8 in winter, PDT = UTC-7 in summer)
#
# Without this, date.today() on a UTC server returns the UTC date, which is
# one day ahead of Pacific time between midnight UTC and ~07:00–08:00 UTC
# (i.e. late-afternoon Pacific).  All KPI windows would shift by one day.
try:
    from zoneinfo import ZoneInfo                    # Python 3.9+ — stdlib
    _PACIFIC = ZoneInfo("America/Los_Angeles")
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo      # pip install backports.zoneinfo
        _PACIFIC = ZoneInfo("America/Los_Angeles")
    except ImportError:
        import pytz                                  # pip install pytz
        _PACIFIC = pytz.timezone("America/Los_Angeles")


def _pacific_today() -> date:
    """Return the current date in America/Los_Angeles — DST-safe."""
    return _dt.now(_PACIFIC).date()


# ── DATE CONSTANTS ─────────────────────────────────────────────────────────────
# Computed once per process run in Pacific local time, not the server clock.
# This ensures YESTERDAY == the last full Pacific calendar day regardless of
# whether the host runs UTC and regardless of PST ↔ PDT clock changes.

TODAY         = _pacific_today()
YESTERDAY     = TODAY - timedelta(days=1)
MTD_START     = TODAY.replace(day=1)
YTD_START     = TODAY.replace(month=1, day=1)

# Previous month: last day of previous month and its first day
PREV_MONTH_END   = MTD_START - timedelta(days=1)
PREV_MONTH_START = PREV_MONTH_END.replace(day=1)

# Same-period last month: match the number of days elapsed so far this month
# e.g. if today is June 8 and yesterday is June 7, compare June 1-7 vs May 1-7
DAYS_ELAPSED_MTD  = YESTERDAY.day  # complete Pacific days elapsed (excludes today)
PREV_MTD_END      = PREV_MONTH_START + timedelta(days=DAYS_ELAPSED_MTD - 1)

# Trial completeness threshold: trials started 7+ days ago (window has fully elapsed)
TRIAL_CUTOFF = TODAY - timedelta(days=7)
# Trial pipeline start: trials started MORE RECENTLY than TRIAL_CUTOFF are still in-window
PIPELINE_START = TRIAL_CUTOFF + timedelta(days=1)


# ── COMPACT DATE LABEL HELPER ──────────────────────────────────────────────────

def _fmt_compact(d: date) -> str:
    """Return 'Jun 7' style label for a single date."""
    return f"{d.strftime('%b')} {d.day}"


def compact_range(start: date, end: date) -> str:
    """
    Return a compact human-readable date range string.

    Same month  : 'Jun 1–7'
    Cross-month : 'May 15–Jun 7'
    Same day    : 'Jun 7'
    """
    if start == end:
        return _fmt_compact(start)
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{_fmt_compact(start)}–{_fmt_compact(end)}"


# ══════════════════════════════════════════════════════════════════════════════
#  SLICE HELPERS
#  These helpers return filtered subsets of the dataframe for a given window.
#  Using helpers keeps the KPI functions readable and avoids repetition.
# ══════════════════════════════════════════════════════════════════════════════

def _in_window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Return rows where subscribed_date falls within [start, end] inclusive."""
    return df[(df["subscribed_date"] >= start) & (df["subscribed_date"] <= end)]

def _yesterday(df):    return _in_window(df, YESTERDAY, YESTERDAY)
def _mtd(df):          return _in_window(df, MTD_START,  YESTERDAY)   # through last full Pacific day
def _prev_full(df):    return _in_window(df, PREV_MONTH_START, PREV_MONTH_END)
def _prev_same(df):    return _in_window(df, PREV_MONTH_START, PREV_MTD_END)
def _ytd(df):          return _in_window(df, YTD_START,   YESTERDAY)   # through last full Pacific day


def _windows(df: pd.DataFrame) -> dict:
    """
    Return a dict of named slices for a dataframe.
    Allows: windows = _windows(paid_df); windows["mtd"] gives MTD paid rows.

    ALL windows use YESTERDAY as the upper bound for consistency — including
    "lifetime", which means "all historical data through the last complete day".
    Today's partial-day rows are excluded everywhere.
    """
    return {
        "yesterday" : _yesterday(df),
        "mtd"       : _mtd(df),
        "prev_full" : _prev_full(df),
        "prev_same" : _prev_same(df),
        "ytd"       : _ytd(df),
        "lifetime"  : df[df["subscribed_date"] <= YESTERDAY],  # through last complete Pacific day
    }


def _counts(df: pd.DataFrame) -> dict:
    """
    For a given dataframe, return counts for all time windows.
    Returns: {yesterday, mtd, prev_full, prev_same, ytd, lifetime}
    """
    w = _windows(df)
    return {k: len(v) for k, v in w.items()}


def _revenue(df: pd.DataFrame) -> dict:
    """
    For a given PAID dataframe, return cash collected and accrual-based MRR
    for all time windows.

    MRR methodology (confirmed):
      Monthly plans : full total_amt for orders in the window.
      Yearly plans  : 1/12 of total_amt for each month of the 12-month recognition
                      window. For a reporting window [start, end], include all yearly
                      orders placed in the 12 months ending at window_end_month.

    Returns nested dict: {window: {cash, mrr}}
    """
    import numpy as _np

    has_duration = "duration" in df.columns
    if has_duration:
        dur_lower = df["duration"].astype(str).str.lower()
        monthly_df = df[~dur_lower.str.contains("year")]
        yearly_df  = df[dur_lower.str.contains("year")]
    else:
        # Fallback: use pre-computed mrr_amount column
        monthly_df = df
        yearly_df  = df.iloc[0:0]  # empty

    # Pre-compute year-month integer for yearly orders (vectorised)
    if not yearly_df.empty:
        dates = pd.to_datetime(yearly_df["subscribed_date"])
        yearly_ym = dates.dt.year * 12 + dates.dt.month
    else:
        yearly_ym = pd.Series([], dtype=int)

    w = _windows(df)
    result = {}

    for k, _v in w.items():
        window_start = None
        window_end   = None

        # Determine window start/end dates to compute year-month bounds
        if k == "yesterday":
            window_start = window_end = YESTERDAY
        elif k == "mtd":
            window_start, window_end = MTD_START, YESTERDAY
        elif k == "prev_full":
            window_start, window_end = PREV_MONTH_START, PREV_MONTH_END
        elif k == "prev_same":
            window_start, window_end = PREV_MONTH_START, PREV_MTD_END
        elif k == "ytd":
            window_start, window_end = YTD_START, YESTERDAY
        else:  # lifetime
            window_start = df["subscribed_date"].min() if not df.empty else YESTERDAY
            window_end   = YESTERDAY

        # Cash: all paid orders in the window (unchanged)
        in_win = df[
            (df["subscribed_date"] >= window_start) &
            (df["subscribed_date"] <= window_end)
        ]
        cash = round(float(in_win["total_amt"].sum()), 2)

        # Monthly MRR: monthly plan orders in window
        m_in_win = monthly_df[
            (monthly_df["subscribed_date"] >= window_start) &
            (monthly_df["subscribed_date"] <= window_end)
        ]
        monthly_mrr = float(m_in_win["total_amt"].sum())

        # Yearly MRR accrual: 1/12 of yearly orders whose 12-month window
        # overlaps this reporting window.
        # A yearly order from month M covers months M..M+11.
        # Include if: order_ym >= ws_ym - 11 AND order_ym <= we_ym
        if not yearly_df.empty:
            ws_ym = window_start.year * 12 + window_start.month
            we_ym = window_end.year   * 12 + window_end.month
            mask = (yearly_ym >= ws_ym - 11) & (yearly_ym <= we_ym)
            yearly_mrr = float(yearly_df.loc[mask, "total_amt"].sum()) / 12.0
        else:
            yearly_mrr = 0.0

        result[k] = {
            "cash": cash,
            "mrr":  round(monthly_mrr + yearly_mrr, 2),
        }

    return result


def _variance(current, previous) -> dict:
    """
    Compute absolute and percentage variance between two numbers.
    Returns: {abs_var, pct_var, direction}
    direction: "up" | "down" | "flat"
    """
    abs_var = current - previous
    pct_var = (abs_var / previous * 100) if previous != 0 else None
    direction = "up" if abs_var > 0 else ("down" if abs_var < 0 else "flat")
    return {
        "abs"       : round(abs_var, 2),
        "pct"       : round(pct_var, 1) if pct_var is not None else None,
        "direction" : direction,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CORE KPI FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def compute_subscription_counts(df: pd.DataFrame) -> dict:
    """
    Total subscription counts broken into three distinct measures:

    1. unique_users       — DISTINCT user_id (how many people use the product)
    2. unique_subs        — DISTINCT subscription_id (how many plans exist)
    3. total_transactions — COUNT of order rows (how many transactions)

    Each returned for all time windows.

    NOTE: total_transactions counts ALL order types (Trial, New Subscription,
          Renewal). Use the breakdown functions below for type-specific counts.
    """
    def _unique(frame, col):
        w = _windows(frame)
        return {k: int(v[col].nunique()) for k, v in w.items()}

    return {
        "unique_users"      : _unique(df, "user_id"),
        "unique_subs"       : _unique(df, "subscription_id"),
        "total_transactions": _counts(df),
    }


def compute_subscription_type_counts(df: pd.DataFrame) -> dict:
    """
    Counts broken down by subscription_type (Trial | New Subscription | Renewal)
    and trial_check (FREE | PAID | TRIAL) for all time windows.

    """
    trial_rows   = df[df["subscription_type"] == "Trial"]
    new_sub_rows = df[df["subscription_type"] == "New Subscription"]
    renewal_rows = df[df["subscription_type"] == "Renewal"]

    paid_rows        = df[df["trial_check"] == "PAID"]
    free_rows        = df[df["trial_check"] == "FREE"]
    trial_check_rows = df[df["trial_check"] == "TRIAL"]

    return {
        # By subscription lifecycle stage
        "trials"      : _counts(trial_rows),
        "new_subs"    : _counts(new_sub_rows),
        "renewals"    : _counts(renewal_rows),

        # By payment type
        "paid"        : _counts(paid_rows),
        "free"        : _counts(free_rows),
        "trial_check" : _counts(trial_check_rows),
    }


def compute_revenue(df_all: pd.DataFrame, df_full: pd.DataFrame = None) -> dict:
    """
    Revenue metrics for all time windows.

    G6: gross revenue includes ALL order statuses (not just Confirmed/Completed).
    A Cancelled or Refunded order that collected money still counts as gross revenue.

    cash     : Gross cash collected — SUM(total_amt) across all order statuses
    mrr      : Accrual-based MRR from gross orders
    refunds  : SUM(refund_amount) bucketed by refund_date (not order date)
    net_cash : cash - refunds
    """
    paid_rows = df_all[df_all["trial_check"] == "PAID"].copy()
    gross = _revenue(paid_rows)

    # Refunds: bucket by refund_date (PST), not order subscribed_date
    df_ref = df_full if df_full is not None else df_all

    if "refund_amount" in df_ref.columns:
        refund_rows = df_ref[df_ref["refund_amount"] > 0].copy()
    else:
        refund_rows = df_ref.iloc[0:0]

    # Determine which date column to use for bucketing
    refund_date_col = "refund_date" if "refund_date" in refund_rows.columns else "subscribed_date"

    def _refund_window(start, end):
        if refund_rows.empty:
            return 0.0
        w = refund_rows[
            (refund_rows[refund_date_col] >= start) &
            (refund_rows[refund_date_col] <= end)
        ]
        return round(float(w["refund_amount"].sum()), 2)

    windows = [
        ("yesterday", YESTERDAY,        YESTERDAY),
        ("mtd",       MTD_START,         YESTERDAY),
        ("prev_full", PREV_MONTH_START,  PREV_MONTH_END),
        ("prev_same", PREV_MONTH_START,  PREV_MTD_END),
        ("ytd",       YTD_START,         YESTERDAY),
        ("lifetime",  date(2000, 1, 1),  YESTERDAY),
    ]

    result = {}
    for key, start, end in windows:
        refunds = _refund_window(start, end)
        result[key] = {
            "cash"     : gross[key]["cash"],
            "mrr"      : gross[key]["mrr"],
            "refunds"  : refunds,
            "net_cash" : round(gross[key]["cash"] - refunds, 2),
        }
    return result


def _month_starts_in_range(period_start: date, period_end: date) -> list:
    """
    Return a list of first-of-month dates from period_start's month through
    period_end's month (inclusive on both ends).

    Used to build avg-denominator windows for YTD and Lifetime rates.
    Example: period_start=Jan 1, period_end=Jun 22 → [Jan 1, Feb 1, ..., Jun 1]
    """
    starts = []
    d = date(period_start.year, period_start.month, 1)
    end_m = date(period_end.year, period_end.month, 1)
    while d <= end_m:
        starts.append(d)
        d = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return starts


def _active_on(sub_df: pd.DataFrame, given_date) -> "pd.Series":
    """
    Boolean Series: True where a subscription was active on given_date.

    sub_df must have columns: paid_till, and optionally end_date.

    Logic mirrors the SQL curr_status CASE:
      Case A — end_date IS NULL OR paid_till > end_date
               → active if paid_till > given_date
      Case B — end_date IS NOT NULL AND paid_till <= end_date
               → active if end_date > given_date
    """
    has_end = "end_date" in sub_df.columns and sub_df["end_date"].notna().any()
    if not has_end:
        return sub_df["paid_till"] > given_date
    use_paid_till = sub_df["end_date"].isna() | (sub_df["paid_till"] > sub_df["end_date"])
    return (
        ( use_paid_till & (sub_df["paid_till"] > given_date)) |
        (~use_paid_till & (sub_df["end_date"]  > given_date))
    )


def _active_count_on(order_df: pd.DataFrame, given_date) -> int:
    """
    Count subscriptions active on given_date, considering only orders placed
    on or before given_date (subscribed_date <= given_date).

    Unlike _active_on — which works on a pre-built sub_summary with all-time
    max paid_till — this function is date-aware: orders placed after given_date
    are excluded. This correctly handles subscriptions that lapsed and later
    re-subscribed; without this filter, the re-subscription's paid_till would
    make the subscription appear continuously active through the gap period.

    Used for all historical active-count denominators: YTD, Lifetime, and
    month-start snapshots in compute_active_by_plan and churn functions.
    """
    eligible = order_df[order_df["subscribed_date"] <= given_date]
    if eligible.empty:
        return 0
    agg_cols = {"paid_till": ("paid_till", "max")}
    if "end_date" in eligible.columns and eligible["end_date"].notna().any():
        agg_cols["end_date"] = ("end_date", "max")
    per_sub = eligible.groupby("subscription_id", as_index=False).agg(**agg_cols)
    if "end_date" in per_sub.columns:
        case_b = per_sub["end_date"].notna() & (per_sub["paid_till"] <= per_sub["end_date"])
        effective = per_sub["paid_till"].copy()
        effective[case_b] = per_sub.loc[case_b, "end_date"]
        return int((effective > given_date).sum())
    return int((per_sub["paid_till"] > given_date).sum())


def compute_active_subscribers(df: pd.DataFrame) -> dict:
    """
    Active subscriber count — point-in-time snapshot, not windowed.

    EOD-YESTERDAY logic: a subscription is counted as active if its paid_till
    is on or after YESTERDAY — meaning it was valid at 23:59:59 of the last
    complete Pacific calendar day. This avoids including the current partial day
    and matches the "as of end of yesterday" framing used across all other KPIs.

    When paid_till is unavailable, falls back to curr_status == Active from SQL.

    active_at_month_start denominator for churn rate:
      Subscriptions whose paid_till >= MTD_START — i.e. paid access had not yet
      expired at the start of the current month.
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()

    if has_paid_till:
        # Deduplicate to one row per subscription
        agg_cols = {"paid_till": ("paid_till", "max"), "curr_status": ("curr_status", "last")}
        if "end_date" in df.columns:
            agg_cols["end_date"] = ("end_date", "max")
        sub_summary = df.sort_values("paid_till").groupby("subscription_id", as_index=False).agg(**agg_cols)

        active_now            = int(_active_on(sub_summary, YESTERDAY).sum())
        # Use date-aware count: only orders placed by month-start cutoff.
        # Avoids inflating the count with future renewal orders that don't
        # exist yet at that point in time.
        active_at_month_start = _active_count_on(df, MTD_START - timedelta(days=1))
    else:
        # Fallback: use curr_status from SQL when paid_till is unavailable
        active_now = int(df[df["curr_status"] == "Active"]["subscription_id"].nunique())
        active_at_month_start = int(df[
            (df["subscribed_date"] < MTD_START) &
            (df["curr_status"] == "Active")
        ]["subscription_id"].nunique())

    # churn_denominator = active_at_month_start (already includes any subs active at start of month)
    # We do not add new_active_mtd separately because any sub with paid_till >= MTD_START is
    # already counted; new subs started mid-month will have paid_till > MTD_START too.
    churn_denominator = active_at_month_start

    return {
        "active_now"            : int(active_now),
        "active_at_month_start" : active_at_month_start,
        "churn_denominator"     : churn_denominator,
        "paid_till_available"   : has_paid_till,
    }


def compute_paid_churn(df: pd.DataFrame) -> dict:
    """
    Paid Churn Rate (redefined v2.2).

    Counts ONLY paid subscriptions (trial_check = PAID at some point).
    Trials and Free plans are excluded from both sides of the rate.

    Numerator   : paid subscriptions where:
                    (a) max paid_till falls within the window
                    (b) curr_status = In-Active
                  This identifies the first missed renewal for each
                  subscription, counted exactly once regardless of how many
                  billing cycles have since elapsed.
                  Falls back to max subscribed_date as churn-date proxy
                  if paid_till is unavailable.

    Denominator : paid subscriptions whose max paid_till >= period_start_date
                  (i.e. they were still in their paid period at the beginning
                  of the period being measured).

    Period start dates:
      MTD        → MTD_START        (1st of current month)
      Prev Month → PREV_MONTH_START  (1st of previous month)
      YTD        → YTD_START        (Jan 1 of current year)

    Retention Rate = 1 − Churn Rate  (returned in each window sub-dict).
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()

    # Only subscriptions that have at least one PAID order
    paid_sub_ids = set(df[df["trial_check"] == "PAID"]["subscription_id"].unique())
    paid_df = df[df["subscription_id"].isin(paid_sub_ids)]

    if has_paid_till:
        agg_cols = {
            "churn_date" : ("paid_till",      "max"),
            "curr_status": ("curr_status",    "last"),
        }
        if "end_date" in paid_df.columns:
            agg_cols["end_date"] = ("end_date", "max")
        sub_summary = paid_df.sort_values("paid_till").groupby("subscription_id", as_index=False).agg(**agg_cols)
        # Rename churn_date → paid_till so _active_on can find it
        sub_summary = sub_summary.rename(columns={"churn_date": "paid_till"})
        # G2: for Case B subs (end_date set and paid_till <= end_date),
        # access ends at end_date — use that as the churn event date
        if "end_date" in sub_summary.columns:
            case_b = sub_summary["end_date"].notna() & (sub_summary["paid_till"] <= sub_summary["end_date"])
            sub_summary.loc[case_b, "paid_till"] = sub_summary.loc[case_b, "end_date"]
    else:
        sub_summary = (
            paid_df.sort_values("subscribed_date").groupby("subscription_id", as_index=False)
            .agg(paid_till=("subscribed_date", "max"), curr_status=("curr_status", "last"))
        )

    sub_summary["churned"] = sub_summary["curr_status"] == "In-Active"

    def _active_paid_at(cutoff_date) -> int:
        # Use date-aware count: only paid orders placed by cutoff_date.
        return _active_count_on(paid_df, cutoff_date)

    def _avg_active_paid_in_period(period_start, period_end) -> int:
        """Avg of active paid subs at each month start in [period_start, period_end]."""
        month_starts = _month_starts_in_range(period_start, period_end)
        counts = [_active_paid_at(ms - timedelta(days=1)) for ms in month_starts]
        return round(sum(counts) / len(counts)) if counts else 0

    def _churn(window_start, window_end, denom: int):
        """denom is a pre-computed integer (point-in-time or avg)."""
        churned_count = int(sub_summary[
            (sub_summary["paid_till"] >= window_start) &
            (sub_summary["paid_till"] <= window_end) &
            sub_summary["churned"]
        ]["subscription_id"].nunique())
        rate  = round(churned_count / denom, 4) if denom > 0 else 0.0
        return {
            "events"         : churned_count,
            "denominator"    : denom,
            "rate"           : rate,
            "retention_rate" : round(1 - rate, 4),
        }

    # Point-in-time denom for single-day/month windows; avg for multi-month windows.
    earliest = paid_df["subscribed_date"].min() if not paid_df.empty else date(2000, 1, 1)
    return {
        "yesterday"      : _churn(YESTERDAY,        YESTERDAY,      _active_paid_at(YESTERDAY        - timedelta(days=1))),
        "mtd"            : _churn(MTD_START,         YESTERDAY,      _active_paid_at(MTD_START        - timedelta(days=1))),
        "prev_full"      : _churn(PREV_MONTH_START,  PREV_MONTH_END, _active_paid_at(PREV_MONTH_START - timedelta(days=1))),
        "prev_same"      : _churn(PREV_MONTH_START,  PREV_MTD_END,   _active_paid_at(PREV_MONTH_START - timedelta(days=1))),
        "ytd"            : _churn(YTD_START,         YESTERDAY,      _avg_active_paid_in_period(YTD_START,  YESTERDAY)),
        "lifetime"       : _churn(date(2000, 1, 1),  YESTERDAY,      _avg_active_paid_in_period(earliest,   YESTERDAY)),
        "using_paid_till": has_paid_till,
    }


def compute_trial_to_paid_conversion(df: pd.DataFrame) -> dict:
    """
    Trial→Paid conversion rate (redefined v2.2).

    Numerator   : distinct subscription_ids that placed a PAID order within
                  the window AND had a Trial order on the same subscription_id
                  before that paid order.
    Denominator : distinct subscription_ids whose trial END DATE falls within
                  the window.  Trial end = paid_till for that subscription; falls
                  back to subscribed_date + 7 days when paid_till is NULL.

    Windows apply identically to numerator and denominator
    (e.g. MTD = Jun 1–8 for both sides).

    This is an event-based, not cohort-based, metric: both sides of the
    rate are anchored to the same window, giving a real-time conversion
    picture for each period.
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()

    # ── Compute trial_end for each trial subscription ─────────────────────
    # G4: derive trial duration from data instead of hardcoding 7 days.
    #
    # Converted trials  → trial_end = MIN(paid_order.subscribed_date) on the
    #   same subscription_id.  paid_till is NOT used here because it gets
    #   overwritten with the post-conversion expiry once a user pays, which
    #   would push trial_end into the wrong period.
    #
    # Unconverted trials → trial_end = paid_till.  For a trial that never
    #   converted, paid_till still holds the trial expiry date (nothing has
    #   updated it).  Falls back to subscribed_date + 7 only when paid_till
    #   is NULL.
    trials = df[df["subscription_type"] == "Trial"][["subscription_id", "subscribed_date", "paid_till"]].copy()
    trial_starts = trials.groupby("subscription_id", as_index=False).agg(
        trial_start   = ("subscribed_date", "min"),
        trial_paid_till = ("paid_till", "max"),
    )

    # First paid order date per subscription (for converted trials)
    first_paid = (
        df[df["trial_check"] == "PAID"]
        .groupby("subscription_id", as_index=False)
        .agg(first_paid_date=("subscribed_date", "min"))
    )

    trial_subs = trial_starts.merge(first_paid, on="subscription_id", how="left")

    # Converted: use first paid order date; unconverted: use paid_till (fallback +7d)
    def _derive_trial_end(row):
        if pd.notna(row["first_paid_date"]):
            return row["first_paid_date"]
        if pd.notna(row["trial_paid_till"]):
            return row["trial_paid_till"]
        return row["trial_start"] + timedelta(days=7)

    trial_subs["trial_end"] = trial_subs.apply(_derive_trial_end, axis=1)

    # ── Paid orders on subscriptions that had a trial ─────────────────────
    trial_sub_ids = set(trial_subs["subscription_id"].unique())
    paid_orders = df[
        (df["trial_check"] == "PAID") &
        (df["subscription_id"].isin(trial_sub_ids))
    ][["subscription_id", "subscribed_date"]].copy()
    paid_orders.columns = ["subscription_id", "paid_order_date"]

    def _conv(window_start, window_end):
        # Denominator: trial subscriptions whose trial ended in this window
        denom_ids = set(
            trial_subs[
                (trial_subs["trial_end"] >= window_start) &
                (trial_subs["trial_end"] <= window_end)
            ]["subscription_id"].unique()
        )
        total = len(denom_ids)
        if total == 0:
            return {"total": 0, "converted": 0, "rate": None}

        # Numerator: paid orders placed in this window on denom_ids
        paid_in_window = set(
            paid_orders[
                (paid_orders["paid_order_date"] >= window_start) &
                (paid_orders["paid_order_date"] <= window_end)
            ]["subscription_id"].unique()
        )
        converted = len(denom_ids & paid_in_window)
        return {
            "total"     : total,
            "converted" : converted,
            "rate"      : round(converted / total, 4),
        }

    return {
        "yesterday" : _conv(YESTERDAY,         YESTERDAY),
        "mtd"       : _conv(MTD_START,          YESTERDAY),
        "prev_full" : _conv(PREV_MONTH_START,   PREV_MONTH_END),
        "prev_same" : _conv(PREV_MONTH_START,   PREV_MTD_END),
        "ytd"       : _conv(YTD_START,          YESTERDAY),
        "lifetime"  : _conv(date(2000, 1, 1),   YESTERDAY),
    }


def compute_trial_pipeline(df: pd.DataFrame) -> dict:
    """
    Trial Pipeline — point-in-time count of trials currently within their
    trial window that have NOT yet converted to a paid order.

    G4: no longer uses a hardcoded 7-day window. A trial is considered
    in-window if paid_till >= TODAY (access has not yet expired) AND
    subscribed_date <= YESTERDAY (exclude today's partial data).
    Falls back to subscribed_date > TRIAL_CUTOFF for rows where paid_till
    is NULL.

    Logic:
      1. Find all Trial subscriptions still within their trial window
      2. Exclude any subscription_id that already has a PAID order
      3. Return the count of remaining open / unconverted trials
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()
    trial_rows = df[
        (df["subscription_type"] == "Trial") &
        (df["subscribed_date"] <= YESTERDAY)
    ]
    if has_paid_till:
        # In-window: trial access has not yet expired
        # For rows missing paid_till, fall back to the 7-day cutoff
        in_window = (
            trial_rows["paid_till"].notna() & (trial_rows["paid_till"] >= TODAY)
        ) | (
            trial_rows["paid_till"].isna() & (trial_rows["subscribed_date"] > TRIAL_CUTOFF)
        )
        recent_trial_ids = set(trial_rows.loc[in_window, "subscription_id"].unique())
    else:
        recent_trial_ids = set(
            trial_rows[trial_rows["subscribed_date"] > TRIAL_CUTOFF]["subscription_id"].unique()
        )

    paid_sub_ids = set(df[df["trial_check"] == "PAID"]["subscription_id"].unique())
    open_pipeline = recent_trial_ids - paid_sub_ids

    # Apply 31-day cap: exclude trials where days_remaining > 31.
    # These are almost certainly data anomalies (paid_till far in the future)
    # and would inflate the pipeline count without representing real at-risk trials.
    # Consistent with get_open_trial_rows which applies the same filter.
    if has_paid_till and open_pipeline:
        pt_by_sub = (
            trial_rows[trial_rows["subscription_id"].isin(open_pipeline)]
            .groupby("subscription_id")["paid_till"].max()
        )
        open_pipeline = {
            sub_id for sub_id, pt in pt_by_sub.items()
            if pd.isna(pt) or (pt - TODAY).days <= 31
        }

    # Compute the actual date range of open trial starts for the label
    if open_pipeline and has_paid_till:
        open_rows = trial_rows[trial_rows["subscription_id"].isin(open_pipeline)]
        pipeline_start = open_rows["subscribed_date"].min()
        pipeline_end   = open_rows["subscribed_date"].max()
        started_range  = compact_range(pipeline_start, pipeline_end)
    else:
        started_range = compact_range(PIPELINE_START, YESTERDAY)

    return {
        "count"         : len(open_pipeline),
        "started_range" : started_range,
    }


def get_open_trial_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a DataFrame with one row per open (unconverted, access-not-expired)
    trial subscription — the raw roster behind the Trial Pipeline count.

    Applies the same logic as compute_trial_pipeline:
      1. subscription_type = 'Trial', subscribed_date <= YESTERDAY
      2. paid_till >= TODAY (access not yet expired)
         Falls back to subscribed_date > TRIAL_CUTOFF when paid_till is NULL
      3. Exclude subscription_ids that already have a PAID order

    Adds a 'days_remaining' column: max(0, paid_till − TODAY).
    Sorted ascending by subscribed_date so oldest (most at-risk) trials appear first.

    Input df should be df_kpi (test-excluded, plan-filtered, all order statuses).
    Confirmed/Completed filter is applied internally.
    """
    if "order_status" in df.columns:
        df_clean = df[df["order_status"].isin(["Confirmed", "Completed"])].copy()
    else:
        df_clean = df.copy()

    has_paid_till = "paid_till" in df_clean.columns and df_clean["paid_till"].notna().any()

    trial_rows = df_clean[
        (df_clean["subscription_type"] == "Trial") &
        (df_clean["subscribed_date"] <= YESTERDAY)
    ]

    if has_paid_till:
        in_window = (
            trial_rows["paid_till"].notna() & (trial_rows["paid_till"] >= TODAY)
        ) | (
            trial_rows["paid_till"].isna() & (trial_rows["subscribed_date"] > TRIAL_CUTOFF)
        )
        recent_trial_ids = set(trial_rows.loc[in_window, "subscription_id"].unique())
    else:
        recent_trial_ids = set(
            trial_rows[trial_rows["subscribed_date"] > TRIAL_CUTOFF]["subscription_id"].unique()
        )

    paid_sub_ids = set(df_clean[df_clean["trial_check"] == "PAID"]["subscription_id"].unique())
    open_ids = recent_trial_ids - paid_sub_ids

    # One row per subscription — take the trial order row
    open_rows = (
        trial_rows[trial_rows["subscription_id"].isin(open_ids)]
        .sort_values("subscribed_date", ascending=False)
        .drop_duplicates(subset=["subscription_id"], keep="first")
        .copy()
    )

    # Days remaining until trial expires
    open_rows["days_remaining"] = open_rows["paid_till"].apply(
        lambda pt: max(0, (pt - TODAY).days) if pd.notna(pt) else None
    )

    # Exclude trials with more than 31 days remaining — these are almost
    # certainly data anomalies (paid_till far in the future) or very recently
    # started long-window trials that don't need follow-up yet.
    open_rows = open_rows[
        open_rows["days_remaining"].isna() | (open_rows["days_remaining"] <= 31)
    ].copy()

    # Sort oldest first — longest-running unconverted trials are highest priority
    open_rows = open_rows.sort_values("subscribed_date", ascending=True).reset_index(drop=True)

    return open_rows


def get_churned_rows(df: pd.DataFrame) -> dict:
    """
    Returns raw subscriber data for churned users in the MTD window.
    Matches the churned populations behind compute_paid_churn 'mtd' and
    compute_trial_to_paid_conversion 'mtd' (unconverted denominator only).

    Returns
    -------
    dict with keys:
      "paid"  : DataFrame — paid subscriptions whose access expired in MTD
                and are now In-Active. One row per subscription, sorted by
                churn_date ascending.
      "trial" : DataFrame — trial subscriptions whose trial ended in MTD
                and did NOT convert to paid. One row per subscription, sorted
                by trial_end ascending.
    """
    if "order_status" in df.columns:
        df_clean = df[df["order_status"].isin(["Confirmed", "Completed"])].copy()
    else:
        df_clean = df.copy()

    info_cols = [c for c in [
        "subscription_id", "user_id", "full_name", "email_id",
        "contact_number", "plan_segment", "platform", "device_type",
    ] if c in df_clean.columns]

    # ── Paid Churn (MTD) ─────────────────────────────────────────────────────
    paid_sub_ids = set(df_clean[df_clean["trial_check"] == "PAID"]["subscription_id"].unique())
    paid_df = df_clean[df_clean["subscription_id"].isin(paid_sub_ids)]
    has_paid_till = "paid_till" in paid_df.columns and paid_df["paid_till"].notna().any()

    if has_paid_till:
        agg_cols = {"churn_date": ("paid_till", "max"), "curr_status": ("curr_status", "last")}
        if "end_date" in paid_df.columns:
            agg_cols["end_date"] = ("end_date", "max")
        sub_summary = paid_df.sort_values("paid_till").groupby(
            "subscription_id", as_index=False
        ).agg(**agg_cols)
        # Case B: if end_date is set and paid_till <= end_date, use end_date as churn date
        if "end_date" in sub_summary.columns:
            case_b = sub_summary["end_date"].notna() & (sub_summary["churn_date"] <= sub_summary["end_date"])
            sub_summary.loc[case_b, "churn_date"] = sub_summary.loc[case_b, "end_date"]
    else:
        sub_summary = paid_df.sort_values("subscribed_date").groupby(
            "subscription_id", as_index=False
        ).agg(churn_date=("subscribed_date", "max"), curr_status=("curr_status", "last"))

    churned_mtd_ids = set(sub_summary[
        (sub_summary["churn_date"] >= MTD_START) &
        (sub_summary["churn_date"] <= YESTERDAY) &
        (sub_summary["curr_status"] == "In-Active")
    ]["subscription_id"])

    sub_info = (
        df_clean[df_clean["subscription_id"].isin(churned_mtd_ids)]
        .sort_values("subscribed_date")
        .drop_duplicates(subset=["subscription_id"], keep="last")[info_cols]
    )
    paid_churned = (
        sub_summary[sub_summary["subscription_id"].isin(churned_mtd_ids)][
            ["subscription_id", "churn_date"]
        ]
        .merge(sub_info, on="subscription_id", how="left")
        .sort_values("churn_date", ascending=True)
        .reset_index(drop=True)
    )

    # ── Trial Churn (MTD) ────────────────────────────────────────────────────
    trials = df_clean[df_clean["subscription_type"] == "Trial"][
        ["subscription_id", "subscribed_date", "paid_till"]
    ].copy()
    trial_starts = trials.groupby("subscription_id", as_index=False).agg(
        trial_start=("subscribed_date", "min"),
        trial_paid_till=("paid_till", "max"),
    )
    first_paid = (
        df_clean[df_clean["trial_check"] == "PAID"]
        .groupby("subscription_id", as_index=False)
        .agg(first_paid_date=("subscribed_date", "min"))
    )
    trial_subs = trial_starts.merge(first_paid, on="subscription_id", how="left")

    def _derive_trial_end(row):
        if pd.notna(row["first_paid_date"]):
            return row["first_paid_date"]
        if pd.notna(row["trial_paid_till"]):
            return row["trial_paid_till"]
        return row["trial_start"] + timedelta(days=7)

    trial_subs["trial_end"] = trial_subs.apply(_derive_trial_end, axis=1)

    # Denominator: trial ended in MTD
    denom_ids = set(trial_subs[
        (trial_subs["trial_end"] >= MTD_START) &
        (trial_subs["trial_end"] <= YESTERDAY)
    ]["subscription_id"])

    # Remove those who converted (paid order in MTD on same sub_id)
    paid_in_mtd = set(
        df_clean[
            (df_clean["trial_check"] == "PAID") &
            (df_clean["subscription_id"].isin(denom_ids)) &
            (df_clean["subscribed_date"] >= MTD_START) &
            (df_clean["subscribed_date"] <= YESTERDAY)
        ]["subscription_id"]
    )
    trial_churned_ids = denom_ids - paid_in_mtd

    trial_sub_info = (
        df_clean[df_clean["subscription_id"].isin(trial_churned_ids)]
        .sort_values("subscribed_date")
        .drop_duplicates(subset=["subscription_id"], keep="last")[info_cols]
    )
    trial_churned = (
        trial_subs[trial_subs["subscription_id"].isin(trial_churned_ids)][
            ["subscription_id", "trial_start", "trial_end"]
        ]
        .merge(trial_sub_info, on="subscription_id", how="left")
        .sort_values("trial_end", ascending=True)
        .reset_index(drop=True)
    )

    return {"paid": paid_churned, "trial": trial_churned}


def compute_free_to_paid_conversion(df: pd.DataFrame) -> dict:
    """
    Free→Paid conversion rate (redefined v2.2).

    Numerator   : distinct user_ids who placed a PAID order within the window
                  AND had a FREE order on any subscription before that paid order.
    Denominator : free users whose free subscription was ACTIVE at the start of
                  the period.  "Active at period start" = the free subscription's
                  paid_till >= period_start_date (falls back to subscribed_date <
                  period_start if paid_till is unavailable).

    Period start dates per window:
      MTD        → MTD_START       (1st of current month)
      Prev Month → PREV_MONTH_START (1st of previous month)
      YTD        → YTD_START       (Jan 1 of current year)
      Lifetime   → all free users ever (no active cutoff applied)

    NOTE: A user can go Free → Trial → Paid; they appear in both this
    metric AND the Trial→Paid metric.  This is intentional.
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()

    # ── Free subscription data ─────────────────────────────────────────────
    free_df = df[df["trial_check"] == "FREE"].copy()

    # Max paid_till per user (across all their free subscriptions)
    if has_paid_till:
        user_paid_till = (
            free_df.groupby("user_id")["paid_till"]
            .max()
            .reset_index()
            .rename(columns={"paid_till": "free_paid_till"})
        )
    else:
        user_paid_till = None

    # Min subscribed_date per user (earliest free order) — fallback denominator
    user_first_free = (
        free_df.groupby("user_id")["subscribed_date"]
        .min()
        .reset_index()
        .rename(columns={"subscribed_date": "first_free_date"})
    )

    # ── Paid orders for free users ─────────────────────────────────────────
    free_user_ids = set(free_df["user_id"].unique())
    paid_orders = df[
        (df["trial_check"] == "PAID") &
        (df["user_id"].isin(free_user_ids))
    ][["user_id", "subscribed_date"]].copy()
    paid_orders.columns = ["user_id", "paid_order_date"]

    def _active_free_at(cutoff_date) -> set:
        """Return set of user_ids who had an active free plan at cutoff_date.
        Uses only free orders placed on or before cutoff_date (date-aware),
        consistent with _active_count_on used for paid subs."""
        if has_paid_till:
            eligible = free_df[free_df["subscribed_date"] <= cutoff_date]
            if eligible.empty:
                return set()
            ept = (
                eligible.groupby("user_id")["paid_till"]
                .max()
                .reset_index()
                .rename(columns={"paid_till": "free_paid_till"})
            )
            return set(ept[ept["free_paid_till"] > cutoff_date]["user_id"].unique())
        else:
            return set(
                user_first_free[user_first_free["first_free_date"] < cutoff_date]["user_id"].unique()
            )

    def _avg_active_free_in_period(period_start, period_end) -> int:
        """Avg of active free user counts at each month start in [period_start, period_end]."""
        month_starts = _month_starts_in_range(period_start, period_end)
        counts = [len(_active_free_at(ms)) for ms in month_starts]
        return round(sum(counts) / len(counts)) if counts else 0

    def _conv(window_start, window_end, denom_cutoff=None, denom_override=None):
        """
        denom_cutoff: point-in-time cutoff (used for MTD, prev windows).
        denom_override: pre-computed avg count (used for YTD and Lifetime).
        When denom_override is provided, all free users are eligible for numerator.
        """
        if denom_override is not None:
            total = denom_override
            # Numerator: any free user who placed a paid order in the window
            conv_users = set(free_user_ids)
        elif denom_cutoff is None:
            total = len(free_user_ids)
            conv_users = set(free_user_ids)
        else:
            conv_users = _active_free_at(denom_cutoff)
            total = len(conv_users)

        if total == 0:
            return {"total": 0, "converted": 0, "rate": None}

        paid_in_window = set(
            paid_orders[
                (paid_orders["paid_order_date"] >= window_start) &
                (paid_orders["paid_order_date"] <= window_end)
            ]["user_id"].unique()
        )
        converted = len(conv_users & paid_in_window)
        return {
            "total"     : total,
            "converted" : converted,
            "rate"      : round(converted / total, 4),
        }

    earliest_free = (
        free_df["subscribed_date"].min()
        if not free_df.empty
        else date(2000, 1, 1)
    )
    return {
        "yesterday" : _conv(YESTERDAY,         YESTERDAY,      denom_cutoff=YESTERDAY),
        "mtd"       : _conv(MTD_START,          YESTERDAY,      denom_cutoff=MTD_START),
        "prev_full" : _conv(PREV_MONTH_START,   PREV_MONTH_END, denom_cutoff=PREV_MONTH_START),
        "prev_same" : _conv(PREV_MONTH_START,   PREV_MTD_END,   denom_cutoff=PREV_MONTH_START),
        "ytd"       : _conv(YTD_START,          YESTERDAY,      denom_override=_avg_active_free_in_period(YTD_START,     YESTERDAY)),
        "lifetime"  : _conv(date(2000, 1, 1),   YESTERDAY,      denom_override=_avg_active_free_in_period(earliest_free, YESTERDAY)),
    }


def compute_plan_performance(df: pd.DataFrame) -> list:
    """
    Per-plan-segment breakdown for the Plan Performance sheet.

    Segments (confirmed 5-way split):
      Lite Monthly | Lite Yearly | Unlimited Monthly | Unlimited Yearly | Free Monthly

    For each segment returns:
      - subscription counts for all windows
      - revenue (cash + MRR) for all windows
      - unique users for all windows

    Returns a list of dicts, one per segment, sorted by lifetime revenue desc.
    """
    # Use plan_segment column from SQL (already computed)
    # Fallback: derive it here if column missing
    if "plan_segment" not in df.columns:
        df = df.copy()
        df["plan_segment"] = df.apply(
            lambda r: "Free Monthly" if r["subscription_name"] == "Free"
                      else f"{r['subscription_name']} {r['duration']}",
            axis=1
        )

    segments = [
        "Free Monthly",
        "Lite Monthly", "Lite Yearly",
        "Unlimited Monthly", "Unlimited Yearly",
    ]

    result = []
    for seg in segments:
        seg_df = df[df["plan_segment"] == seg]
        if len(seg_df) == 0:
            continue

        paid_seg = seg_df[seg_df["trial_check"] == "PAID"]
        w = _windows(seg_df)

        result.append({
            "segment"    : seg,
            "counts"     : _counts(seg_df),
            "unique_users": {k: int(v["user_id"].nunique()) for k, v in w.items()},
            "revenue"    : _revenue(paid_seg),
        })

    return result


def compute_platform_breakdown(df: pd.DataFrame) -> list:
    """
    Platform breakdown (4-way split confirmed):
      iOS App | Android App | Web | Mobile Browser

    Maps device_type column to the 4 platform labels.
    Returns list of dicts sorted by MTD count desc.
    """
    # Map device_type to platform label
    # device_type values: IOS, ANDROID, BROWSER_DESKTOP,
    #                     IOS_BROWSER_MOBILE, Android_BROWSER_MOBILE
    platform_map = {
        "IOS"                   : "iOS App",
        "ANDROID"               : "Android App",
        "BROWSER_DESKTOP"       : "Web",
        "IOS_BROWSER_MOBILE"    : "Mobile Browser",
        "Android_BROWSER_MOBILE": "Mobile Browser",
    }

    df = df.copy()
    df["platform_label"] = df["device_type"].map(platform_map).fillna("Web")

    platforms = ["iOS App", "Android App", "Web", "Mobile Browser"]
    result = []
    for plat in platforms:
        plat_df = df[df["platform_label"] == plat]
        if len(plat_df) == 0:
            continue
        paid_plat = plat_df[plat_df["trial_check"] == "PAID"]
        result.append({
            "platform" : plat,
            "counts"   : _counts(plat_df),
            "revenue"  : _revenue(paid_plat),
        })

    # Sort by MTD count descending
    result.sort(key=lambda x: x["counts"]["mtd"], reverse=True)
    return result


def compute_monthly_trend(df: pd.DataFrame) -> list:
    """
    Monthly trend data for the last 12 full months (not including current month).

    For each month returns:
      new_subs, trials, renewals, paid, free,
      cash_revenue, mrr_revenue, unique_users,
      trial_to_paid_rate (for that month's cohort, 7-day rule applied)

    Returns list of dicts sorted ascending (oldest → newest).

    NOTE: Current month is excluded from the trend because it is incomplete.
    The Executive Dashboard shows current month as MTD figures separately.
    """
    df = df.copy()
    df["month"] = pd.to_datetime(df["subscribed_date"]).dt.to_period("M")

    # Last 12 full months = exclude current month
    current_period = pd.Period(TODAY, "M")
    full_months = sorted([p for p in df["month"].unique() if p < current_period])[-12:]
    months = full_months  # used in loop

    result = []
    for month in months:
        month_df   = df[df["month"] == month]
        paid_month = month_df[month_df["trial_check"] == "PAID"]

        # Trial→Paid for this month's cohort
        # Full months only — all trials have had time to convert or expire
        trial_month = month_df[month_df["subscription_type"] == "Trial"]
        paid_ids    = set(df[df["trial_check"] == "PAID"]["subscription_id"])
        conv_count  = trial_month[trial_month["subscription_id"].isin(paid_ids)].shape[0]
        trial_total = len(trial_month)
        conv_rate   = round(conv_count / trial_total, 4) if trial_total > 0 else None

        new_subs_count  = int((month_df["subscription_type"] == "New Subscription").sum())
        trials_count    = int((month_df["subscription_type"] == "Trial").sum())
        renewals_count  = int((month_df["subscription_type"] == "Renewal").sum())
        # Paid new subs: new subscriptions that are paid (excludes free new sign-ups)
        paid_new_count  = int(
            ((month_df["subscription_type"] == "New Subscription") &
             (month_df["trial_check"] == "PAID")).sum()
        )
        # Total subs = new + renewals (excludes trials — represents committed subscribers)
        total_subs      = new_subs_count + renewals_count

        result.append({
            "month"           : str(month),
            "month_label"     : month.strftime("%b %Y"),
            "new_subs"        : new_subs_count,
            "trials"          : trials_count,
            "renewals"        : renewals_count,
            "paid"            : int((month_df["trial_check"] == "PAID").sum()),
            "free"            : int((month_df["trial_check"] == "FREE").sum()),
            "unique_users"    : int(month_df["user_id"].nunique()),
            "cash_revenue"    : round(float(paid_month["total_amt"].sum()), 2),
            "mrr_revenue"     : round(float(paid_month["mrr_amount"].sum()), 2),
            "trial_conv_rate" : conv_rate,
            "paid_new_subs"   : paid_new_count,
            "total_subs"      : total_subs,
        })

    # Also append current partial month (MTD)
    current_month_df = df[df["month"] == current_period]
    if not current_month_df.empty:
        cm_paid = current_month_df[current_month_df["trial_check"] == "PAID"]
        cm_trial = current_month_df[current_month_df["subscription_type"] == "Trial"]
        cm_paid_ids = set(df[df["trial_check"] == "PAID"]["subscription_id"])
        cm_conv = cm_trial[cm_trial["subscription_id"].isin(cm_paid_ids)].shape[0]
        cm_trial_total = len(cm_trial)
        cm_conv_rate = round(cm_conv / cm_trial_total, 4) if cm_trial_total > 0 else None
        cm_new_subs = int((current_month_df["subscription_type"] == "New Subscription").sum())
        cm_renewals = int((current_month_df["subscription_type"] == "Renewal").sum())
        cm_paid_new = int(
            ((current_month_df["subscription_type"] == "New Subscription") &
             (current_month_df["trial_check"] == "PAID")).sum()
        )
        result.append({
            "month"           : str(current_period),
            "month_label"     : current_period.strftime("%b") + " MTD",
            "new_subs"        : cm_new_subs,
            "trials"          : int((current_month_df["subscription_type"] == "Trial").sum()),
            "renewals"        : cm_renewals,
            "paid"            : int((current_month_df["trial_check"] == "PAID").sum()),
            "free"            : int((current_month_df["trial_check"] == "FREE").sum()),
            "unique_users"    : int(current_month_df["user_id"].nunique()),
            "cash_revenue"    : round(float(cm_paid["total_amt"].sum()), 2),
            "mrr_revenue"     : round(float(cm_paid["mrr_amount"].sum()), 2),
            "trial_conv_rate" : cm_conv_rate,
            "paid_new_subs"   : cm_paid_new,
            "total_subs"      : cm_new_subs + cm_renewals,
        })

    return result


def compute_funnel(df: pd.DataFrame) -> dict:
    """
    Subscription funnel breakdown for the Funnel sheet.

    Combined funnel stages:
      Sign-ups (all orders) → Trials → New Subscriptions → Renewals → Churn

    Also includes per-plan breakdown (Lite vs Unlimited) underneath.

    All windows: yesterday, mtd, prev_full, prev_same, ytd, lifetime.
    """
    # Combined funnel
    funnel = {
        "signups"      : _counts(df),
        "trials"       : _counts(df[df["subscription_type"] == "Trial"]),
        "new_subs"     : _counts(df[df["subscription_type"] == "New Subscription"]),
        "renewals"     : _counts(df[df["subscription_type"] == "Renewal"]),
        "paid"         : _counts(df[df["trial_check"] == "PAID"]),
        "free"         : _counts(df[df["trial_check"] == "FREE"]),
    }

    # Per-plan breakdown (Lite vs Unlimited, excluding Free)
    paid_plans = ["Lite", "Unlimited"]
    plan_breakdown = {}
    for plan in paid_plans:
        plan_df = df[df["subscription_name"] == plan]
        plan_breakdown[plan] = {
            "trials"   : _counts(plan_df[plan_df["subscription_type"] == "Trial"]),
            "new_subs" : _counts(plan_df[plan_df["subscription_type"] == "New Subscription"]),
            "renewals" : _counts(plan_df[plan_df["subscription_type"] == "Renewal"]),
            "paid"     : _counts(plan_df[plan_df["trial_check"] == "PAID"]),
            "revenue"  : _revenue(plan_df[plan_df["trial_check"] == "PAID"]),
        }

    # Per-segment breakdown (all 5 plan_segments)
    segment_breakdown = {}
    for seg in PLAN_SEGMENTS:
        seg_df = df[df["plan_segment"] == seg]
        segment_breakdown[seg] = {
            "trials"   : _counts(seg_df[seg_df["subscription_type"] == "Trial"]),
            "new_subs" : _counts(seg_df[seg_df["subscription_type"] == "New Subscription"]),
            "renewals" : _counts(seg_df[seg_df["subscription_type"] == "Renewal"]),
            "paid"     : _counts(seg_df[seg_df["trial_check"] == "PAID"]),
            "revenue"  : _revenue(seg_df[seg_df["trial_check"] == "PAID"]),
        }

    return {"combined": funnel, "by_plan": plan_breakdown, "by_segment": segment_breakdown}


def compute_renewal_state(df: pd.DataFrame) -> dict:
    """
    Breaks active-at-month-start paid subscribers into 3 mutually exclusive states
    for the current MTD window only.

    active_at_start : paid subs with paid_till >= MTD_START - 1 day
    churned         : active_at_start subs whose paid_till expired in MTD AND In-Active
    renewed         : active_at_start subs who placed a Renewal paid order in MTD
    waiting         : active_at_start - churned - renewed (still active, not yet renewed)
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()

    paid_sub_ids = set(df[df["trial_check"] == "PAID"]["subscription_id"].unique())
    paid_df = df[df["subscription_id"].isin(paid_sub_ids)]

    if not has_paid_till or paid_df.empty:
        return {"active_at_start": 0, "churned": 0, "renewed": 0, "waiting": 0}

    agg_cols_rs = {"max_paid_till": ("paid_till", "max"), "curr_status": ("curr_status", "last")}
    if "end_date" in paid_df.columns:
        agg_cols_rs["end_date"] = ("end_date", "max")
    sub_summary = (
        paid_df.sort_values("paid_till").groupby("subscription_id", as_index=False)
        .agg(**agg_cols_rs)
    )
    # G2: for Case B subs, churn date is end_date, not paid_till
    if "end_date" in sub_summary.columns:
        case_b = sub_summary["end_date"].notna() & (sub_summary["max_paid_till"] <= sub_summary["end_date"])
        sub_summary.loc[case_b, "max_paid_till"] = sub_summary.loc[case_b, "end_date"]

    # Rename for _active_on compatibility
    sub_summary = sub_summary.rename(columns={"max_paid_till": "paid_till"})

    denom_cutoff = MTD_START - timedelta(days=1)
    # G3: use _active_on() so Case B subs (governed by end_date) and the
    # strict > comparison are both handled correctly
    active_at_start_ids = set(
        sub_summary.loc[_active_on(sub_summary, denom_cutoff), "subscription_id"]
    )
    active_at_start = len(active_at_start_ids)

    # Churned: effective access end in MTD window AND In-Active
    churned = int(sub_summary[
        sub_summary["subscription_id"].isin(active_at_start_ids) &
        (sub_summary["paid_till"] >= MTD_START) &
        (sub_summary["paid_till"] <= YESTERDAY) &
        (sub_summary["curr_status"] == "In-Active")
    ].shape[0])

    # Renewed: placed a Renewal paid order in MTD AND was in active_at_start
    renewed_subs = set(paid_df[
        (paid_df["subscription_type"] == "Renewal") &
        (paid_df["trial_check"] == "PAID") &
        (paid_df["subscribed_date"] >= MTD_START) &
        (paid_df["subscribed_date"] <= YESTERDAY)
    ]["subscription_id"].unique())
    renewed = len(renewed_subs & active_at_start_ids)

    waiting = max(0, active_at_start - churned - renewed)

    return {
        "active_at_start": active_at_start,
        "churned":         churned,
        "renewed":         renewed,
        "waiting":         waiting,
    }


PLAN_SEGMENTS = [
    "Lite Monthly", "Lite Yearly",
    "Unlimited Monthly", "Unlimited Yearly",
    "Free Monthly",
]


def compute_per_segment_retention(df: pd.DataFrame) -> dict:
    """
    Computes MTD and Prev Month retention metrics broken down by plan_segment.
    Returns dict keyed by segment name, each with:
      renewals_mtd, renewals_prev
      churn_events_mtd, churn_events_prev
      churn_rate_mtd, churn_rate_prev
      retention_rate_mtd, retention_rate_prev
      active_at_start_mtd, active_at_start_prev
      trial_conv_mtd, trial_conv_prev  (rate, total, converted)
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()
    result = {}

    for seg in PLAN_SEGMENTS:
        seg_df = df[df["plan_segment"] == seg].copy()

        # ── Renewals ─────────────────────────────────────────────────────────
        def _renewals(start, end):
            return int(seg_df[
                (seg_df["subscription_type"] == "Renewal") &
                (seg_df["subscribed_date"] >= start) &
                (seg_df["subscribed_date"] <= end)
            ]["subscription_id"].nunique())

        renewals_mtd       = _renewals(MTD_START,        YESTERDAY)
        renewals_prev      = _renewals(PREV_MONTH_START, PREV_MONTH_END)
        renewals_prev_same = _renewals(PREV_MONTH_START, PREV_MTD_END)
        renewals_ytd       = _renewals(YTD_START,        YESTERDAY)
        renewals_lifetime  = _renewals(date(2000, 1, 1), YESTERDAY)

        # ── Paid churn (only for paid segments) ──────────────────────────────
        paid_ids_seg = set(seg_df[seg_df["trial_check"] == "PAID"]["subscription_id"].unique())
        paid_seg_df  = seg_df[seg_df["subscription_id"].isin(paid_ids_seg)]

        def _seg_churn(window_start, window_end, denom: int):
            """denom is a pre-computed integer (point-in-time or avg)."""
            if not has_paid_till or paid_seg_df.empty:
                return {"events": 0, "denominator": 0, "rate": 0.0, "retention_rate": 1.0}
            agg_cols = {
                "paid_till"  : ("paid_till",   "max"),
                "curr_status": ("curr_status", "last"),
            }
            if "end_date" in paid_seg_df.columns:
                agg_cols["end_date"] = ("end_date", "max")
            sub_s = paid_seg_df.sort_values("paid_till").groupby("subscription_id", as_index=False).agg(**agg_cols)
            # G2: for Case B subs, churn date is end_date, not paid_till
            if "end_date" in sub_s.columns:
                case_b = sub_s["end_date"].notna() & (sub_s["paid_till"] <= sub_s["end_date"])
                sub_s.loc[case_b, "paid_till"] = sub_s.loc[case_b, "end_date"]
            events = int(sub_s[
                (sub_s["paid_till"] >= window_start) &
                (sub_s["paid_till"] <= window_end) &
                (sub_s["curr_status"] == "In-Active")
            ].shape[0])
            rate  = round(events / denom, 4) if denom > 0 else 0.0
            return {
                "events": events, "denominator": denom,
                "rate": rate, "retention_rate": round(1 - rate, 4),
            }

        def _seg_active_paid_at(cutoff_date) -> int:
            if not has_paid_till or paid_seg_df.empty:
                return 0
            # Use date-aware count: only paid orders placed by cutoff_date.
            return _active_count_on(paid_seg_df, cutoff_date)

        def _avg_seg_active_paid_in_period(period_start, period_end) -> int:
            month_starts = _month_starts_in_range(period_start, period_end)
            counts = [_seg_active_paid_at(ms - timedelta(days=1)) for ms in month_starts]
            return round(sum(counts) / len(counts)) if counts else 0

        earliest_seg = (
            paid_seg_df["subscribed_date"].min()
            if not paid_seg_df.empty and has_paid_till
            else date(2000, 1, 1)
        )
        churn_yesterday = _seg_churn(YESTERDAY,        YESTERDAY,      _seg_active_paid_at(YESTERDAY        - timedelta(days=1)))
        churn_mtd       = _seg_churn(MTD_START,        YESTERDAY,      _seg_active_paid_at(MTD_START        - timedelta(days=1)))
        churn_prev      = _seg_churn(PREV_MONTH_START, PREV_MONTH_END, _seg_active_paid_at(PREV_MONTH_START - timedelta(days=1)))
        churn_prev_same = _seg_churn(PREV_MONTH_START, PREV_MTD_END,   _seg_active_paid_at(PREV_MONTH_START - timedelta(days=1)))
        churn_ytd       = _seg_churn(YTD_START,        YESTERDAY,      _avg_seg_active_paid_in_period(YTD_START,    YESTERDAY))
        churn_lifetime  = _seg_churn(date(2000, 1, 1), YESTERDAY,      _avg_seg_active_paid_in_period(earliest_seg, YESTERDAY))

        # ── Trial→Paid conversion ──────────────────────────────────────────
        def _seg_trial_conv(window_start, window_end):
            t_rows = seg_df[seg_df["subscription_type"] == "Trial"].copy()
            if t_rows.empty:
                return {"total": 0, "converted": 0, "rate": None}
            # G4: derive trial_end from data (same logic as compute_trial_to_paid_conversion)
            t_starts = t_rows.groupby("subscription_id", as_index=False).agg(
                trial_start     = ("subscribed_date", "min"),
                trial_paid_till = ("paid_till",       "max"),
            )
            t_first_paid = (
                seg_df[seg_df["trial_check"] == "PAID"]
                .groupby("subscription_id", as_index=False)
                .agg(first_paid_date=("subscribed_date", "min"))
            )
            t_subs = t_starts.merge(t_first_paid, on="subscription_id", how="left")
            def _seg_trial_end(row):
                if pd.notna(row["first_paid_date"]):
                    return row["first_paid_date"]
                if pd.notna(row["trial_paid_till"]):
                    return row["trial_paid_till"]
                return row["trial_start"] + timedelta(days=7)
            t_subs["trial_end"] = t_subs.apply(_seg_trial_end, axis=1)
            denom_ids = set(t_subs[
                (t_subs["trial_end"] >= window_start) &
                (t_subs["trial_end"] <= window_end)
            ]["subscription_id"])
            total = len(denom_ids)
            if total == 0:
                return {"total": 0, "converted": 0, "rate": None}
            paid_in_win = set(seg_df[
                (seg_df["trial_check"] == "PAID") &
                (seg_df["subscribed_date"] >= window_start) &
                (seg_df["subscribed_date"] <= window_end)
            ]["subscription_id"])
            converted = len(denom_ids & paid_in_win)
            return {"total": total, "converted": converted, "rate": round(converted/total, 4)}

        t_conv_mtd       = _seg_trial_conv(MTD_START,        YESTERDAY)
        t_conv_prev      = _seg_trial_conv(PREV_MONTH_START, PREV_MONTH_END)
        t_conv_prev_same = _seg_trial_conv(PREV_MONTH_START, PREV_MTD_END)
        t_conv_ytd       = _seg_trial_conv(YTD_START,        YESTERDAY)
        t_conv_lifetime  = _seg_trial_conv(date(2000, 1, 1), YESTERDAY)

        result[seg] = {
            "renewals_mtd":         renewals_mtd,
            "renewals_prev":        renewals_prev,
            "renewals_prev_same":   renewals_prev_same,
            "renewals_ytd":         renewals_ytd,
            "renewals_lifetime":    renewals_lifetime,
            "churn_yesterday":      churn_yesterday,
            "churn_mtd":            churn_mtd,
            "churn_prev":           churn_prev,
            "churn_prev_same":      churn_prev_same,
            "churn_ytd":            churn_ytd,
            "churn_lifetime":       churn_lifetime,
            "trial_conv_mtd":       t_conv_mtd,
            "trial_conv_prev":      t_conv_prev,
            "trial_conv_prev_same": t_conv_prev_same,
            "trial_conv_ytd":       t_conv_ytd,
            "trial_conv_lifetime":  t_conv_lifetime,
        }

    return result


def compute_cancellations(df: pd.DataFrame) -> dict:
    """
    Cancellation and refund metrics for all time windows.

    count         : number of orders with order_status in ('Cancelled', 'Refund')
    refund_amount : sum of SBO_RefundAmount on those orders
    """
    if "order_status" not in df.columns:
        empty = {"count": 0, "refund_amount": 0.0}
        return {w: dict(empty) for w in ["yesterday","mtd","prev_full","prev_same","ytd","lifetime"]}

    cancelled = df[df["order_status"].isin(["Cancelled", "Refund"])].copy()
    # Bucket by refund_date when available (consistent with compute_revenue),
    # falling back to subscribed_date for rows where refund_date is absent.
    date_col = "refund_date" if "refund_date" in cancelled.columns else "subscribed_date"
    if date_col == "refund_date":
        cancelled["_bucket_date"] = cancelled["refund_date"].fillna(cancelled["subscribed_date"])
    else:
        cancelled["_bucket_date"] = cancelled["subscribed_date"]

    def _window(start, end):
        w = cancelled[
            (cancelled["_bucket_date"] >= start) &
            (cancelled["_bucket_date"] <= end)
        ]
        return {
            "count"         : int(len(w)),
            "refund_amount" : round(float(w["refund_amount"].sum()), 2),
        }

    return {
        "yesterday": _window(YESTERDAY,       YESTERDAY),
        "mtd":       _window(MTD_START,        YESTERDAY),
        "prev_full": _window(PREV_MONTH_START, PREV_MONTH_END),
        "prev_same": _window(PREV_MONTH_START, PREV_MTD_END),
        "ytd":       _window(YTD_START,        YESTERDAY),
        "lifetime":  _window(date(2000, 1, 1), YESTERDAY),
    }


def compute_active_by_plan(df: pd.DataFrame) -> dict:
    """
    Month-on-month active subscriber counts per plan segment + Trials.

    For each first-of-month from Jan 1 of the current year through MTD_START,
    counts subscriptions that were active at the START of that day (i.e. their
    paid access had not yet expired), broken down by plan segment and trial status.

    Active at start of day D  =  paid_till > (D − 1 day)
    (same strict-greater-than logic as _active_on() everywhere else)

    Non-trial segments (PLAN_SEGMENTS): subscriptions are classified by their
    current/most-recent plan_segment.  If a sub upgraded mid-year it appears in
    its current tier for all historical months — a known simplification.

    Trials: unconverted trial subscriptions active at D.
      Criteria: (a) has a Trial order, (b) trial paid_till > D-1,
                (c) no paid order was placed before D (not yet converted).

    Returns
    -------
    dict[date, dict[str, int]]
        Keys are date objects (Jan 1, Feb 1, ..., MTD_START).
        Values map each plan segment label + "Trials" to a subscriber count.
    """
    has_paid_till = "paid_till" in df.columns and df["paid_till"].notna().any()
    if not has_paid_till:
        return {}

    # Month starts: Jan 1 of current year through MTD_START (inclusive)
    year_start  = date(YESTERDAY.year, 1, 1)
    month_dates = _month_starts_in_range(year_start, MTD_START)

    # ── Deduplicate: one row per subscription_id ──────────────────────────────
    # Take the most-recent plan_segment and max paid_till across all orders.
    agg_cols = {
        "plan_segment": ("plan_segment", "last"),
        "paid_till":    ("paid_till",    "max"),
    }
    if "end_date" in df.columns:
        agg_cols["end_date"] = ("end_date", "max")

    sub_summary = (
        df.sort_values("subscribed_date")
          .groupby("subscription_id", as_index=False)
          .agg(**agg_cols)
    )

    # Apply Case B: end_date governs access when set and <= paid_till
    if "end_date" in sub_summary.columns:
        case_b = (
            sub_summary["end_date"].notna() &
            (sub_summary["paid_till"] <= sub_summary["end_date"])
        )
        sub_summary.loc[case_b, "paid_till"] = sub_summary.loc[case_b, "end_date"]

    # Drop end_date so active check uses the already-corrected paid_till only
    sub_for_check = sub_summary[["subscription_id", "plan_segment", "paid_till"]].copy()

    # ── Trial subscription data ───────────────────────────────────────────────
    trial_sub_ids = set(df[df["subscription_type"] == "Trial"]["subscription_id"].unique())
    # Max paid_till per trial subscription (controls when trial expires).
    # Restricted to Trial orders only — paid renewal orders on the same
    # subscription_id have a much later paid_till and would inflate historical
    # trial active counts for subscriptions that later converted.
    trial_pt = (
        df[
            df["subscription_id"].isin(trial_sub_ids) &
            (df["subscription_type"] == "Trial")
        ]
        .groupby("subscription_id")["paid_till"]
        .max()
        .to_dict()
    )
    # Earliest trial order date per subscription — used to exclude trials that
    # started after a given date D from historical active counts.
    trial_start_dates = (
        df[
            df["subscription_id"].isin(trial_sub_ids) &
            (df["subscription_type"] == "Trial")
        ]
        .groupby("subscription_id")["subscribed_date"]
        .min()
        .to_dict()
    )
    # Subscriptions that ever placed a paid order — used to detect conversion
    paid_sub_ids = set(df[df["trial_check"] == "PAID"]["subscription_id"].unique())
    # First paid order date per subscription (to check if converted before D)
    first_paid = (
        df[df["trial_check"] == "PAID"]
        .groupby("subscription_id")["subscribed_date"]
        .min()
        .to_dict()
    )

    # ── Compute counts for each month start ───────────────────────────────────
    result = {}
    for d in month_dates:
        cutoff = d - timedelta(days=1)   # paid_till > cutoff  <=>  active at start of d

        # Subscription IDs still in unconverted trial status at D.
        # A trial sub's plan_segment may match a PLAN_SEGMENT (e.g. "Lite Monthly"),
        # so it would be double-counted if included in both the segment count and
        # the Trials count. We exclude these from PLAN_SEGMENTS counts.
        still_in_trial_at_d = set()
        for sub_id in trial_sub_ids:
            if sub_id in paid_sub_ids:
                fp = first_paid.get(sub_id)
                if fp is not None and fp < d:
                    continue  # converted before d — counted in plan segment, not Trials
            still_in_trial_at_d.add(sub_id)

        counts = {}

        # Active subs per plan segment — exclude those still in trial at D.
        # Segment membership comes from sub_for_check (current plan, known
        # simplification). Active check uses _active_count_on so that orders
        # placed after D are excluded — prevents future renewals from inflating
        # the count for subscriptions that had lapsed by date D.
        for seg in PLAN_SEGMENTS:
            seg_sub_ids = set(sub_for_check[
                (sub_for_check["plan_segment"] == seg) &
                (~sub_for_check["subscription_id"].isin(still_in_trial_at_d))
            ]["subscription_id"])
            if not seg_sub_ids:
                counts[seg] = 0
                continue
            counts[seg] = _active_count_on(
                df[df["subscription_id"].isin(seg_sub_ids)], cutoff
            )

        # Active unconverted trials at D — use _active_count_on for consistency
        # with how plan segments are counted (date-aware, Case A/B applied).
        trial_eligible_ids = [
            sub_id for sub_id in still_in_trial_at_d
            if sub_id in trial_pt
        ]
        if trial_eligible_ids:
            counts["Trials"] = _active_count_on(
                df[
                    df["subscription_id"].isin(trial_eligible_ids) &
                    (df["subscription_type"] == "Trial")
                ],
                cutoff,
            )
        else:
            counts["Trials"] = 0

        result[d] = counts

    return result


def compute_all_kpis(df: pd.DataFrame, config: dict) -> dict:
    """
    Master function — runs all KPI computations and returns a single dict.

    Parameters
    ----------
    df     : full raw dataframe loaded from MySQL / Excel source sheet
    config : dict of thresholds read from the Config sheet
             e.g. {"churn_rate_warn": 0.05, "trial_conv_warn": 0.30, ...}

    Returns
    -------
    dict  : all KPI metrics, metadata labels, and computed values
    """
    from datetime import datetime, timezone

    # ── Metadata labels ────────────────────────────────────────────────────────
    as_of_str    = YESTERDAY.strftime("%Y-%m-%d")
    mtd_start_s  = MTD_START.strftime("%Y-%m-%d")
    days_elapsed = (YESTERDAY - MTD_START).days + 1

    lbl_yesterday = _fmt_compact(YESTERDAY)
    lbl_mtd       = compact_range(MTD_START, YESTERDAY)
    lbl_prev_full = compact_range(PREV_MONTH_START, PREV_MONTH_END)
    lbl_prev_same = compact_range(PREV_MONTH_START, PREV_MTD_END)
    lbl_ytd       = compact_range(YTD_START, YESTERDAY)

    # Data currency check
    data_lag_days = (TODAY - YESTERDAY).days
    data_stale    = data_lag_days > 1

    generated_on = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Clean dataframe: Confirmed / Completed orders only ────────────────────
    # All KPIs operate on confirmed/completed orders to exclude cancelled,
    # refunded, or pending transactions from active counts and rate calculations.
    # compute_revenue and compute_cancellations receive df (full) for refund
    # bucketing — they handle the filtering internally.
    if "order_status" in df.columns:
        df_clean = df[df["order_status"].isin(["Confirmed", "Completed"])].copy()
    else:
        df_clean = df.copy()

    # ── Compute all KPIs ───────────────────────────────────────────────────────
    return {
        # ── Metadata ──────────────────────────────────────────────────────────
        "as_of"           : as_of_str,
        "mtd_start"       : mtd_start_s,
        "prev_month_start": PREV_MONTH_START.strftime("%Y-%m-%d"),
        "prev_month_end"  : PREV_MONTH_END.strftime("%Y-%m-%d"),
        "prev_same_end"   : PREV_MTD_END.strftime("%Y-%m-%d"),
        "days_elapsed_mtd": days_elapsed,
        "lbl_yesterday"   : lbl_yesterday,
        "lbl_mtd"         : lbl_mtd,
        "lbl_prev_full"   : lbl_prev_full,
        "lbl_prev_same"   : lbl_prev_same,
        "lbl_ytd"         : lbl_ytd,
        "data_stale"      : data_stale,
        "data_lag_days"   : data_lag_days,
        "tz_label"        : "Pacific Time (PT)",
        "generated_on"    : generated_on,

        # ── Core KPIs ─────────────────────────────────────────────────────────
        "active"             : compute_active_subscribers(df_clean),
        "subscription_counts": compute_subscription_counts(df_clean),
        "subscription_types" : compute_subscription_type_counts(df_clean),
        "revenue"            : compute_revenue(df_clean, df),
        "paid_churn"         : compute_paid_churn(df_clean),
        "trial_conversion" : compute_trial_to_paid_conversion(df_clean),
        "free_conversion"  : compute_free_to_paid_conversion(df_clean),
        "trial_pipeline"   : compute_trial_pipeline(df_clean),
        "plan_performance" : compute_plan_performance(df_clean),
        "funnel"           : compute_funnel(df_clean),
        "monthly_trend"    : compute_monthly_trend(df_clean),
        "renewal_state"    : compute_renewal_state(df_clean),
        "segment_retention": compute_per_segment_retention(df_clean),
        "active_by_plan"   : compute_active_by_plan(df_clean),
        "cancellations"    : compute_cancellations(df),
    }