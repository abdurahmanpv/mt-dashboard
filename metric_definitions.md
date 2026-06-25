# CEO Dashboard — Metric Definitions, Logic & Validation

> **Date context for all examples:** Today = Jun 18, Yesterday = Jun 17,
> MTD = Jun 1–17, Prev Full = May 1–31, Prev Same = May 1–17, YTD = Jan 1–17.
> Every date is **Pacific Time (PST/PDT)**.

---

## How to use this document

Each metric section has five parts:

- **What it answers** — the business question in one sentence
- **Who is in / who is out** — exact scope so there's no ambiguity
- **How it is calculated** — step by step, written as SQL you can follow
- **Assumptions** — every assumption made, explicit and implicit
- **Validation SQL** — queries to run against MySQL to spot-check the number

---

## Date variables used in all validation queries

```sql
SET @today          = DATE(CONVERT_TZ(NOW(), 'GMT', 'America/Los_Angeles'));  -- Jun 18
SET @yesterday      = @today - INTERVAL 1 DAY;                               -- Jun 17
SET @mtd_start      = DATE_FORMAT(@today, '%Y-%m-01');                        -- Jun 1
SET @ytd_start      = DATE_FORMAT(@today, '%Y-01-01');                        -- Jan 1
SET @prev_end       = @mtd_start - INTERVAL 1 DAY;                           -- May 31
SET @prev_start     = DATE_FORMAT(@prev_end, '%Y-%m-01');                     -- May 1
SET @days_elapsed   = DAY(@yesterday);                                        -- 17
SET @prev_same_end  = @prev_start + INTERVAL (@days_elapsed - 1) DAY;        -- May 17
```

---

## Quick orientation — what the raw data looks like

The SQL query produces **one row per billing order**. One subscriber on a monthly plan for 3 months = 3 rows. Key columns:

| Column | What it represents |
|---|---|
| `subscription_id` | One plan instance. A user with two plans has two subscription_ids. |
| `user_id` | The person. Unique per human. |
| `subscribed_date` | Date the order was placed (PST). |
| `paid_till` | Last day the subscriber has paid access through. |
| `end_date` | Set when a subscription is cancelled or admin-terminated. NULL if never cancelled. |
| `order_status` | Confirmed, Completed, Cancelled, Refund |
| `trial_check` | PAID (total_amt > 0) / TRIAL (price > 0, discount = price, total = 0) / FREE (price = 0, discount = 0, total = 0) |
| `subscription_type` | Trial / New Subscription / Renewal — see Metric 2 |
| `total_amt` | Money collected on this order (0 for trials and free plans) |
| `refund_amount` | Money returned (non-zero only on Cancelled/Refund rows) |

**Confirmed and Completed orders are used for all subscriber count, type, churn, and conversion KPIs.** Cancelled and Refund rows are excluded from these to avoid inflating counts. Revenue is an exception: gross revenue uses ALL order statuses (`SUM(total_amt)` regardless of status), and refunds are subtracted using `refund_date` (not order date).

---

---

## MECE overview — do the metrics cover everything with no gaps?

This table shows how the full dataset is sliced across each dimension. MECE means every record falls into exactly one bucket (no overlap, no gaps).

| What is being sliced | Buckets | MECE? | Notes |
|---|---|---|---|
| Every confirmed/completed order | Trial + New Subscription + Renewal | ✅ Yes | Every order is exactly one type |
| New Subscriptions | First paid order on a subscription (or first Free order) | ✅ Yes | One per subscription_id |
| Paid subs active at month-start | Churned + Renewed + Waiting | ⚠️ Mostly | Overlap possible if a sub renewed then expired same month |
| Trial→Paid and Free→Paid | Separate metrics | ⚠️ Overlap | A Free→Trial→Paid user appears in both. Intentional. |
| Every subscription_id | Active or Inactive (as of yesterday) | ✅ Yes | Binary, no middle ground |

---

---

## Metric 1 — Active Subscribers

### What it answers
> "How many subscription plans are live right now, as of the end of yesterday?"

### Who is in / who is out

**In:** Any `subscription_id` where access was still valid at the end of Jun 17 — whether the plan is paid, free, or trial.
**Out:** Subscriptions where billing lapsed before Jun 17, and cancelled subscriptions where access has ended.
**Not a window metric:** This is a snapshot count, not a count of events during a period.

### How it is calculated

Step 1 — Collapse to one row per subscription (take the latest billing date):

```sql
SELECT
    subscription_id,
    MAX(paid_till) AS paid_till,
    MAX(end_date)  AS end_date   -- NULL if subscription was never cancelled
FROM raw_orders
GROUP BY subscription_id
```

Step 2 — Check if the subscription was active on Jun 17:

```sql
-- Case A: No hard cutoff, OR user cancelled mid-period (paid_till > end_date)
--         (e.g. they paid through May 31 then cancelled on May 20 — still active until May 31)
--         → active if paid_till > Jun 17  (strictly greater — expired subscriptions not counted)
WHEN (end_date IS NULL OR paid_till > end_date) AND paid_till > @yesterday

-- Case B: System set a hard access cutoff (end_date IS NOT NULL AND paid_till <= end_date)
--         → active if end_date > Jun 17  (strictly greater)
WHEN end_date IS NOT NULL AND paid_till <= end_date AND end_date > @yesterday
```

