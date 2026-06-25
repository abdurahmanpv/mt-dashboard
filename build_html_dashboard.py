"""
build_html_dashboard.py
=======================
Generates a self-contained 4-tab interactive HTML dashboard mirroring the Excel workbook:
  Tab 1 — Executive Dashboard
  Tab 2 — Plan Performance
  Tab 3 — Retention & Renewals
  Tab 4 — Raw Data (CSV download only)

Template: _dashboard_template.html (same directory).
Uses @@MARKER@@ substitution throughout — no f-strings.
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _sanitize(v: Any) -> Any:
    import numpy as np
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            out[str(k) if isinstance(k, date) else k] = _sanitize(val)
        return out
    if isinstance(v, list):
        return [_sanitize(i) for i in v]
    if isinstance(v, date):
        return str(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        fv = float(v)
        return None if (math.isnan(fv) or math.isinf(fv)) else fv
    if isinstance(v, np.ndarray):
        return _sanitize(v.tolist())
    if isinstance(v, pd.DataFrame):
        return []
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _j(v: Any) -> str:
    return json.dumps(_sanitize(v))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _pct(v) -> str:
    try:
        f = float(v)
        return "—" if (math.isnan(f) or math.isinf(f)) else f"{f * 100:.2f}%"
    except Exception:
        return "—"


def _num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(v):,}"
    except Exception:
        return str(v)


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "—"


def _delta(a, b, money=False) -> str:
    try:
        a, b = float(a), float(b)
        if b == 0:
            return "—"
        delta = a - b
        pct = delta / b * 100
        sign = "▲" if delta >= 0 else "▼"
        abs_d = f"${abs(delta):,.0f}" if money else f"{abs(delta):,.0f}"
        return f"{sign} {abs_d} ({abs(pct):.1f}%)"
    except Exception:
        return "—"


def _rows_to_records(df: pd.DataFrame, cols: list) -> list:
    available = [c for c in cols if c in df.columns]
    if not available:
        return []
    return df[available].astype(str).fillna("").to_dict("records")


# ---------------------------------------------------------------------------
# Drill-down extraction
# ---------------------------------------------------------------------------

DRILL_COLS = ["subscription_id", "user_id", "plan_segment", "subscription_type",
              "subscribed_date", "paid_till", "curr_status"]


def _extract_drilldown(df_clean: pd.DataFrame, kpi: dict,
                       trial_df, churn_data: dict,
                       df_raw: pd.DataFrame = None) -> dict:
    from kpi_engine import MTD_START, YESTERDAY, PLAN_SEGMENTS

    dd: dict = {}
    cutoff = MTD_START - timedelta(days=1)

    # Active now
    if "paid_till" in df_clean.columns:
        agg = df_clean.groupby("subscription_id", as_index=False).agg(
            paid_till=("paid_till", "max"), user_id=("user_id", "first"),
            plan_segment=("plan_segment", "last"), curr_status=("curr_status", "last"))
        ids = set(agg[agg["paid_till"] > YESTERDAY]["subscription_id"])
        dd["active_now"] = _rows_to_records(agg[agg["subscription_id"].isin(ids)], DRILL_COLS)
    else:
        dd["active_now"] = []

    # Active at month start
    elig = df_clean[df_clean["subscribed_date"] <= cutoff].copy()
    if not elig.empty and "paid_till" in elig.columns:
        agg2 = elig.groupby("subscription_id", as_index=False).agg(
            paid_till=("paid_till", "max"), user_id=("user_id", "first"),
            plan_segment=("plan_segment", "last"), curr_status=("curr_status", "last"))
        ids2 = set(agg2[agg2["paid_till"] > cutoff]["subscription_id"])
        dd["active_at_month_start"] = _rows_to_records(
            agg2[agg2["subscription_id"].isin(ids2)], DRILL_COLS)
    else:
        dd["active_at_month_start"] = []

    # Paid churn MTD — churn_data keys are "paid" and "trial"
    paid_df = churn_data.get("paid") if isinstance(churn_data, dict) else None
    if isinstance(paid_df, pd.DataFrame) and not paid_df.empty:
        dd["churned_mtd"] = _rows_to_records(paid_df, DRILL_COLS + ["churn_date"])
    else:
        dd["churned_mtd"] = []

    trial_churn_df = churn_data.get("trial") if isinstance(churn_data, dict) else None
    if isinstance(trial_churn_df, pd.DataFrame) and not trial_churn_df.empty:
        dd["churned_trial"] = _rows_to_records(trial_churn_df, DRILL_COLS)
    else:
        dd["churned_trial"] = []

    # Trial pipeline
    if trial_df is not None and not getattr(trial_df, "empty", True):
        trial_cols = ["subscription_id", "user_id", "plan_segment",
                      "subscribed_date", "paid_till", "days_remaining"]
        dd["trial_pipeline"] = _rows_to_records(trial_df, trial_cols)
    else:
        dd["trial_pipeline"] = []

    # Active by plan breakdown (per-plan drill cards in Executive tab)
    active_by_plan = kpi.get("active_by_plan", {})
    if active_by_plan:
        latest_date = max(active_by_plan.keys())
        dd["active_by_plan_date"] = str(latest_date)
        plan_drill = {}
        for seg in list(PLAN_SEGMENTS) + ["Trials"]:
            seg_rows: list = []
            if seg == "Trials":
                if "subscription_type" in df_clean.columns:
                    tids = set(df_clean[df_clean["subscription_type"] == "Trial"]["subscription_id"])
                    te = df_clean[df_clean["subscription_id"].isin(tids) &
                                  (df_clean["subscribed_date"] <= cutoff)]
                    if not te.empty and "paid_till" in te.columns:
                        ta = te.groupby("subscription_id", as_index=False).agg(
                            paid_till=("paid_till", "max"), user_id=("user_id", "first"))
                        seg_rows = _rows_to_records(
                            ta[ta["paid_till"] > cutoff],
                            ["subscription_id", "user_id", "paid_till"])
            else:
                s = df_clean[(df_clean["plan_segment"] == seg) &
                             (df_clean["subscribed_date"] <= cutoff)]
                if not s.empty and "paid_till" in s.columns:
                    sa = s.groupby("subscription_id", as_index=False).agg(
                        paid_till=("paid_till", "max"), user_id=("user_id", "first"),
                        curr_status=("curr_status", "last"))
                    seg_rows = _rows_to_records(
                        sa[sa["paid_till"] > cutoff],
                        ["subscription_id", "user_id", "plan_segment",
                         "paid_till", "curr_status"])
            plan_drill[seg] = seg_rows
        dd["active_by_plan_breakdown"] = plan_drill
        dd["raw_data_row_count"] = len(df_clean)
    else:
        dd["active_by_plan_date"] = ""
        dd["active_by_plan_breakdown"] = {}
        dd["raw_data_row_count"] = 0

    # Raw data — all columns including PII, no filtering
    # Use df_raw (full dataset) if provided, else fall back to df_clean
    src = df_raw if isinstance(df_raw, pd.DataFrame) and not df_raw.empty else df_clean
    dd["raw_data_cols"]    = list(src.columns)
    dd["raw_data_records"] = src.astype(str).fillna("").to_dict("records")
    dd["raw_data_row_count"] = len(src)

    return dd


# ---------------------------------------------------------------------------
# Python-built HTML table fragments
# ---------------------------------------------------------------------------

PLANS = ["Lite Monthly", "Lite Yearly", "Unlimited Monthly",
         "Unlimited Yearly", "Free Monthly"]
WINDOWS_6 = [("yesterday", "Yesterday"), ("mtd", "MTD"), ("prev_full", "Prev Month"),
              ("prev_same", "Same Period"), ("ytd", "YTD"), ("lifetime", "Lifetime")]


def _td(v: str) -> str:
    return "<td>" + str(v) + "</td>"


def _th(v: str) -> str:
    return "<th>" + str(v) + "</th>"


def _sep_row(label: str, colspan: int) -> str:
    return ("<tr style='background:#EEF2FF'>"
            "<td colspan='" + str(colspan) + "' "
            "style='font-weight:700;color:#1B2A4A;padding:8px 12px'>"
            + label + "</td></tr>")


def _build_abp_html(kpi: dict) -> str:
    abp = kpi.get("active_by_plan", {})
    if not abp:
        return "<tr><td colspan='8'>No data</td></tr>"
    segs = ["Lite Monthly", "Lite Yearly", "Unlimited Monthly",
            "Unlimited Yearly", "Free Monthly", "Trials"]
    hdr = "<tr>" + _th("Month")
    for s in segs:
        hdr += _th(s)
    hdr += _th("Total") + "</tr>"
    rows = hdr
    for m in sorted(abp.keys()):
        counts = abp[m]
        total = sum(counts.values())
        rows += "<tr>" + _td(str(m))
        for s in segs:
            rows += _td(f"{counts.get(s, 0):,}")
        rows += "<td><strong>" + f"{total:,}" + "</strong></td></tr>"
    return rows


def _build_pp_html(kpi: dict) -> str:
    pp = kpi.get("plan_performance", [])
    rows = ""
    for item in (pp if isinstance(pp, list) else []):
        seg    = item.get("segment", "")
        counts = item.get("counts", {})
        r      = item.get("revenue", {})
        uu     = item.get("unique_users", {})
        rows += "<tr>" + _td(seg)
        for wk, _ in WINDOWS_6:
            rows += _td(_num(counts.get(wk)))
        rows += (_td(_money(r.get("mtd", {}).get("cash")))
                 + _td(_money(r.get("mtd", {}).get("mrr")))
                 + _td(_num(uu.get("mtd")))
                 + "</tr>")
    return rows or "<tr><td colspan='10'>No data</td></tr>"


def _build_plan_cross_html(kpi: dict) -> str:
    funnel_by_plan = kpi.get("funnel", {}).get("by_plan", {})
    pp = kpi.get("plan_performance", [])
    rev_lookup = {item.get("segment", ""): item.get("revenue", {}) for item in pp}
    short = {"Lite Monthly": "Lite Mo", "Lite Yearly": "Lite Yr",
             "Unlimited Monthly": "Unlim Mo", "Unlimited Yearly": "Unlim Yr",
             "Free Monthly": "Free Mo"}
    wins3 = [("mtd", "MTD"), ("prev_full", "Prev"), ("lifetime", "All Time")]
    stages = [("trials", "Trials"), ("new_subs", "New Subs"),
              ("renewals", "Renewals"), ("paid", "Paid")]
    hdr = "<tr>" + _th("Stage")
    for plan in PLANS:
        for _, wl in wins3:
            hdr += _th(short.get(plan, plan) + " " + wl)
    hdr += "</tr>"
    rows = hdr
    for sk, sl in stages:
        rows += "<tr><td><strong>" + sl + "</strong></td>"
        for plan in PLANS:
            counts = funnel_by_plan.get(plan, {}).get(sk, {})
            for wk, _ in wins3:
                rows += _td(_num(counts.get(wk)))
        rows += "</tr>"
    rows += "<tr><td><strong>Cash Revenue ($)</strong></td>"
    for plan in PLANS:
        r = rev_lookup.get(plan, {})
        for wk, _ in wins3:
            rows += _td(_money(r.get(wk, {}).get("cash")))
    rows += "</tr>"
    return rows


def _build_perf_metrics_rows(kpi: dict) -> str:
    sc   = kpi.get("subscription_counts", {})
    st   = kpi.get("subscription_types", {})
    rev  = kpi.get("revenue", {})
    canc = kpi.get("cancellations", {})

    def _simple_row(label, source_dict, fmt_fn=_num, indent=False):
        pad = " style='padding-left:24px'" if indent else ""
        row = "<tr><td" + pad + ">" + label + "</td>"
        for wk, _ in WINDOWS_6:
            row += _td(fmt_fn(source_dict.get(wk)))
        return row + "</tr>"

    def _rev_row(label, key, fmt_fn=_money, indent=False):
        pad = " style='padding-left:24px'" if indent else ""
        row = "<tr><td" + pad + ">" + label + "</td>"
        for wk, _ in WINDOWS_6:
            row += _td(fmt_fn(rev.get(wk, {}).get(key)))
        return row + "</tr>"

    html  = _sep_row("Volume", 7)
    html += _simple_row("Unique Users",         sc.get("unique_users", {}))
    html += _simple_row("Unique Subscriptions", sc.get("unique_subs", {}))
    html += _simple_row("Total Transactions",   sc.get("total_transactions", {}))
    html += _sep_row("By Type", 7)
    html += _simple_row("Trials",            st.get("trials", {}))
    html += _simple_row("New Subscriptions", st.get("new_subs", {}))
    html += _simple_row("Renewals",          st.get("renewals", {}))
    html += _simple_row("Paid",              st.get("paid", {}))
    html += _simple_row("Free",              st.get("free", {}))
    html += _sep_row("Revenue", 7)
    html += _rev_row("Gross Cash ($)", "cash")
    html += _rev_row("Refunds ($)",    "refunds")
    html += _rev_row("Net Cash ($)",   "net_cash")
    html += _rev_row("MRR ($)",        "mrr")
    html += _sep_row("Cancellations", 7)
    html += "<tr><td>Cancelled Orders</td>"
    for wk, _ in WINDOWS_6:
        html += _td(_num(canc.get(wk, {}).get("count")))
    html += "</tr>"
    html += "<tr><td>Total Refunded ($)</td>"
    for wk, _ in WINDOWS_6:
        html += _td(_money(canc.get(wk, {}).get("refund_amount")))
    html += "</tr>"
    return html


def _build_mom_var_html(kpi: dict) -> str:
    st  = kpi.get("subscription_types", {})
    rev = kpi.get("revenue", {})

    def _row(label, mtd, pf, ps, money=False):
        fmt = _money if money else _num
        return ("<tr>" + _td(label) + _td(fmt(mtd))
                + _td(_delta(mtd, pf, money=money))
                + _td(_delta(mtd, ps, money=money)) + "</tr>")

    rows  = _row("New Subscriptions",
                 st.get("new_subs", {}).get("mtd"),
                 st.get("new_subs", {}).get("prev_full"),
                 st.get("new_subs", {}).get("prev_same"))
    rows += _row("Trials",
                 st.get("trials", {}).get("mtd"),
                 st.get("trials", {}).get("prev_full"),
                 st.get("trials", {}).get("prev_same"))
    rows += _row("Renewals",
                 st.get("renewals", {}).get("mtd"),
                 st.get("renewals", {}).get("prev_full"),
                 st.get("renewals", {}).get("prev_same"))
    rows += _row("Paid Subscribers",
                 st.get("paid", {}).get("mtd"),
                 st.get("paid", {}).get("prev_full"),
                 st.get("paid", {}).get("prev_same"))
    rows += _row("Gross Cash ($)",
                 rev.get("mtd", {}).get("cash"),
                 rev.get("prev_full", {}).get("cash"),
                 rev.get("prev_same", {}).get("cash"), money=True)
    rows += _row("Net Cash ($)",
                 rev.get("mtd", {}).get("net_cash"),
                 rev.get("prev_full", {}).get("net_cash"),
                 rev.get("prev_same", {}).get("net_cash"), money=True)
    rows += _row("MRR ($)",
                 rev.get("mtd", {}).get("mrr"),
                 rev.get("prev_full", {}).get("mrr"),
                 rev.get("prev_same", {}).get("mrr"), money=True)
    return rows


def _build_trend_html(kpi: dict) -> str:
    trend = kpi.get("monthly_trend", [])
    rows = ""
    for t in trend:
        rows += ("<tr>"
                 + _td(t.get("month_label", ""))
                 + _td(_num(t.get("new_subs")))
                 + _td(_num(t.get("trials")))
                 + _td(_num(t.get("renewals")))
                 + _td(_num(t.get("paid")))
                 + _td(_money(t.get("cash_revenue")))
                 + _td(_money(t.get("mrr_revenue")))
                 + _td(_pct(t.get("trial_conv_rate")))
                 + "</tr>")
    return rows or "<tr><td colspan='8'>No data</td></tr>"


def _build_ret_conv_html(kpi: dict) -> str:
    seg_ret = kpi.get("segment_retention", {})
    pchurn  = kpi.get("paid_churn", {})
    tconv   = kpi.get("trial_conversion", {})
    fconv   = kpi.get("free_conversion", {})
    rs      = kpi.get("renewal_state", {})
    canc    = kpi.get("cancellations", {})

    def _row(label, get_fn, fmt_fn=_num, indent=False, row_id=None):
        pad = " style='padding-left:24px'" if indent else ""
        toggle = ""
        if row_id:
            toggle = (" <button onclick=\"toggleSeg('" + row_id + "')\" "
                      "style='font-size:10px;margin-left:6px;cursor:pointer;"
                      "border:1px solid #ccc;border-radius:3px;padding:1px 5px;"
                      "background:#f5f5f5'>[+]</button>")
        row = "<tr><td" + pad + ">" + label + toggle + "</td>"
        for wk, _ in WINDOWS_6:
            row += _td(fmt_fn(get_fn(wk)))
        return row + "</tr>"

    def _seg_rows(metric_fn, fmt_fn, group_id):
        out = ""
        for seg, data in seg_ret.items():
            out += ("<tr class='seg-row seg-" + group_id
                    + "' style='display:none;background:#FAFAFA'>"
                    "<td style='padding-left:36px;color:#555'>" + seg + "</td>")
            for wk, _ in WINDOWS_6:
                out += _td(fmt_fn(metric_fn(seg, data, wk)))
            out += "</tr>"
        return out

    CHURN_WIN = {"mtd": "churn_mtd", "prev_full": "churn_prev",
                 "prev_same": "churn_prev_same", "ytd": "churn_ytd",
                 "lifetime": "churn_lifetime", "yesterday": "churn_yesterday"}
    TCONV_WIN = {"mtd": "trial_conv_mtd", "prev_full": "trial_conv_prev",
                 "prev_same": "trial_conv_prev_same", "ytd": "trial_conv_ytd",
                 "lifetime": "trial_conv_lifetime"}

    html  = _row("Renewals",
                 lambda wk: kpi.get("subscription_types", {}).get("renewals", {}).get(wk),
                 row_id="ren")
    html += _seg_rows(
        lambda seg, data, wk: data.get({"mtd": "renewals_mtd", "prev_full": "renewals_prev",
                                         "ytd": "renewals_ytd", "lifetime": "renewals_lifetime"}.get(wk, ""), None),
        _num, "ren")

    html += _row("Paid Churn Events",
                 lambda wk: pchurn.get(wk, {}).get("events"), row_id="pce")
    html += _seg_rows(
        lambda seg, data, wk: data.get(CHURN_WIN.get(wk, ""), {}).get("events"),
        _num, "pce")

    html += _row("Paid Churn Rate",
                 lambda wk: pchurn.get(wk, {}).get("rate"), fmt_fn=_pct, row_id="pcr")
    html += _seg_rows(
        lambda seg, data, wk: data.get(CHURN_WIN.get(wk, ""), {}).get("rate"),
        _pct, "pcr")

    html += _row("Paid Retention Rate",
                 lambda wk: pchurn.get(wk, {}).get("retention_rate"), fmt_fn=_pct, row_id="prr")
    html += _seg_rows(
        lambda seg, data, wk: data.get(CHURN_WIN.get(wk, ""), {}).get("retention_rate"),
        _pct, "prr")

    html += _sep_row("Renewal State (MTD)", 7)
    html += _row("Active at Month Start (Paid)",
                 lambda wk: rs.get("active_at_start") if wk == "mtd" else None)
    html += _row("Churned",
                 lambda wk: rs.get("churned") if wk == "mtd" else None)
    html += _row("Renewed",
                 lambda wk: rs.get("renewed") if wk == "mtd" else None)
    html += _row("Waiting",
                 lambda wk: rs.get("waiting") if wk == "mtd" else None)

    html += _sep_row("Conversion", 7)
    html += _row("Trial to Paid Conv%",
                 lambda wk: tconv.get(wk, {}).get("rate"), fmt_fn=_pct, row_id="tc")
    html += _seg_rows(
        lambda seg, data, wk: data.get(TCONV_WIN.get(wk, ""), {}).get("rate"),
        _pct, "tc")
    html += _row("  Trials Ended (denom)",
                 lambda wk: tconv.get(wk, {}).get("total"), indent=True)
    html += _row("  Converted (num)",
                 lambda wk: tconv.get(wk, {}).get("converted"), indent=True)
    html += _row("Free to Paid Conv%",
                 lambda wk: fconv.get(wk, {}).get("rate"), fmt_fn=_pct)
    html += _row("  Active Free (denom)",
                 lambda wk: fconv.get(wk, {}).get("total"), indent=True)
    html += _row("  Converted (num)",
                 lambda wk: fconv.get(wk, {}).get("converted"), indent=True)
    html += _sep_row("Cancellations", 7)
    html += _row("Cancelled Orders",
                 lambda wk: canc.get(wk, {}).get("count"))
    html += _row("Total Refunded ($)",
                 lambda wk: canc.get(wk, {}).get("refund_amount"), fmt_fn=_money)
    return html


def _build_mom_ret_html(kpi: dict) -> str:
    pchurn = kpi.get("paid_churn", {})
    st     = kpi.get("subscription_types", {})

    def _row(label, mtd, pf, ps, money=False):
        fmt = _money if money else _num
        return ("<tr>" + _td(label) + _td(fmt(mtd))
                + _td(_delta(mtd, pf, money=money))
                + _td(_delta(mtd, ps, money=money)) + "</tr>")

    rows  = _row("Renewals",
                 st.get("renewals", {}).get("mtd"),
                 st.get("renewals", {}).get("prev_full"),
                 st.get("renewals", {}).get("prev_same"))
    rows += _row("Paid Churn Events",
                 pchurn.get("mtd", {}).get("events"),
                 pchurn.get("prev_full", {}).get("events"),
                 pchurn.get("prev_same", {}).get("events"))
    return rows


def _build_metric_val_html(kpi: dict) -> str:
    seg_ret = kpi.get("segment_retention", {})
    pchurn  = kpi.get("paid_churn", {})
    tconv   = kpi.get("trial_conversion", {})
    rs      = kpi.get("renewal_state", {})

    def _row(label, ce, denom, rate, ren, td, cp, tcr, bold=False):
        tag = "strong" if bold else "span"
        return ("<tr>"
                + _td("<" + tag + ">" + label + "</" + tag + ">")
                + _td(_num(ce)) + _td(_num(denom))
                + "<td class='rate'>" + _pct(rate) + "</td>"
                + _td(_num(ren)) + _td(_num(td)) + _td(_num(cp))
                + "<td class='rate'>" + _pct(tcr) + "</td>"
                + "</tr>")

    rows = _row("Overall",
                pchurn.get("mtd", {}).get("events"),
                pchurn.get("mtd", {}).get("denominator"),
                pchurn.get("mtd", {}).get("rate"),
                rs.get("renewed"),
                tconv.get("mtd", {}).get("total"),
                tconv.get("mtd", {}).get("converted"),
                tconv.get("mtd", {}).get("rate"),
                bold=True)
    for seg, data in seg_ret.items():
        chm = data.get("churn_mtd", {})
        tcm = data.get("trial_conv_mtd", {})
        rows += _row(seg,
                     chm.get("events"), chm.get("denominator"), chm.get("rate"),
                     data.get("renewals_mtd"),
                     tcm.get("total"), tcm.get("converted"), tcm.get("rate"))
    return rows


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_html(df_kpi_clean: pd.DataFrame, kpi: dict,
               trial_pipeline_df, churn_data: dict, output_path: str,
               df_raw: pd.DataFrame = None) -> str:

    template_path = Path(__file__).parent / "_dashboard_template.html"
    if not template_path.exists():
        raise FileNotFoundError(
            "Template not found: " + str(template_path) + "\n"
            "Make sure _dashboard_template.html is in the same directory."
        )
    html = template_path.read_text(encoding="utf-8")

    labels = {
        "mtd":  kpi.get("lbl_mtd",       "MTD"),
        "prev": kpi.get("lbl_prev_full",  "Prev Month"),
        "same": kpi.get("lbl_prev_same",  "Same Period"),
        "ytd":  kpi.get("lbl_ytd",        "YTD"),
    }

    dd        = _extract_drilldown(df_kpi_clean, kpi, trial_pipeline_df, churn_data, df_raw=df_raw)
    data_blob = _j({"kpi": kpi, "drilldown": dd})
    perf_hdr  = "<th>Metric</th>" + "".join(
        "<th>" + lbl + "</th>" for _, lbl in WINDOWS_6)

    html = html.replace("@@AS_OF@@",           str(kpi.get("as_of", "")))
    html = html.replace("@@TZ@@",              str(kpi.get("tz_label", "PT")))
    html = html.replace("@@GENERATED_ON@@",    str(kpi.get("generated_on", "")))
    html = html.replace("@@MTD_START@@",       str(kpi.get("mtd_start", "")))
    html = html.replace("@@LBL_MTD@@",         labels["mtd"])
    html = html.replace("@@PERF_HDR@@",        perf_hdr)
    html = html.replace("@@PERF_ROWS@@",       _build_perf_metrics_rows(kpi))
    html = html.replace("@@MOM_VAR_HTML@@",    _build_mom_var_html(kpi))
    html = html.replace("@@TREND_HTML@@",      _build_trend_html(kpi))
    html = html.replace("@@ABP_HTML@@",        _build_abp_html(kpi))
    html = html.replace("@@ABP_DATE@@",        dd.get("active_by_plan_date", ""))
    html = html.replace("@@PP_HTML@@",         _build_pp_html(kpi))
    html = html.replace("@@PLAN_CROSS_HTML@@", _build_plan_cross_html(kpi))
    html = html.replace("@@RET_CONV_HTML@@",   _build_ret_conv_html(kpi))
    html = html.replace("@@MOM_RET_HTML@@",    _build_mom_ret_html(kpi))
    html = html.replace("@@METRIC_VAL_HTML@@", _build_metric_val_html(kpi))
    html = html.replace("@@DATA_BLOB@@",       data_blob)

    Path(output_path).write_text(html, encoding="utf-8")
    return str(Path(output_path).resolve())
