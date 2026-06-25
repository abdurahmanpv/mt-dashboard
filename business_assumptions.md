# CEO Dashboard — Business Assumptions

Every metric in the dashboard is built on top of these assumptions. If any of these change, the affected metrics must be recalculated.

---

## A. Plans in Scope

The dashboard covers three B2C plans: **Free**, **Lite**, and **Unlimited**. MileageTracker also has B2B plans (e.g., Business Pro) which will be visible in the Raw Data sheet, but all KPI metrics — active subscribers, revenue, churn, conversion — apply only to the three B2C plans. The SQL already filters by `SUB_SubscriptionType = 'MileageTracker'`, but B2B plan names within that type still need to be explicitly excluded. This needs to be confirmed: what are the exact B2B plan names in `tbl_subscription` so an exclusion filter can be added?

**order_status impact:** All order statuses are visible in Raw Data. KPI metrics should only count meaningful order statuses — to be confirmed per assumption J.

---

## B. Plan Structure

Plans have the following billing options:

| Plan | Monthly | Yearly |
|---|---|---|
| Free | ✅ | ❌ |
| Lite | ✅ | ✅ |
| Unlimited | ✅ | ✅ |

**order_status impact:** None — plan structure is independent of order status.

---

## C. Subscription Lifecycle and Trial Rules

The lifecycle for each plan:

- **Free:** `New Subscription → Renewal → Renewal → ...` (no trial ever)
- **Lite / Unlimited:** `Trial → New Subscription → Renewal → Renewal → ...`

**Trial is per user, not per subscription.** A user gets exactly one trial across their lifetime. If a user has already consumed a trial on any plan and starts a new subscription (e.g., after upgrading or re-subscribing), no new trial is generated for the new plan. The system enforces this at the billing level — the question is whether the order fingerprint (amounts) makes this detectable reliably, or whether there are edge cases where a second trial-like order could appear for a returning user.

There is no explicit "trial" label on orders. Trial is derived: an order is a trial if it is the first confirmed/completed order on the subscription AND the price was fully discounted (price > 0, discount = price, total = 0).

**order_status impact:** Trial classification uses only `Confirmed` and `Completed` orders. If the first order on a subscription is `Cancelled`, it is ignored and the next confirmed order becomes the "first order" for classification purposes.

---

## D. subscription_id Behaviour

A `subscription_id` identifies one subscription slot for a user. The following rules govern when subscription_id changes and when it stays the same:

**subscription_id stays the SAME when:**
- A user upgrades their plan (see assumption E for valid upgrade paths)
- Billing renews normally (Renewal orders)

**subscription_id changes when:**
- A user starts a completely new subscription (after churning and re-subscribing)

A user cannot hold two active subscriptions simultaneously — the product enforces a single active subscription per user at any point in time.

This means a single `subscription_id` can have orders across different plan names if an upgrade occurred. For plan-level breakdowns, the plan associated with the most recent order on that `subscription_id` determines the current plan category. For historical snapshots (e.g., what was the plan in October 2025), the plan at that point in time is needed — but since the dashboard joins `tbl_user_subscription → tbl_subscription`, which reflects the current plan link, historical plan may be inaccurate for upgraded subscriptions. This needs to be confirmed with engineering.

**order_status impact:** All order statuses share the same `subscription_id`. A Cancelled order does not create a new subscription_id.

---

## E. Upgrade Paths (No Downgrade)

Users can only upgrade, never downgrade. The valid upgrade paths are:

| From | Can upgrade to |
|---|---|
| Free Monthly | Lite Monthly, Lite Yearly, Unlimited Monthly, Unlimited Yearly |
| Lite Monthly | Lite Yearly, Unlimited Monthly, Unlimited Yearly |
| Lite Yearly | Unlimited Yearly |
| Unlimited Monthly | Lite Yearly, Unlimited Yearly |
| Unlimited Yearly | *(no upgrade — highest tier)* |

When a user upgrades, the `subscription_id` stays the same (see assumption D). The first order after an upgrade will be classified as a `Renewal` because it is not the first confirmed/completed order on the subscription and the user already has prior paid orders.

**order_status impact:** An upgrade order is a Confirmed/Completed paid order. There is no special order_status for upgrades — they look identical to renewal orders.

---

## F. Active Subscription Definition

A subscription is **active on date D** if the subscriber still has valid access as of the start of D+1 (i.e., `00:00:00` of the day after D). Equivalently, `paid_till > D` (strictly greater, not >=).