Step 3 — Count the subscriptions that pass either case.

Two counts are produced:
- `active_now` = active on Jun 17 (the headline number)
- `active_at_month_start` = active on May 31 (used as the denominator for churn rate)

### Assumptions

1. **[Explicit]** Counts `subscription_id`, not `user_id`. A user with a Free plan and a Paid plan has 2 active subscriptions.
2. **[Explicit]** "Active" is evaluated at end of yesterday — not real-time.
3. **[Explicit]** `paid_till` comes from `USB_PaidTill` converted to PST. `end_date` from `USB_EndDateTime` converted to PST.
4. **[Implicit]** If `paid_till` is stored as `2026-06-17 00:00:00` (midnight), the DATE() conversion gives Jun 17, so it is counted as active on Jun 17. This is the intended behaviour.
5. **[Implicit]** Free plan subscriptions have a `paid_till` that renews even though no money is collected. They are included in the active count.
6. **[Implicit]** `curr_status` from the SQL is only used as a fallback if `paid_till` is unavailable — which should not happen in production.

### Validation SQL

```sql
-- Should match the "active_now" number on the dashboard
SELECT COUNT(DISTINCT tus.USB_SubscriptionID) AS active_subscribers
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
WHERE
    -- Case A
    (
        (tus.USB_EndDateTime IS NULL
         OR DATE(CONVERT_TZ(tus.USB_PaidTill,    'GMT', 'America/Los_Angeles'))
          > DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles')))
        AND DATE(CONVERT_TZ(tus.USB_PaidTill, 'GMT', 'America/Los_Angeles')) >= @yesterday
    )
    OR
    -- Case B
    (
        tus.USB_EndDateTime IS NOT NULL
        AND DATE(CONVERT_TZ(tus.USB_PaidTill,    'GMT', 'America/Los_Angeles'))
         <= DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles'))
        AND DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles')) >= @yesterday
    );

-- Drill-down: see which subscriptions are active and why
SELECT
    tus.USB_SubscriptionID,
    DATE(CONVERT_TZ(tus.USB_PaidTill,    'GMT', 'America/Los_Angeles')) AS paid_till_pst,
    DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles')) AS end_date_pst,
    CASE
        WHEN (tus.USB_EndDateTime IS NULL
              OR DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
               > DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles')))
            THEN 'Case A (paid_till governs)'
        ELSE 'Case B (end_date governs)'
    END AS active_case
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
WHERE -- (same WHERE clause as above)
    (
        (tus.USB_EndDateTime IS NULL
         OR DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
          > DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles')))
        AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles')) >= @yesterday
    )
    OR
    (
        tus.USB_EndDateTime IS NOT NULL
        AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
         <= DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles'))
        AND DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles')) >= @yesterday
    )
ORDER BY paid_till_pst;
```

---

---

## Metric 2 — Order Type (Trial / New Subscription / Renewal)

### What it answers
> "For any given billing order, is this a first-time trial, a first real payment, or a repeat renewal?"

### Who is in / who is out

Every confirmed/completed order gets exactly one label. No order goes unlabelled, no order gets two labels.

| Label | Who gets it |
|---|---|
| **Trial** | First order on the subscription, price was fully discounted, zero dollars collected |
| **New Subscription** | First real payment (whether direct or after a trial); also first order on a Free plan |
| **Renewal** | Every other order — repeat payments, free plan renewals, complimentary months given by support |

### How it is calculated

```sql
CASE

    -- BLOCK 1: Is this the first confirmed/completed order for this subscription?
    WHEN tso.SBO_SubscribedDate = (
        SELECT MIN(x.SBO_SubscribedDate)
        FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders x
        WHERE x.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
          AND x.SBO_OrderStatus IN ('Confirmed', 'Completed')
    )
    THEN
        CASE
            -- Trial fingerprint: list price shown, fully discounted, nothing collected
            WHEN SBO_Price > 0 AND SBO_Discount = SBO_Price AND SBO_TotalAmount = 0
                THEN 'Trial'
            -- First order and money was collected (paid direct, skipped trial)
            -- OR first order on Free plan (all zeros)
            ELSE 'New Subscription'
        END

    -- BLOCK 2: Not the first order, but this is the FIRST time real money was collected.
    -- Fires for trial-to-paid conversions (order 1 = trial, this order = first payment).
    WHEN SBO_TotalAmount > 0
     AND NOT EXISTS (
         SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders prev
         WHERE prev.SBO_USB_SubscriptionID = this subscription_id
           AND prev.SBO_OrderStatus IN ('Confirmed', 'Completed')
           AND prev.SBO_TotalAmount > 0
           AND prev.SBO_SubscribedDate < this order's SBO_SubscribedDate
     )
    THEN 'New Subscription'

    -- BLOCK 3: Everything else
    ELSE 'Renewal'

END AS subscription_type
```

### Assumptions

