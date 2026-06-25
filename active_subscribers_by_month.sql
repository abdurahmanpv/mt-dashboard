-- ============================================================================
-- ACTIVE SUBSCRIBERS BY MONTH — PLAN SEGMENT BREAKDOWN
-- ============================================================================
-- Each row = one month. The check_date is the last day of the prior month,
-- i.e. "who was active at the very start of that month."
--
--   Oct 2025 row  →  active on Sep 30 2025
--   Nov 2025 row  →  active on Oct 31 2025
--   ...
--   Jun 2026 row  →  active on May 31 2026
--   Jun 17 2026   →  active on Jun 17 2026  (current MTD snapshot)
--
-- Active-on-date logic (mirrors dashboard exactly):
--   Case A: end_date IS NULL OR paid_till > end_date
--           → subscription is active if paid_till > check_date
--   Case B: end_date IS NOT NULL AND paid_till <= end_date
--           → subscription is active if end_date > check_date
--
-- NOTE: strictly GREATER THAN check_date (not >=).
--   "Active on D" means valid access through ALL of day D,
--   i.e. paid_till >= D+1 00:00:00.  A subscription with
--   paid_till = check_date expired ON that day and is not active.
--
-- Category is evaluated AS OF the check_date (point-in-time):
--   Free Monthly      : Free plan (SUB_SubscriptionName = 'Free')
--   Lite Monthly      : Lite plan, Monthly billing, first paid order <= check_date
--   Lite Yearly       : Lite plan, Yearly billing,  first paid order <= check_date
--   Unlimited Monthly : Unlimited plan, Monthly,    first paid order <= check_date
--   Unlimited Yearly  : Unlimited plan, Yearly,     first paid order <= check_date
--   Trial             : Lite or Unlimited plan with NO paid order yet as of check_date
--   Other             : Any plan name not in the above (should be 0 — investigate if not)
-- ============================================================================

WITH

-- ── 1. Check dates ────────────────────────────────────────────────────────────
check_dates AS (
    SELECT 'Oct 2025'    AS month_label, DATE('2025-09-30') AS check_date UNION ALL
    SELECT 'Nov 2025',                   DATE('2025-10-31')               UNION ALL
    SELECT 'Dec 2025',                   DATE('2025-11-30')               UNION ALL
    SELECT 'Jan 2026',                   DATE('2025-12-31')               UNION ALL
    SELECT 'Feb 2026',                   DATE('2026-01-31')               UNION ALL
    SELECT 'Mar 2026',                   DATE('2026-02-28')               UNION ALL
    SELECT 'Apr 2026',                   DATE('2026-03-31')               UNION ALL
    SELECT 'May 2026',                   DATE('2026-04-30')               UNION ALL
    SELECT 'Jun 2026',                   DATE('2026-05-31')               UNION ALL
    SELECT 'Jun 17 2026',               DATE('2026-06-17')
),

-- ── 2. One row per subscription — static fields only ─────────────────────────
-- (paid_till is current value from the DB, used for the active check)
sub_base AS (
    SELECT
        tus.USB_SubscriptionID                                                       AS subscription_id,
        DATE(CONVERT_TZ(tus.USB_PaidTill,       'GMT', 'America/Los_Angeles'))      AS paid_till,
        DATE(CONVERT_TZ(tus.USB_EndDateTime,    'GMT', 'America/Los_Angeles'))      AS end_date,
        -- Use this to exclude subscriptions that hadn't started yet on the check_date
        DATE(CONVERT_TZ(tus.USB_CreatedDateTime,'GMT', 'America/Los_Angeles'))      AS created_date,
        ts.SUB_SubscriptionName                                                      AS plan_name,
        ts.SUB_Duration                                                              AS duration
    FROM WAY_SUBSCRIPTIONS.tbl_user_subscription  tus
    JOIN WAY_SUBSCRIPTIONS.tbl_subscription       ts
      ON tus.USB_SUB_SubscriptionID = ts.SUB_SubscriptionID
),

