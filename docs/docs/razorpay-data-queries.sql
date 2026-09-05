-- =============================================================================
-- REVENUE RECOVERY AI - RAZORPAY INGESTED DATA ANALYTICS QUERIES (STEP 8A)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Query 1: Total Payments Ingested
-- -----------------------------------------------------------------------------
SELECT 
    COUNT(id) AS total_payments_count,
    COUNT(DISTINCT external_payment_id) AS distinct_razorpay_payments
FROM payments;


-- -----------------------------------------------------------------------------
-- Query 2: Total Payment Monetary Value
-- -----------------------------------------------------------------------------
SELECT 
    currency,
    COALESCE(SUM(amount), 0.00) AS total_amount,
    COALESCE(AVG(amount), 0.00) AS avg_payment_amount,
    MIN(amount) AS min_amount,
    MAX(amount) AS max_amount
FROM payments
GROUP BY currency;


-- -----------------------------------------------------------------------------
-- Query 3: Successful (Captured) Payments
-- -----------------------------------------------------------------------------
SELECT 
    COUNT(id) AS successful_payments_count,
    COALESCE(SUM(amount), 0.00) AS successful_revenue_amount
FROM payments
WHERE status = 'CAPTURED' OR captured = TRUE;


-- -----------------------------------------------------------------------------
-- Query 4: Failed Payments
-- -----------------------------------------------------------------------------
SELECT 
    COUNT(id) AS failed_payments_count,
    COALESCE(SUM(amount), 0.00) AS total_failed_amount
FROM payments
WHERE status = 'FAILED';


-- -----------------------------------------------------------------------------
-- Query 5: Payment Failure Rate (%)
-- -----------------------------------------------------------------------------
SELECT 
    COUNT(id) AS total_transactions,
    COUNT(id) FILTER (WHERE status = 'CAPTURED' OR captured = TRUE) AS successful_transactions,
    COUNT(id) FILTER (WHERE status = 'FAILED') AS failed_transactions,
    ROUND(
        (COUNT(id) FILTER (WHERE status = 'FAILED')::numeric / NULLIF(COUNT(id), 0)) * 100.0,
        2
    ) AS failure_rate_percentage
FROM payments;


-- -----------------------------------------------------------------------------
-- Query 6: Payments Breakdown by Payment Method
-- -----------------------------------------------------------------------------
SELECT 
    payment_method,
    COUNT(id) AS transaction_count,
    COALESCE(SUM(amount), 0.00) AS total_volume,
    COUNT(id) FILTER (WHERE status = 'CAPTURED' OR captured = TRUE) AS captured_count,
    COUNT(id) FILTER (WHERE status = 'FAILED') AS failed_count,
    ROUND(
        (COUNT(id) FILTER (WHERE status = 'FAILED')::numeric / NULLIF(COUNT(id), 0)) * 100.0,
        2
    ) AS method_failure_rate_percentage
FROM payments
GROUP BY payment_method
ORDER BY total_volume DESC;


-- -----------------------------------------------------------------------------
-- Query 7: Payments Breakdown by Bank / Issuing Institution
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(bank, 'N/A') AS bank_name,
    COUNT(id) AS transaction_count,
    COALESCE(SUM(amount), 0.00) AS total_amount,
    COUNT(id) FILTER (WHERE status = 'FAILED') AS failed_count,
    ROUND(
        (COUNT(id) FILTER (WHERE status = 'FAILED')::numeric / NULLIF(COUNT(id), 0)) * 100.0,
        2
    ) AS bank_failure_rate_percentage
FROM payments
GROUP BY bank
ORDER BY transaction_count DESC;


-- -----------------------------------------------------------------------------
-- Query 8: Failures Breakdown by error_code
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(failure_code, 'UNSPECIFIED') AS error_code,
    COUNT(id) AS failure_count,
    COALESCE(SUM(amount), 0.00) AS total_amount_at_risk
FROM payments
WHERE status = 'FAILED'
GROUP BY failure_code
ORDER BY failure_count DESC;


-- -----------------------------------------------------------------------------
-- Query 9: Failures Breakdown by error_source
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(error_source, 'UNKNOWN') AS error_source,
    COUNT(id) AS failure_count,
    COALESCE(SUM(amount), 0.00) AS total_amount_at_risk
FROM payments
WHERE status = 'FAILED'
GROUP BY error_source
ORDER BY failure_count DESC;


-- -----------------------------------------------------------------------------
-- Query 10: Failures Breakdown by error_step
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(error_step, 'UNKNOWN') AS error_step,
    COUNT(id) AS failure_count,
    COALESCE(SUM(amount), 0.00) AS total_amount_at_risk
FROM payments
WHERE status = 'FAILED'
GROUP BY error_step
ORDER BY failure_count DESC;


-- -----------------------------------------------------------------------------
-- Query 11: Failures Breakdown by error_reason
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(error_reason, 'UNKNOWN') AS error_reason,
    COUNT(id) AS failure_count,
    COALESCE(SUM(amount), 0.00) AS total_amount_at_risk
FROM payments
WHERE status = 'FAILED'
GROUP BY error_reason
ORDER BY failure_count DESC;


-- -----------------------------------------------------------------------------
-- Query 12: Payments Ingested Over Time (Daily Aggregates)
-- -----------------------------------------------------------------------------
SELECT 
    DATE_TRUNC('day', COALESCE(razorpay_created_at, created_at)) AS payment_date,
    COUNT(id) AS total_payments,
    COUNT(id) FILTER (WHERE status = 'CAPTURED' OR captured = TRUE) AS captured_payments,
    COUNT(id) FILTER (WHERE status = 'FAILED') AS failed_payments,
    COALESCE(SUM(amount), 0.00) AS total_volume
FROM payments
GROUP BY DATE_TRUNC('day', COALESCE(razorpay_created_at, created_at))
ORDER BY payment_date DESC;


-- -----------------------------------------------------------------------------
-- Query 13: Recovery Cases Created from Failed Payments
-- -----------------------------------------------------------------------------
SELECT 
    c.id AS recovery_case_id,
    p.external_payment_id AS razorpay_payment_id,
    c.status AS recovery_case_status,
    c.amount_at_risk,
    c.recovered_amount,
    p.payment_method,
    p.error_reason,
    c.created_at AS case_opened_at,
    c.closed_at AS case_closed_at
FROM recovery_cases c
JOIN payments p ON c.payment_id = p.id
ORDER BY c.created_at DESC;