1. **[Explicit]** Only Confirmed and Completed orders are checked when finding the "first order." A cancelled first order is ignored.
2. **[Explicit]** Trial fingerprint = price listed > 0, full discount applied, zero dollars collected.
3. **[Explicit]** A "comped" renewal (support gives a subscriber a free month) has the same fingerprint as a trial but is a second-or-later order → correctly goes to Renewal.
4. **[Implicit]** If two orders on the same subscription share the exact same `SBO_SubscribedDate` and that date equals the earliest date, both are treated as first orders. This is a rare edge case but can cause double-counting of trials. A row_number tiebreaker by order ID would fix this.
5. **[Implicit]** Block 2 uses a strict `<` on the date. If a trial and the first payment happen on the exact same calendar date, the first-payment order falls to Renewal. Unlikely in practice since trials last 7 days.
6. **[Implicit]** A returning user who re-subscribes after churning gets a new `subscription_id`. Their new trial correctly shows as Trial on the new subscription.

### Validation SQL

```sql
-- 1. Count by type for MTD — compare to dashboard
SELECT subscription_type, COUNT(DISTINCT subscription_id) AS count
FROM (
    -- (Paste the full mileage_tracker_subscriptions.sql query here as a subquery)
    -- or use the raw_data table if you have it loaded
    SELECT * FROM raw_data
) sub
WHERE subscribed_date BETWEEN @mtd_start AND @yesterday
GROUP BY subscription_type;

-- 2. Find subscriptions that still have Trial followed directly by Renewal (no New Sub)
-- These are the bug cases that the Jun 18 fix addressed. Should return 0 rows.
SELECT
    tso.SBO_USB_SubscriptionID                        AS subscription_id,
    MIN(CASE WHEN tso.SBO_TotalAmount = 0
              AND tso.SBO_Price > 0
              AND tso.SBO_Discount = tso.SBO_Price
             THEN tso.SBO_SubscribedDate END)         AS trial_date,
    MIN(CASE WHEN tso.SBO_TotalAmount > 0
             THEN tso.SBO_SubscribedDate END)         AS first_paid_date
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE tso.SBO_OrderStatus IN ('Confirmed', 'Completed')
GROUP BY tso.SBO_USB_SubscriptionID
HAVING
    trial_date IS NOT NULL        -- has a trial
    AND first_paid_date IS NOT NULL  -- and a paid order
    -- and the paid order is NOT the second order on the subscription
    -- (if this returns rows, the first-paid-after-trial is being miscategorised)
ORDER BY trial_date DESC;
```

---

---

## Metric 3 — Paid Churn

### What it answers
> "Of all the paying subscribers who were active going into this period, how many stopped paying?"

### Who is in / who is out

**In (denominator):** Subscriptions that have at least one real payment (`total_amt > 0`) ever AND were active just before the window started.
**In (numerator — churned):** From that group, subscriptions whose billing lapsed inside the window AND whose status is now Inactive.
**Out:** Free plan subscriptions. Trial-only subscriptions (trialled but never paid). Subscriptions that renewed before their billing lapsed.

### How it is calculated

Step 1 — Identify all paid subscriptions:
```sql
paid_sub_ids = subscription_ids where ANY order has total_amt > 0
```

Step 2 — Collapse to one row per paid subscription:
```sql
SELECT
    subscription_id,
    MAX(paid_till)    AS paid_till,
    MAX(end_date)     AS end_date,
    -- ⚠️ Gap: should use curr_status from the row with MAX(paid_till)
    FIRST(curr_status) AS curr_status
FROM raw_orders
WHERE subscription_id IN (paid_sub_ids)
GROUP BY subscription_id
```

Step 3 — Denominator: who was active the day before the window opened?
```sql
-- For MTD (window = Jun 1–17): check who was active on May 31
-- Uses the same Case A / Case B logic as Metric 1
COUNT(*) WHERE active_on(paid_till, end_date, date = @prev_end)
```

Step 4 — Numerator: who churned inside the window?
```sql
COUNT(*) WHERE
    paid_till BETWEEN @mtd_start AND @yesterday   -- billing lapsed in this window
    AND curr_status = 'In-Active'                 -- did not renew
```

Step 5:
```
Churn Rate     = Churned ÷ Denominator
Retention Rate = 1 − Churn Rate
```

**Windows and their denominator cutoffs:**

| Window | Events counted | Denominator date |
|---|---|---|
| MTD | Jun 1 → Jun 17 | May 31 |
| Prev Full | May 1 → May 31 | Apr 30 |
| Prev Same | May 1 → May 17 | Apr 30 |
| YTD | Jan 1 → Jun 17 | Dec 31 |

### Assumptions

1. **[Explicit]** "Paid subscription" = subscription_id that has at least one order with `total_amt > 0`, even if that subscription has since lapsed.
2. **[Explicit]** Churn = billing lapsed in window (paid_till in window) AND status is Inactive. Not renewing = churning.
3. **[Explicit]** Denominator is "active just before the window," not "active during the window." This prevents subs that started and churned in the same month from inflating the denominator.
4. **[Implicit — Gap G1]** `curr_status` is aggregated using `FIRST()` across all orders of a subscription. For a subscription with many old (expired) orders, the first row encountered almost always has `curr_status = In-Active` because old orders have long-expired `paid_till`. This can make active subscriptions look churned. Fix: use `curr_status` from the row with `MAX(paid_till)`.
5. **[Implicit — Gap G2]** The churn event window filter uses `paid_till` for all subscriptions. For a cancelled subscription where `end_date < paid_till` (Case B in active logic), the actual service end date is `end_date`, not `paid_till`. A subscriber who cancelled on Jun 5 but had `paid_till = May 25` would appear as a May churn, not a June churn.

