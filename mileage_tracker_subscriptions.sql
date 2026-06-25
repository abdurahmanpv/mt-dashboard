-- ============================================================================
-- QUERY      : MileageTracker Subscription Data Source
-- Dashboard  : CEO Subscription & Revenue Dashboard
-- Database   : WAY_SUBSCRIPTIONS + PROD_WAY_DB
-- Timezone   : All dates converted from GMT → America/Los_Angeles (PST/PDT)
-- Scope      : All MileageTracker subscription orders (all statuses)
-- Refresh    : Full reload daily via daily_refresh.py
--
-- ACTIVE SUBSCRIPTION DEFINITION:
--   A subscription is active on date D if paid_till > D (strictly greater than).
--   Case A (end_date IS NULL OR paid_till > end_date): active if paid_till > D.
--   Case B (end_date IS NOT NULL AND paid_till <= end_date): active if end_date > D.
--   A subscription with paid_till = D expired ON that day and is NOT active.
--
-- SUBSCRIPTION TYPE LOGIC:
--   Order fingerprints (derived from order financials — no stored label):
--     Trial order : SBO_Price > 0  AND SBO_Discount = SBO_Price AND total_amt = 0
--                   Must be the first order on the subscription_id
--     Free order  : SBO_Price = 0  AND SBO_Discount = 0          AND total_amt = 0
--                   Free plan has no trial; can have renewals
--     Paid order  : total_amt > 0
--     Comped order: SBO_Price > 0  AND SBO_Discount = SBO_Price AND total_amt = 0
--                   Always a renewal (2nd+ order) — never the first order
--
--   subscription_type values:
--     'Trial'            : first order on subscription_id AND trial fingerprint
--     'New Subscription' : first paid order (total_amt > 0) on subscription_id,
--                          OR first order on subscription_id for Free plan
--     'Renewal'          : all subsequent orders (2nd+ on the same subscription_id)
--
-- REVENUE:
--   Gross revenue = SUM(total_amt) across ALL order statuses.
--   Refunds are subtracted using refund_amount bucketed by refund_date (PST),
--   not the original order date.
--
-- SCOPE:
--   B2C plans only: Free, Lite, Unlimited.
--   All datetime columns stored in UTC; converted to America/Los_Angeles before use.
--
-- SUBSCRIPTION TYPE LOGIC (v2):
--   Order fingerprints confirmed with business:
--     Trial order : SBO_Price > 0  AND SBO_Discount = SBO_Price AND total_amt = 0
--                   Always the first order on a subscription_id (ranked by SBO_SubscribedDate)
--     Free order  : SBO_Price = 0  AND SBO_Discount = 0          AND total_amt = 0
--                   Free plan has no trial; can have renewals
--     Paid order  : total_amt > 0
--     Comped order: SBO_Price > 0  AND SBO_Discount = SBO_Price AND total_amt = 0
--                   Always a renewal (2nd+ order) — never the first order
--
--   subscription_type values:
--     'Trial'            : first order on subscription_id AND trial fingerprint
--     'New Subscription' : first paid order (total_amt > 0) on subscription_id
--                          OR first order on subscription_id for Free plan
--     'Renewal'          : all subsequent orders (paid, free renewal, or comped)
-- ============================================================================