Examples:
- "Active on Jun 17" = `paid_till >= Jun 17 23:59:59 PST`
- "Active on May 31" = `paid_till >= May 31 23:59:59 PST`

In code, since `paid_till` is stored as a DATE (after PST conversion), this is expressed as `paid_till > D` (strictly greater than the check date). A subscription with `paid_till = D` expired on that day and is not counted as active.

Two cases depending on whether `USB_EndDateTime` is set:

**Case A — No end_date, or user cancelled mid-period:**
`end_date IS NULL` OR `paid_till > end_date` → access is governed by `paid_till`. Active if `paid_till > D`.

This covers: user cancelled but already paid through their billing period (they retain access until `paid_till`).

**Case B — System set a hard access cutoff:**
`end_date IS NOT NULL` AND `paid_till <= end_date` → access is governed by `end_date`. Active if `end_date > D`.

`end_date` (`USB_EndDateTime`) is set by the system when automatic renewal attempts fail and the subscription is terminated. It is not a policy violation flag. The question to confirm: is `end_date` set at the moment the first renewal attempt fails, or after all retry attempts are exhausted?

All dates converted to PST before comparison.

**order_status impact:** Active status is determined at the subscription level (`USB_PaidTill`, `USB_EndDateTime`) — not from order rows. Order status does not affect whether a subscription is active.

---

## G. Trial Duration

Trial duration is not hardcoded. It should be computed from the data: the number of days between the trial order's `subscribed_date` and the first paid order's `subscribed_date` on the same `subscription_id`. For unconverted trials, the trial end can be estimated from `USB_PaidTill` at the time of the trial (before any conversion updates it). The hardcoded 7-day assumption previously used must be removed from the codebase.

**order_status impact:** Trial duration computation should use only `Confirmed` and `Completed` orders.

---

## H. Revenue and Refunds

**Revenue:** `SBO_TotalAmount` from ALL orders is included in gross revenue, regardless of `order_status`. A Cancelled or Refunded order that collected money still counts as gross revenue.

**Refunds:** Revenue is reduced by `SBO_RefundAmount` where it exists. The refund is attributed to the period in which it was processed, using `SBO_RefundDate` converted to PST (not the original order date). This means a May payment refunded in June reduces June revenue, not May.

**Net Revenue:** `SUM(total_amt) − SUM(refund_amount WHERE refund_date in window)`

Additional refund columns to include in the SQL and Raw Data sheet (for the new session):
- `SBO_RefundDate` — date the refund was processed (must be converted to PST)
- `SBO_GatewayRefundRefID` — reference ID from the payment gateway for the refund
- `SBO_OrderRefundReason` — reason for the refund

**order_status impact:** All order statuses contribute to gross revenue via `SBO_TotalAmount`. Refunds are tracked separately via `SBO_RefundAmount` and `SBO_RefundDate` regardless of status.

---

## I. Timezones

All datetime columns in the database are stored in **UTC** and must be converted to **Pacific Time (PST/PDT = America/Los_Angeles)** before any date comparison or bucketing. There are no exceptions — `SBO_SubscribedDate` is also a DATETIME stored in UTC and must be converted. All dates in the dashboard represent Pacific calendar days.

Conversion: `DATE(CONVERT_TZ(column, 'GMT', 'America/Los_Angeles'))`

**order_status impact:** Timezone conversion applies to all date columns across all order statuses.

---

## J. Implementation Notes

The following have been confirmed and implemented:

1. **Active logic:** `paid_till > D` (strictly greater than). A subscription with `paid_till = D` expired on D and is not active. Implemented in `_active_on()` in kpi_engine.py.
2. **`curr_status` selection:** always taken from the row with the maximum `paid_till` per subscription (not the first row in DataFrame order).
3. **Churn date for Case B:** churn events use `end_date` (not `paid_till`) as the event date for Case B subscriptions.
4. **Renewal state denominator:** uses `_active_on()` so Case B subscriptions are included correctly.
5. **Trial duration:** derived from data — converted trial end = first paid order date; unconverted trial end = `paid_till` on the trial row. No hardcoded 7-day assumption.
6. **Revenue:** gross revenue = `SUM(total_amt)` across all order statuses. Refunds subtracted using `refund_date` (PST) not order date.
7. **Win-back metric removed:** `is_returning_user`, `genuine_new`, and all win-back logic removed from SQL, kpi_engine.py, data_loader.py, sheet_builders.py, and sheet_executive.py.
8. **Refund columns added to SQL:** `refund_date` (PST), `gateway_refund_ref_id`, `refund_reason`.