### Validation SQL

```sql
-- DENOMINATOR: paid subscriptions active on May 31 (for MTD churn rate)
SELECT COUNT(DISTINCT tus.USB_SubscriptionID) AS churn_denominator
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
WHERE
    EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders o
        WHERE o.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
          AND o.SBO_OrderStatus IN ('Confirmed', 'Completed')
          AND o.SBO_TotalAmount > 0
    )
    AND (
        -- Case A
        (
            (tus.USB_EndDateTime IS NULL
             OR DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
              > DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles')))
            AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles')) >= @prev_end
        )
        OR
        -- Case B
        (
            tus.USB_EndDateTime IS NOT NULL
            AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
             <= DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles'))
            AND DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles')) >= @prev_end
        )
    );

-- NUMERATOR: who churned in MTD? (grain-level — inspect each row)
SELECT
    tus.USB_SubscriptionID,
    DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))    AS paid_till_pst,
    DATE(CONVERT_TZ(tus.USB_EndDateTime,'GMT','America/Los_Angeles'))  AS end_date_pst,
    tus.USB_Status
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
WHERE
    EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders o
        WHERE o.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
          AND o.SBO_OrderStatus IN ('Confirmed', 'Completed')
          AND o.SBO_TotalAmount > 0
    )
    AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
        BETWEEN @mtd_start AND @yesterday
    AND tus.USB_Status != 'Active'
ORDER BY paid_till_pst;
```

---

---

## Metric 4 — Renewal State (Churned / Renewed / Waiting)

### What it answers
> "Of the paid subscribers active at the start of this month, what happened to each of them so far — did they leave, renew, or are they still within their billing period?"

### Who is in / who is out