SELECT

    -- -------------------------------------------------------------------------
    -- ORDER IDENTIFIERS
    -- Unique per subscription order. Use this as the grain for the dashboard.
    -- -------------------------------------------------------------------------
    tso.SBO_OrderIdentifier                                     AS order_identifier,
    tso.SBO_OrderStatus                                         AS order_status,

    -- -------------------------------------------------------------------------
    -- USER IDENTIFIERS
    -- USR_UserID is the unique user key across all WAY products.
    -- DISTINCT on user_id gives total unique MileageTracker users.
    -- -------------------------------------------------------------------------
    tu.USR_UserID                                               AS user_id,
    tuc.UCD_FullName                                            AS full_name,
    tu.USR_EmailID                                              AS email_id,

    -- Most recent phone number for the user (contact type 1 = phone)
    uc.UCO_ContactValue                                         AS contact_number,

    -- -------------------------------------------------------------------------
    -- GEOGRAPHIC INFO
    -- ~40% of users have no address on file — nulls are expected here.
    -- Kept in datasource for reference; not surfaced in main dashboard.
    -- -------------------------------------------------------------------------
    ta.ADD_City                                                 AS city,
    ta.ADD_State                                                AS state,
    ta.ADD_ZipCode                                              AS zip_code,

    -- -------------------------------------------------------------------------
    -- PLAN DETAILS
    -- SUB_SubscriptionName : Free | Lite | Unlimited | Business Pro Trial
    -- SUB_Duration         : Monthly | Yearly
    -- -------------------------------------------------------------------------
    ts.SUB_SubscriptionName                                     AS subscription_name,
    ts.SUB_Duration                                             AS duration,

    -- -------------------------------------------------------------------------
    -- REVENUE FIELDS
    -- subscription_amt : list price before any discount (gross)
    -- discount         : amount deducted (full price for trials = $0 collected)
    -- total_amt        : actual cash collected = subscription_amt - discount
    --                    This is $0 for all trial orders.
    -- -------------------------------------------------------------------------
    tso.SBO_Price                                               AS subscription_amt,
    tso.SBO_Discount                                            AS discount,
    tso.SBO_TotalAmount                                         AS total_amt,

    -- -------------------------------------------------------------------------
    -- REFUND AMOUNT
    -- Populated for Cancelled / Refunded orders.
    -- For revenue reporting: net_cash = SUM(total_amt) - SUM(refund_amount)
    -- where refund_amount is only non-zero on cancelled/refunded rows.
    -- -------------------------------------------------------------------------
    COALESCE(tso.SBO_RefundAmount, 0)                           AS refund_amount,

    -- -------------------------------------------------------------------------
    -- REFUND DETAIL COLUMNS
    -- refund_date  : date the refund was processed (PST). Used to bucket refunds
    --               by period — a May payment refunded in June reduces June revenue.
    -- gateway_refund_ref_id : Stripe refund reference ID. NULL when no refund.
    -- refund_reason : reason code/description recorded at time of refund.
    -- -------------------------------------------------------------------------
    DATE(CONVERT_TZ(tso.SBO_RefundDate, 'GMT', 'America/Los_Angeles'))
                                                                AS refund_date,
    tso.SBO_GatewayRefundRefID                                  AS gateway_refund_ref_id,
    tso.SBO_OrderRefundReason                                   AS refund_reason,

    -- -------------------------------------------------------------------------
    -- MRR AMOUNT
    -- Normalizes yearly plans to a monthly equivalent for MRR reporting.
    -- Monthly plans: total_amt as-is
    -- Yearly plans : total_amt / 12
    -- Free / Trial : 0
    -- -------------------------------------------------------------------------
    CASE
        WHEN tso.SBO_TotalAmount > 0 AND ts.SUB_Duration = 'Yearly'
            THEN ROUND(tso.SBO_TotalAmount / 12, 2)
        WHEN tso.SBO_TotalAmount > 0 AND ts.SUB_Duration = 'Monthly'
            THEN tso.SBO_TotalAmount
        ELSE 0
    END                                                         AS mrr_amount,

    -- -------------------------------------------------------------------------
    -- TRIAL CHECK
    -- Classifies each order into one of three user types:
    --   FREE  : user is on the permanent Free plan (no payment expected)
    --   PAID  : actual payment collected (total_amt > 0)
    --   TRIAL : mandatory 7-day trial period (total_amt = 0, plan ≠ Free)
    --           All paid subscriptions (Lite/Unlimited) must go through trial first.
    --           Trial→Paid conversion is determined by checking if a subsequent
    --           PAID order exists for the same user_id (see dashboard logic).
    -- -------------------------------------------------------------------------
    CASE
        WHEN ts.SUB_SubscriptionName = 'Free'   THEN 'FREE'
        WHEN tso.SBO_TotalAmount > 0            THEN 'PAID'
        ELSE                                         'TRIAL'
    END                                                         AS trial_check,

    -- -------------------------------------------------------------------------
    -- PLAN SEGMENT (5-way split for Plan Performance sheet)
    --   Lite Monthly | Lite Yearly | Unlimited Monthly | Unlimited Yearly | Free Monthly
    -- -------------------------------------------------------------------------
    CASE
        WHEN ts.SUB_SubscriptionName = 'Free'
            THEN 'Free Monthly'
        ELSE CONCAT(ts.SUB_SubscriptionName, ' ', ts.SUB_Duration)
    END                                                         AS plan_segment,

    -- -------------------------------------------------------------------------
    -- PAYMENT REFERENCE
    -- Stripe payment intent / charge ID. NULL for free and trial orders.
    -- -------------------------------------------------------------------------
    tso.SBO_GatewayChargeRefID                                  AS stripe_pmt_id,

    -- -------------------------------------------------------------------------
    -- SUBSCRIPTION DATES (converted to PST/PDT)
    -- -------------------------------------------------------------------------
    DATE(CONVERT_TZ(tso.SBO_SubscribedDate, 'GMT', 'America/Los_Angeles'))
                                                                AS subscribed_date,
    DATE_FORMAT(CONVERT_TZ(tso.SBO_SubscribedDate, 'GMT', 'America/Los_Angeles'), '%H:%i:%s')
                                                                AS subscribed_time,

    -- paid_till: date the subscription's paid access expires (PST/PDT).
    -- Used as the churn date and one of two possible active-window anchors.
    -- NULL for subscriptions where USB_PaidTill is not set.
    DATE(CONVERT_TZ(tus.USB_PaidTill, 'GMT', 'America/Los_Angeles'))
                                                                AS paid_till,

    -- end_date: cancellation or final-expiry date (PST/PDT).
    -- Set at the moment of cancellation, OR after the final failed renewal attempt.
    -- NULL for subscriptions that are still ongoing (no termination event yet).
    DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles'))
                                                                AS end_date,

    -- -------------------------------------------------------------------------
    -- ACTIVE STATUS (curr_status)
    --
    -- A subscription is Active on a given date if it still had access at that
    -- date, determined by which date anchor to use:
    --
    --   Case A — end_date IS NULL  OR  paid_till > end_date
    --     (ongoing subscription, OR cancelled mid-period with access until paid_till)
    --     → active if paid_till >= given_date
    --
    --   Case B — end_date IS NOT NULL  AND  paid_till <= end_date
    --     (natural expiry or post-expiry end_date set after final renewal attempt;
    --      user retains access through the renewal-attempt grace period)
    --     → active if end_date >= given_date
    --
    -- "given_date" here is end-of-yesterday (Pacific).
    -- USB_Status = 'Active' check removed — end_date already captures termination.
    --
    -- DST NOTE: DATE(CONVERT_TZ(NOW(), 'GMT', 'America/Los_Angeles')) gives the
    --   current Pacific calendar day regardless of server timezone.
    -- -------------------------------------------------------------------------
    CASE
        WHEN (
            -- Case A: no end_date or cancelled mid-period → use paid_till
            (
                tus.USB_EndDateTime IS NULL
                OR DATE(CONVERT_TZ(tus.USB_PaidTill,    'GMT', 'America/Los_Angeles'))
                 > DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles'))
            )
            AND DATE(CONVERT_TZ(tus.USB_PaidTill, 'GMT', 'America/Los_Angeles'))
                 > DATE(CONVERT_TZ(NOW(), 'GMT', 'America/Los_Angeles')) - INTERVAL 1 DAY
        )
        OR (
            -- Case B: natural expiry / final cancellation → use end_date
            tus.USB_EndDateTime IS NOT NULL
            AND DATE(CONVERT_TZ(tus.USB_PaidTill,    'GMT', 'America/Los_Angeles'))
             <= DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles'))
            AND DATE(CONVERT_TZ(tus.USB_EndDateTime, 'GMT', 'America/Los_Angeles'))
                 > DATE(CONVERT_TZ(NOW(), 'GMT', 'America/Los_Angeles')) - INTERVAL 1 DAY
        )
        THEN 'Active'
        ELSE 'In-Active'
    END                                                         AS curr_status,

    -- -------------------------------------------------------------------------
    -- SUBSCRIPTION & PAYMENT IDs
    -- -------------------------------------------------------------------------
    tus.USB_SubscriptionID                                      AS subscription_id,
    tupo.UPO_GatewayAccountID                                   AS stripe_cus_id,

    -- -------------------------------------------------------------------------
    -- SUBSCRIPTION TYPE  (3 values: Trial | New Subscription | Renewal)
    --
    -- Step 1 — Determine if this order is the first on its subscription_id
    --          using a correlated subquery ranked by SBO_SubscribedDate.
    --          SBO_CreatedDateTime is NOT used here due to known system lag
    --          that can cause it to differ slightly from USB_CreatedDateTime.
    --
    -- Step 2 — Apply fingerprint logic per order type:
    --
    --   TRIAL
    --     · SBO_Price > 0                     (real plan price exists)
    --     · SBO_Discount = SBO_Price           (fully discounted = $0 collected)
    --     · SBO_TotalAmount = 0               (no cash collected)
    --     · IS the first order on subscription_id (ranked by SBO_SubscribedDate)
    --     · Note: comped orders have identical financials but are NEVER first
    --             orders — so first-order check cleanly separates them
    --
    --   NEW SUBSCRIPTION
    --     · IS the first order on subscription_id AND (
    --         total_amt > 0                   (user skipped trial, paid directly)
    --         OR SBO_Price = 0               (Free plan — no trial, no discount)
    --       )
    --
    --   RENEWAL
    --     · All subsequent orders (2nd order onwards) on a subscription_id
    --     · Covers: paid renewals, free plan renewals, comped renewals
    --
    -- NOTE: subscription_type is subscription-level, not user-level.
    -- -------------------------------------------------------------------------
    CASE
        -- Is this the first order on this subscription_id?
        WHEN tso.SBO_SubscribedDate = (
                SELECT MIN(first_ord.SBO_SubscribedDate)
                FROM   WAY_SUBSCRIPTIONS.tbl_subscription_orders first_ord
                WHERE  first_ord.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
                  AND  first_ord.SBO_OrderStatus IN ('Confirmed', 'Completed')
             )
        THEN
            CASE
                -- First order + trial fingerprint (price > 0, fully discounted)
                WHEN tso.SBO_Price > 0
                     AND tso.SBO_Discount = tso.SBO_Price
                     AND tso.SBO_TotalAmount = 0
                    THEN 'Trial'

                -- First order + paid directly (skipped trial)
                -- OR first order on Free plan (Price=0, Discount=0, total=0)
                WHEN tso.SBO_TotalAmount > 0
                     OR (tso.SBO_Price = 0 AND tso.SBO_Discount = 0 AND tso.SBO_TotalAmount = 0)
                    THEN 'New Subscription'

                -- Safety fallback: first order with unrecognised fingerprint
                -- (log and investigate if this appears in production)
                ELSE 'New Subscription'
            END

        -- Not the first order, but this IS the first paid order (trial → paid conversion).
        -- Condition: total_amt > 0 AND no prior paid order exists on this subscription.
        -- This fires when a user completes their trial and makes their first payment.
        WHEN tso.SBO_TotalAmount > 0
             AND NOT EXISTS (
                 SELECT 1
                 FROM   WAY_SUBSCRIPTIONS.tbl_subscription_orders prev_paid
                 WHERE  prev_paid.SBO_USB_SubscriptionID = tus.USB_SubscriptionID
                   AND  prev_paid.SBO_OrderStatus        IN ('Confirmed', 'Completed')
                   AND  prev_paid.SBO_TotalAmount         > 0
                   AND  prev_paid.SBO_SubscribedDate      < tso.SBO_SubscribedDate
             )
        THEN 'New Subscription'

        -- All other non-first orders → Renewal
        -- Covers: paid renewals, free renewals, comped orders (Price=Discount, total=0)
        ELSE 'Renewal'
    END                                                         AS subscription_type,

    -- -------------------------------------------------------------------------
    -- DEVICE TYPE (granular)
    -- IOS                   : iPhone or iPad, native app
    -- ANDROID               : Android phone, native app
    -- IOS_BROWSER_MOBILE    : iPhone or iPad, mobile browser
    -- Android_BROWSER_MOBILE: Android phone, mobile browser
    -- BROWSER_DESKTOP       : all other (Windows, Mac, etc.)
    -- -------------------------------------------------------------------------
    CASE
        WHEN (tso.SBO_Platform IN ('iPhone', 'iPhone App') OR tso.SBO_Platform LIKE '%iPad%')
             AND tso.SBO_Browser = 'App'
            THEN 'IOS'
        WHEN tso.SBO_Platform IN ('Android', 'Android App')
             AND tso.SBO_Browser = 'App'
            THEN 'ANDROID'
        WHEN (tso.SBO_Platform IN ('Android', 'Android App') OR tso.SBO_Platform LIKE '%iPad%')
             AND tso.SBO_Browser <> 'App'
            THEN 'Android_BROWSER_MOBILE'
        WHEN (tso.SBO_Platform IN ('iPhone', 'iPhone App') OR tso.SBO_Platform LIKE '%iPad%')
             AND tso.SBO_Browser <> 'App'
            THEN 'IOS_BROWSER_MOBILE'
        ELSE 'BROWSER_DESKTOP'
    END                                                         AS device_type,

    -- -------------------------------------------------------------------------
    -- PLATFORM (high-level channel)
    -- Mobile App     : iOS or Android native app
    -- Mobile Browser : iOS or Android web browser
    -- Web            : desktop browser
    -- -------------------------------------------------------------------------
    CASE
        WHEN (tso.SBO_Platform IN ('iPhone', 'iPhone App', 'Android', 'Android App')
              OR tso.SBO_Platform LIKE '%iPad%')
             AND tso.SBO_Browser = 'App'
            THEN 'Mobile App'
        WHEN (tso.SBO_Platform IN ('iPhone', 'iPhone App', 'Android', 'Android App')
              OR tso.SBO_Platform LIKE '%iPad%')
             AND tso.SBO_Browser <> 'App'
            THEN 'Mobile Browser'
        ELSE 'Web'
    END                                                         AS platform