-- ── 3. First paid order date per subscription ─────────────────────────────────
-- Used to determine point-in-time category:
--   If first_paid_date <= check_date  → subscriber had already converted by that date
--   If first_paid_date >  check_date
--   OR first_paid_date IS NULL        → still in trial as of that date
first_paid AS (
    SELECT
        SBO_USB_SubscriptionID                                                          AS subscription_id,
        MIN(DATE(CONVERT_TZ(SBO_SubscribedDate, 'GMT', 'America/Los_Angeles')))        AS first_paid_date
    FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders
    WHERE SBO_OrderStatus IN ('Confirmed', 'Completed')
      AND SBO_TotalAmount > 0
    GROUP BY SBO_USB_SubscriptionID
)

-- ── 4. Final pivot ────────────────────────────────────────────────────────────
SELECT
    cd.month_label,
    cd.check_date,

    -- Free plan (no trial, no payment ever)
    COUNT(CASE
        WHEN sb.plan_name = 'Free'
        THEN 1 END)                                                         AS free_monthly,

    -- Lite Monthly — had converted to paid by check_date
    COUNT(CASE
        WHEN sb.plan_name = 'Lite' AND sb.duration = 'Monthly'
         AND fp.first_paid_date IS NOT NULL
         AND fp.first_paid_date <= cd.check_date
        THEN 1 END)                                                         AS lite_monthly,

    -- Lite Yearly — had converted to paid by check_date
    COUNT(CASE
        WHEN sb.plan_name = 'Lite' AND sb.duration = 'Yearly'
         AND fp.first_paid_date IS NOT NULL
         AND fp.first_paid_date <= cd.check_date
        THEN 1 END)                                                         AS lite_yearly,

    -- Unlimited Monthly — had converted to paid by check_date
    COUNT(CASE
        WHEN sb.plan_name = 'Unlimited' AND sb.duration = 'Monthly'
         AND fp.first_paid_date IS NOT NULL
         AND fp.first_paid_date <= cd.check_date
        THEN 1 END)                                                         AS unlimited_monthly,

    -- Unlimited Yearly — had converted to paid by check_date
    COUNT(CASE
        WHEN sb.plan_name = 'Unlimited' AND sb.duration = 'Yearly'
         AND fp.first_paid_date IS NOT NULL
         AND fp.first_paid_date <= cd.check_date
        THEN 1 END)                                                         AS unlimited_yearly,

    -- Trial — non-free plan with NO paid order yet as of check_date
    COUNT(CASE
        WHEN sb.plan_name != 'Free'
         AND (fp.first_paid_date IS NULL OR fp.first_paid_date > cd.check_date)
        THEN 1 END)                                                         AS trial,

    -- Other — plan name not in the 5 standard segments (should always be 0)
    COUNT(CASE
        WHEN sb.plan_name NOT IN ('Free', 'Lite', 'Unlimited')
        THEN 1 END)                                                         AS other,

    -- Grand total (verify: = free + lite_m + lite_y + unl_m + unl_y + trial + other)
    COUNT(*)                                                                AS total_active

FROM check_dates cd
CROSS JOIN sub_base sb
LEFT  JOIN first_paid fp ON fp.subscription_id = sb.subscription_id
WHERE
    -- Subscription must have been created by the check_date
    sb.created_date <= cd.check_date

    -- Active on check_date (Case A or Case B)
    -- Uses strictly > (not >=): a subscription with paid_till = check_date
    -- expired ON that day and is not counted as active.
    AND (
        -- Case A: no hard cutoff, or user cancelled mid-period (paid_till > end_date)
        (
            (sb.end_date IS NULL OR sb.paid_till > sb.end_date)
            AND sb.paid_till > cd.check_date
        )
        OR
        -- Case B: hard access cutoff set (end_date IS NOT NULL AND paid_till <= end_date)
        (
            sb.end_date IS NOT NULL
            AND sb.paid_till <= sb.end_date
            AND sb.end_date > cd.check_date
        )
    )

GROUP BY cd.month_label, cd.check_date
ORDER BY cd.check_date;