**In:** Every subscription_id that has at least one paid order AND was active on May 31. Same universe as the Paid Churn MTD denominator.
**Out:** Free plan subscriptions. Subscribers who started in June (they weren't active at month start).
**MTD only** — no historical windows for this metric.

### How it is calculated

```sql
-- Universe: paid subs active at May 31 (same as churn denominator)
active_at_start = paid_sub_ids WHERE active_on(paid_till, end_date, date = May 31)

-- Churned: billing lapsed in June AND currently inactive
churned = active_at_start WHERE
    paid_till BETWEEN @mtd_start AND @yesterday
    AND curr_status = 'In-Active'

-- Renewed: placed a paid Renewal order in June
renewed = active_at_start WHERE
    a Renewal order with total_amt > 0 exists
    AND that order's subscribed_date BETWEEN @mtd_start AND @yesterday

-- Waiting: everyone else (still active, renewal date not yet reached)
waiting = active_at_start - churned - renewed
```

These three always add up to `active_at_start` by construction.

### Assumptions

1. **[Explicit]** MTD-only metric, no historical comparison.
2. **[Explicit]** "Renewed" means a subscription_type = Renewal AND trial_check = PAID order was placed in June by a sub that was active at month start.
3. **[Implicit — Gap G3]** The denominator here uses `MAX(paid_till) >= May 31` directly, not the full Case A / Case B active logic. A subscription with `paid_till = Jun 15` but `end_date = May 20` would pass this check (paid_till >= May 31) even though access ended May 20. Should be consistent with the churn denominator.
4. **[Implicit — Gap G4]** A subscription could appear in both Churned and Renewed if a renewal order went through but the subscription still expired before Jun 17 (very rare, but possible). The code caps Waiting at zero to avoid negative numbers.
5. **[Implicit]** Same `FIRST(curr_status)` gap as Metric 3 (Gap G1).

### Validation SQL

```sql
-- Active at May 31
WITH active_at_start AS (
    SELECT tus.USB_SubscriptionID
    FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
    WHERE
        EXISTS (
            SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders o
            WHERE o.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
              AND o.SBO_OrderStatus IN ('Confirmed','Completed') AND o.SBO_TotalAmount > 0
        )
        AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles')) >= @prev_end
)

SELECT
    COUNT(DISTINCT a.USB_SubscriptionID) AS active_at_start,

    -- Churned
    SUM(CASE
        WHEN DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles'))
                 BETWEEN @mtd_start AND @yesterday
             AND tus.USB_Status != 'Active'
        THEN 1 ELSE 0
    END) AS churned,

    -- Renewed
    COUNT(DISTINCT ren.SBO_USB_SubscriptionID) AS renewed

FROM active_at_start a
JOIN WAY_SUBSCRIPTIONS.tbl_user_subscription tus
  ON tus.USB_SubscriptionID = a.USB_SubscriptionID
LEFT JOIN WAY_SUBSCRIPTIONS.tbl_subscription_orders ren
       ON ren.SBO_USB_SubscriptionID = a.USB_SubscriptionID
      AND ren.SBO_OrderStatus IN ('Confirmed','Completed')
      AND ren.SBO_TotalAmount > 0
      AND DATE(ren.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday;

-- Check: active_at_start = churned + renewed + waiting
-- If churned + renewed > active_at_start, you have an overlap (Gap G4)
```

---

---

## Metric 5 — Trial Pipeline

### What it answers
> "How many people are currently inside their free trial window and haven't converted yet?"

### Who is in / who is out

**In:** Subscription IDs where the trial started between Jun 12 and Jun 17 (within the last 6 complete days) AND no payment has been made yet.
**Out:** Trials that started before Jun 12 — their 7-day window has fully elapsed (they either converted or they didn't). Trials that already converted (have any paid order).

### How it is calculated

```sql
-- Step 1: trials that started recently (window still open)
recent_trials = subscription_ids WHERE
    subscription_type = 'Trial'
    AND subscribed_date > (TODAY - 7 days)   -- = Jun 11, so we get Jun 12 and later
    AND subscribed_date <= @yesterday         -- exclude today's partial data

-- Step 2: remove those that already converted
open_pipeline = recent_trials
    MINUS subscription_ids where ANY order has trial_check = 'PAID'
```

Returns: count of unconverted open trials and the date range they started (Jun 12–17).

### Assumptions

1. **[Explicit]** Trial duration is exactly 7 days for every plan type.
2. **[Explicit]** A trial is "converted" if ANY paid order exists on that subscription_id — regardless of date.
3. **[Implicit]** This is a point-in-time snapshot. The number changes every day as new trials start and old ones expire or convert.
4. **[Implicit]** Trials that started on Jun 11 or earlier have a full 7-day window elapsed (Jun 11 + 7 = Jun 18 = today). They are counted in the Trial→Paid Conversion metric instead (Metric 6), not here.

### Validation SQL

```sql
SELECT COUNT(DISTINCT tso.SBO_USB_SubscriptionID) AS open_trial_count
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE
    tso.SBO_OrderStatus IN ('Confirmed', 'Completed')
    AND tso.SBO_Price > 0
    AND tso.SBO_Discount = tso.SBO_Price
    AND tso.SBO_TotalAmount = 0
    -- Started in the last 6 complete days
    AND DATE(tso.SBO_SubscribedDate)
        BETWEEN (@today - INTERVAL 6 DAY) AND @yesterday
    -- Has not yet paid
    AND NOT EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders paid
        WHERE paid.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND paid.SBO_OrderStatus IN ('Confirmed', 'Completed')
          AND paid.SBO_TotalAmount > 0
    );

-- See who they are
SELECT
    tso.SBO_USB_SubscriptionID  AS subscription_id,
    tu.USR_EmailID              AS email,
    DATE(tso.SBO_SubscribedDate) AS trial_start,
    DATE(tso.SBO_SubscribedDate) + INTERVAL 7 DAY AS trial_expires
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
JOIN WAY_SUBSCRIPTIONS.tbl_user_subscription tus
  ON tus.USB_SubscriptionID = tso.SBO_USB_SubscriptionID
JOIN PROD_WAY_DB.tbl_user tu ON tu.USR_UserID = tus.USB_USR_UserID
WHERE
    tso.SBO_OrderStatus IN ('Confirmed','Completed')
    AND tso.SBO_Price > 0 AND tso.SBO_Discount = tso.SBO_Price AND tso.SBO_TotalAmount = 0
    AND DATE(tso.SBO_SubscribedDate) BETWEEN (@today - INTERVAL 6 DAY) AND @yesterday
    AND NOT EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders paid
        WHERE paid.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND paid.SBO_OrderStatus IN ('Confirmed','Completed') AND paid.SBO_TotalAmount > 0
    )
ORDER BY trial_start;
```

---

---

## Metric 6 — Trial → Paid Conversion Rate

### What it answers
> "Of the trials whose 7-day window closed during this period, what percentage of them paid?"

### Who is in / who is out

**Denominator:** Subscriptions whose trial ended (= trial start + 7 days) within the window.
**Numerator:** From that group, those that also placed a paid order within the same window.
**Out:** Trials still open (Metric 5). Paid-direct subscribers who never had a trial. Free plans.

**Important:** Both the trial expiry AND the payment must land in the same window. If someone's trial expired May 31 but they paid Jun 3, they count in neither month.

### How it is calculated

```sql
-- Step 1: Trial end date for each trial subscription
-- Use subscribed_date + 7 days (NOT paid_till)
-- Reason: paid_till gets updated to the new billing expiry after conversion.
-- Using paid_till would push converted trials into a future month's denominator.
trial_end = subscribed_date + 7 days

-- Step 2: Denominator — trials that ended in this window
denom = subscription_ids WHERE trial_end BETWEEN @mtd_start AND @yesterday

-- Step 3: Numerator — from denom, those that also paid in this window
numer = denom WHERE
    a PAID order exists AND that order's subscribed_date BETWEEN @mtd_start AND @yesterday

Rate = numer ÷ denom
```

### Assumptions

1. **[Explicit]** Trial duration = 7 days for all plans.
2. **[Explicit]** Trial end date = `subscribed_date + 7 days`, not `paid_till`. This is intentional to avoid pushing converted trials into wrong months.
3. **[Explicit]** Both trial expiry and payment must fall in the same window (event-based, not cohort-based).
4. **[Implicit]** Consequence of assumption 3: if someone's trial ended May 31 but they paid Jun 5, they appear in neither month's numerator. They simply go untracked. In practice this is a small number.
5. **[Implicit]** Early in a new month (e.g. Jun 1–3), very few trials have expired yet, so the denominator is small and the rate can swing wildly. This is expected behaviour.
6. **[Implicit]** Measured per `subscription_id`. A user who ran two separate trials is counted twice in the denominator.

### Validation SQL

```sql
-- DENOMINATOR: trials whose window ended in MTD
SELECT COUNT(DISTINCT tso.SBO_USB_SubscriptionID) AS trial_denom
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE
    tso.SBO_OrderStatus IN ('Confirmed','Completed')
    AND tso.SBO_Price > 0 AND tso.SBO_Discount = tso.SBO_Price AND tso.SBO_TotalAmount = 0
    -- This must be the first order on the subscription
    AND tso.SBO_SubscribedDate = (
        SELECT MIN(x.SBO_SubscribedDate)
        FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders x
        WHERE x.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND x.SBO_OrderStatus IN ('Confirmed','Completed')
    )
    -- Trial ended in MTD (start + 7 days lands in Jun 1–17)
    AND DATE(tso.SBO_SubscribedDate) + INTERVAL 7 DAY BETWEEN @mtd_start AND @yesterday;

-- NUMERATOR: from above, those that also paid in MTD
SELECT COUNT(DISTINCT tso.SBO_USB_SubscriptionID) AS trial_converted
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE
    tso.SBO_OrderStatus IN ('Confirmed','Completed')
    AND tso.SBO_Price > 0 AND tso.SBO_Discount = tso.SBO_Price AND tso.SBO_TotalAmount = 0
    AND tso.SBO_SubscribedDate = (
        SELECT MIN(x.SBO_SubscribedDate) FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders x
        WHERE x.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND x.SBO_OrderStatus IN ('Confirmed','Completed')
    )
    AND DATE(tso.SBO_SubscribedDate) + INTERVAL 7 DAY BETWEEN @mtd_start AND @yesterday
    AND EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders paid
        WHERE paid.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND paid.SBO_OrderStatus IN ('Confirmed','Completed')
          AND paid.SBO_TotalAmount > 0
          AND DATE(paid.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday
    );
```

---

---

## Metric 7 — Free → Paid Conversion Rate

### What it answers
> "Of the users on a permanent Free plan who were active going into this period, how many of them upgraded to a paid plan?"

### Who is in / who is out

**Denominator:** User IDs on a Free plan whose free subscription was still active at the start of the window (free `paid_till >= window start date`).
**Numerator:** From that group, users who placed any paid order (`total_amt > 0`) during the window.
**Out:** Users who never had a Free plan. Free users whose plan had already lapsed.
**Measured at user level** (not subscription level) — a person upgrading is one decision regardless of how many subscriptions they have.

**Note:** A user who went Free → Trial → Paid appears in BOTH this metric AND Trial→Paid. This is intentional because they are two separate funnel paths.

### How it is calculated

```sql
-- Step 1: Free users active at window start
free_denom = user_ids WHERE
    ANY order has trial_check = 'FREE'
    AND MAX(paid_till) on their free subscriptions >= @mtd_start  -- still valid Jun 1

-- Step 2: Numerator — those who paid in the window
numer = free_denom WHERE
    a PAID order (total_amt > 0) exists AND subscribed_date BETWEEN @mtd_start AND @yesterday

Rate = numer ÷ denom
```

For Lifetime: no active-at-start filter — denominator = all users who ever had a free plan.

### Assumptions

1. **[Explicit]** Measured at `user_id` level. A user counts at most once in the numerator per window even if they took out multiple paid subscriptions.
2. **[Explicit]** Lifetime denominator = all free users ever (no active filter).
3. **[Implicit — Gap G5]** A user who converted Free→Paid months ago but still has an old Free subscription with `paid_till >= Jun 1` stays in the denominator. They're not actually a "potential upgrader" anymore. This inflates the denominator and deflates the conversion rate.
4. **[Implicit]** The paid order counted in the numerator can be on any subscription_id — it just has to be placed by the same user_id in the window. It doesn't have to be a direct upgrade of the Free plan.

### Validation SQL

```sql
-- DENOMINATOR: free users whose free plan was active on Jun 1
SELECT COUNT(DISTINCT tus.USB_USR_UserID) AS free_denom
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus
JOIN WAY_SUBSCRIPTIONS.tbl_subscription ts
  ON tus.USB_SUB_SubscriptionID = ts.SUB_SubscriptionID
WHERE ts.SUB_SubscriptionName = 'Free'
  AND DATE(CONVERT_TZ(tus.USB_PaidTill,'GMT','America/Los_Angeles')) >= @mtd_start;

-- NUMERATOR: from above, who placed a paid order in MTD?
SELECT COUNT(DISTINCT tus_free.USB_USR_UserID) AS free_converted
FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus_free
JOIN WAY_SUBSCRIPTIONS.tbl_subscription ts_free
  ON tus_free.USB_SUB_SubscriptionID = ts_free.SUB_SubscriptionID
WHERE
    ts_free.SUB_SubscriptionName = 'Free'
    AND DATE(CONVERT_TZ(tus_free.USB_PaidTill,'GMT','America/Los_Angeles')) >= @mtd_start
    AND EXISTS (
        SELECT 1
        FROM WAY_SUBSCRIPTIONS.tbl_user_subscription tus_paid
        JOIN WAY_SUBSCRIPTIONS.tbl_subscription_orders tso_paid
          ON tso_paid.SBO_USB_SubscriptionID = tus_paid.USB_SubscriptionID
        WHERE tus_paid.USB_USR_UserID = tus_free.USB_USR_UserID
          AND tso_paid.SBO_OrderStatus IN ('Confirmed','Completed')
          AND tso_paid.SBO_TotalAmount > 0
          AND DATE(tso_paid.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday
    );
```

---

---

---

## Metric 9 — Renewals Count

### What it answers
> "How many paid subscriptions renewed (billing cycled successfully) during this period?"

### Who is in / who is out

**In:** Orders where `subscription_type = 'Renewal'` AND `trial_check = 'PAID'` (real money collected) AND `subscribed_date` is in the window.
**Out:** Free plan renewals (zero dollars). First payments. Trials.

### How it is calculated

```sql
SELECT COUNT(DISTINCT subscription_id)
FROM raw_orders
WHERE subscription_type = 'Renewal'
  AND trial_check = 'PAID'
  AND subscribed_date BETWEEN @mtd_start AND @yesterday
```

### Assumptions

1. **[Explicit]** Counted as distinct `subscription_id`s. If the same subscription cycled twice in a month (possible for short billing periods), it counts as 1 renewal. If you want to count total transactions, remove the DISTINCT and count rows.
2. **[Implicit]** Free plan renewals ($0 orders after the first Free order) have `subscription_type = 'Renewal'` but `trial_check = 'FREE'`. They are excluded here because of the `trial_check = 'PAID'` filter. Confirm this is intentional.

### Validation SQL

```sql
-- Paid renewals in MTD
SELECT COUNT(DISTINCT tso.SBO_USB_SubscriptionID) AS renewals_count
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE
    tso.SBO_OrderStatus IN ('Confirmed','Completed')
    AND tso.SBO_TotalAmount > 0
    -- Not the first order
    AND tso.SBO_SubscribedDate > (
        SELECT MIN(x.SBO_SubscribedDate)
        FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders x
        WHERE x.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND x.SBO_OrderStatus IN ('Confirmed','Completed')
    )
    -- Not the first paid order (exclude trial-to-paid conversion)
    AND EXISTS (
        SELECT 1 FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders prev_paid
        WHERE prev_paid.SBO_USB_SubscriptionID = tso.SBO_USB_SubscriptionID
          AND prev_paid.SBO_OrderStatus IN ('Confirmed','Completed')
          AND prev_paid.SBO_TotalAmount > 0
          AND prev_paid.SBO_SubscribedDate < tso.SBO_SubscribedDate
    )
    AND DATE(tso.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday;
```

---

---

## Metric 10 — Revenue (Cash, MRR, Refunds, Net Cash)

### What it answers
> "How much money came in, how does that translate to a monthly rate, and what was refunded?"

Three views of the same underlying payments:

| View | What it is |
|---|---|
| **Cash** | Actual dollars collected in the window |
| **MRR** | Cash normalised to a monthly rate (yearly plans ÷ 12) |
| **Refunds** | Money returned in the window |
| **Net Cash** | Cash − Refunds |

### Who is in / who is out

**Cash & MRR:** Only Confirmed/Completed orders where `total_amt > 0`.
**Refunds:** Any order (including Cancelled/Refund) where `refund_amount > 0`, windowed by order date.
**Out:** Trial orders ($0). Free plan orders ($0). Pending orders.

### How it is calculated

```sql
-- Cash
SELECT ROUND(SUM(total_amt), 2)
FROM raw_orders
WHERE order_status IN ('Confirmed','Completed')
  AND trial_check = 'PAID'
  AND subscribed_date BETWEEN @mtd_start AND @yesterday

-- MRR (monthly normalised)
SELECT ROUND(SUM(mrr_amount), 2)
FROM raw_orders
WHERE order_status IN ('Confirmed','Completed')
  AND trial_check = 'PAID'
  AND subscribed_date BETWEEN @mtd_start AND @yesterday
-- mrr_amount = total_amt          (monthly plans)
--            = total_amt / 12     (yearly plans)

-- Refunds
SELECT ROUND(SUM(refund_amount), 2)
FROM raw_orders  -- includes Cancelled and Refund rows
WHERE subscribed_date BETWEEN @mtd_start AND @yesterday
  AND refund_amount > 0

-- Net Cash = Cash - Refunds
```

### Assumptions

1. **[Explicit]** Cash and MRR use only Confirmed/Completed orders. Refunds use the full dataset (all order statuses).
2. **[Explicit]** MRR divides yearly plan revenue by 12. A $120/year plan = $10/month MRR.
3. **[Implicit — Gap G6]** Refunds are bucketed by the refund transaction date (`subscribed_date` of the refund order). If someone paid $9.99 in May and is refunded in June, May shows +$9.99 cash and June shows −$9.99 refunds. Within a single month, Cash and Refunds can appear to mismatch. Net Cash across all time (Lifetime) is always correct.
4. **[Implicit]** MRR is an approximation. True MRR would spread each payment across the months it covers (e.g. a June yearly payment contributes $10 to Jun through May next year). The current approach assigns the full normalised amount to the month the order was placed.

### Validation SQL

```sql
-- Cash and MRR for MTD
SELECT
    ROUND(SUM(tso.SBO_TotalAmount), 2) AS cash_mtd,
    ROUND(SUM(
        CASE
            WHEN ts.SUB_Duration = 'Yearly'  THEN tso.SBO_TotalAmount / 12
            WHEN ts.SUB_Duration = 'Monthly' THEN tso.SBO_TotalAmount
            ELSE 0
        END
    ), 2) AS mrr_mtd
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
JOIN WAY_SUBSCRIPTIONS.tbl_user_subscription tus
  ON tso.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
JOIN WAY_SUBSCRIPTIONS.tbl_subscription ts
  ON tus.USB_SUB_SubscriptionID = ts.SUB_SubscriptionID
WHERE
    tso.SBO_OrderStatus IN ('Confirmed','Completed')
    AND tso.SBO_TotalAmount > 0
    AND DATE(tso.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday;

-- Refunds for MTD
SELECT ROUND(SUM(COALESCE(tso.SBO_RefundAmount, 0)), 2) AS refunds_mtd
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
WHERE tso.SBO_RefundAmount > 0
  AND DATE(tso.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday;
```

---

---

## Metric 11 — Cancellations

### What it answers
> "How many orders were explicitly cancelled or refunded during this period, and how much money was returned?"

### Who is in / who is out

**In:** Any order row where `order_status IN ('Cancelled', 'Refund')` with `subscribed_date` in the window.
**Out:** Confirmed/Completed orders (even if the subscriber later stopped renewing — that's churn, not a cancellation).

**Key distinction:** Churn (Metric 3) = subscriber stopped renewing. Cancellation (this metric) = an order was explicitly voided/refunded. These are different events. A subscriber can churn without generating a Cancellation row.

### How it is calculated

```sql
SELECT
    COUNT(*)               AS cancellation_rows,
    SUM(refund_amount)     AS total_refunded
FROM raw_orders
WHERE order_status IN ('Cancelled', 'Refund')
  AND subscribed_date BETWEEN @mtd_start AND @yesterday
```

### Assumptions

1. **[Explicit]** Counts order rows, not unique subscribers. One subscriber refunded = 1 row here.
2. **[Implicit]** If your system creates both a "Cancelled" order row AND a separate "Refund" row for the same event, one cancellation would count as 2 rows. Confirm with engineering whether this happens.

### Validation SQL

```sql
-- Cancellations in MTD — inspect each one
SELECT
    tso.SBO_OrderIdentifier                AS order_id,
    tso.SBO_USB_SubscriptionID             AS subscription_id,
    tu.USR_EmailID                         AS email,
    tso.SBO_OrderStatus                    AS status,
    DATE(tso.SBO_SubscribedDate)           AS cancelled_date,
    tso.SBO_TotalAmount                    AS original_amount,
    COALESCE(tso.SBO_RefundAmount, 0)      AS refunded
FROM WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
JOIN WAY_SUBSCRIPTIONS.tbl_user_subscription tus
  ON tus.USB_SubscriptionID = tso.SBO_USB_SubscriptionID
JOIN PROD_WAY_DB.tbl_user tu ON tu.USR_UserID = tus.USB_USR_UserID
WHERE
    tso.SBO_OrderStatus IN ('Cancelled', 'Refund')
    AND DATE(tso.SBO_SubscribedDate) BETWEEN @mtd_start AND @yesterday
ORDER BY tso.SBO_SubscribedDate DESC;
```

---

---

## Resolved Gaps

All previously identified gaps have been fixed in kpi_engine.py:

| # | Metric | Fix applied |
|---|---|---|
| **G1** | Paid Churn, Renewal State | `curr_status` now taken from the row with max `paid_till` (not first row). Sort by `paid_till` ascending before groupby, use `last()`. |
| **G2** | Paid Churn | Churn event date now uses `end_date` for Case B subscriptions (not `paid_till`). |
| **G3** | Renewal State | Denominator now uses `_active_on()` — handles Case B and strict `>` comparison. |
| **G4** | Trial duration | Derived from data: converted = first paid order date; unconverted = `paid_till`. No hardcoded 7-day assumption. |
| **G5** | Active logic | All active checks use strict `>` (not `>=`). A subscription with `paid_till = D` is not active on D. |
| **G6** | Revenue | Gross revenue uses all order statuses. Refunds bucketed by `refund_date` (PST), not order date. |