-- ============================================================================
-- JOINS
-- ============================================================================
FROM
    WAY_SUBSCRIPTIONS.tbl_user_subscription         tus

    -- Subscription plan details (name, duration, type)
    LEFT JOIN WAY_SUBSCRIPTIONS.tbl_subscription    ts
           ON tus.USB_SUB_SubscriptionID = ts.SUB_SubscriptionID

    -- Individual order / billing event per subscription
    LEFT JOIN WAY_SUBSCRIPTIONS.tbl_subscription_orders tso
           ON tso.SBO_USB_SubscriptionID = tus.USB_SubscriptionID

    -- Core user record
    LEFT JOIN PROD_WAY_DB.tbl_user                  tu
           ON tus.USB_USR_UserID = tu.USR_UserID

    -- User display name
    -- Inline subquery picks ONE credential row per user (MIN full name) to prevent
    -- fan-out when a user has multiple login methods (e.g., Google + email).
    LEFT JOIN (
        SELECT  UCD_USR_UserId,
                MIN(UCD_FullName) AS UCD_FullName
        FROM    PROD_WAY_DB.tbl_user_credentials
        GROUP BY UCD_USR_UserId
    ) tuc
           ON tuc.UCD_USR_UserId = tu.USR_UserID

    -- Address affiliation (type 3 = user address)
    -- ~40% of users have no address; nulls are expected.
    -- Inline subquery picks ONE address per user (MIN address ID) to prevent
    -- fan-out when a user has multiple addresses on file.
    LEFT JOIN (
        SELECT  AAF_EntityID,
                MIN(AAF_ADD_AddressID) AS AAF_ADD_AddressID
        FROM    PROD_WAY_DB.tbl_address_affiliations
        WHERE   AAF_ADE_AddressEntityTypeID = 3
        GROUP BY AAF_EntityID
    ) taa
           ON taa.AAF_EntityID = tu.USR_UserID

    LEFT JOIN PROD_WAY_DB.tbl_address               ta
           ON taa.AAF_ADD_AddressID = ta.ADD_AddressID

    -- Stripe customer ID
    -- Inline subquery picks ONE row per user (MIN gateway account ID) to prevent
    -- fan-out when a user has multiple payment option rows in the table.
    LEFT JOIN (
        SELECT  UPO_USR_UserID,
                MIN(UPO_GatewayAccountID) AS UPO_GatewayAccountID
        FROM    PROD_WAY_DB.tbl_user_payment_option
        GROUP BY UPO_USR_UserID
    ) tupo
           ON tupo.UPO_USR_UserID = tu.USR_UserID

    -- Most recent phone number (contact type 1 = phone)
    -- Correlated subquery picks the latest entry to avoid fan-out
    LEFT JOIN PROD_WAY_DB.tbl_user_contact          uc
           ON uc.UCO_USR_UserID    = tu.USR_UserID
          AND uc.UCO_CTY_ContactTypeID = 1
          AND uc.UCO_CreatedDateTime = (
                SELECT MAX(UCO_CreatedDateTime)
                FROM   PROD_WAY_DB.tbl_user_contact
                WHERE  UCO_USR_UserID      = uc.UCO_USR_UserID
                  AND  UCO_CTY_ContactTypeID = 1
              )

-- ============================================================================
-- FILTERS
-- ============================================================================
WHERE
    -- Scope to MileageTracker product only
    ts.SUB_SubscriptionType = 'MileageTracker'

    -- All order statuses included (Confirmed, Completed, Cancelled, Refund, etc.)
    -- order_status reflects the CURRENT status of each order as stored in
    -- tbl_subscription_orders.SBO_OrderStatus — if an order was Confirmed and
    -- later Cancelled, it will appear here with order_status = 'Cancelled'.
    -- Filter by order_status in downstream analysis as needed.

ORDER BY
    tso.SBO_SubscribedDate  DESC,
    tus.USB_SubscriptionID,
    tso.SBO_OrderIdentifier
