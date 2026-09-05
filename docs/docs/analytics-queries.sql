-- =============================================================================
-- REVENUE RECOVERY AI - PRODUCTION ANALYTICS QUERIES (STEP 7)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Query A: Total Revenue at Risk
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(SUM(amount_at_risk), 0.00) AS total_revenue_at_risk,
    COUNT(id) AS total_failed_cases
FROM recovery_cases;


-- -----------------------------------------------------------------------------
-- Query B: Total Revenue Recovered
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(SUM(recovered_amount), 0.00) AS total_revenue_recovered,
    COUNT(id) FILTER (WHERE status = 'RECOVERED') AS total_recovered_cases
FROM recovery_cases;


-- -----------------------------------------------------------------------------
-- Query C: Overall Recovery Rate (%)
-- -----------------------------------------------------------------------------
SELECT 
    COALESCE(SUM(amount_at_risk), 0.00) AS total_at_risk,
    COALESCE(SUM(recovered_amount), 0.00) AS total_recovered,
    ROUND(
        (COALESCE(SUM(recovered_amount), 0.00) / NULLIF(SUM(amount_at_risk), 0.00)) * 100.0, 
        2
    ) AS recovery_rate_percentage
FROM recovery_cases;


-- -----------------------------------------------------------------------------
-- Query D: Recovery Rate by Action Type
-- -----------------------------------------------------------------------------
SELECT 
    l.action_type,
    COUNT(l.id) AS total_interventions,
    COUNT(l.id) FILTER (WHERE l.label = 1) AS attributable_recoveries,
    COALESCE(SUM(l.amount_at_risk), 0.00) AS total_amount_at_risk,
    COALESCE(SUM(l.amount_recovered), 0.00) AS total_amount_recovered,
    ROUND(
        (COALESCE(SUM(l.amount_recovered), 0.00) / NULLIF(SUM(l.amount_at_risk), 0.00)) * 100.0,
        2
    ) AS recovery_rate_percentage
FROM learning_examples l
WHERE l.is_finalized = TRUE
GROUP BY l.action_type
ORDER BY recovery_rate_percentage DESC;


-- -----------------------------------------------------------------------------
-- Query E: Recovery Rate by Diagnosis Category
-- -----------------------------------------------------------------------------
SELECT 
    l.diagnosis_category,
    COUNT(l.id) AS case_count,
    COUNT(l.id) FILTER (WHERE l.label = 1) AS successful_recoveries,
    COALESCE(SUM(l.amount_at_risk), 0.00) AS total_at_risk,
    COALESCE(SUM(l.amount_recovered), 0.00) AS total_recovered,
    ROUND(
        (COALESCE(SUM(l.amount_recovered), 0.00) / NULLIF(SUM(l.amount_at_risk), 0.00)) * 100.0,
        2
    ) AS recovery_rate_percentage
FROM learning_examples l
WHERE l.is_finalized = TRUE
GROUP BY l.diagnosis_category
ORDER BY recovery_rate_percentage DESC;


-- -----------------------------------------------------------------------------
-- Query F: Average Time to Recovery (Seconds & Hours)
-- -----------------------------------------------------------------------------
SELECT 
    action_type,
    attribution,
    COUNT(id) AS attributable_sample_size,
    ROUND(AVG(time_to_recovery_seconds)::numeric, 2) AS avg_time_to_recovery_seconds,
    ROUND((AVG(time_to_recovery_seconds) / 3600.0)::numeric, 2) AS avg_time_to_recovery_hours,
    MIN(time_to_recovery_seconds) AS min_recovery_seconds,
    MAX(time_to_recovery_seconds) AS max_recovery_seconds
FROM recovery_outcomes
WHERE time_to_recovery_seconds IS NOT NULL
GROUP BY action_type, attribution
ORDER BY avg_time_to_recovery_seconds ASC;


-- -----------------------------------------------------------------------------
-- Query G: Recovery Amount by Action Type
-- -----------------------------------------------------------------------------
SELECT 
    a.action_type,
    COUNT(DISTINCT o.id) AS outcome_count,
    COALESCE(SUM(o.amount_recovered), 0.00) AS total_recovered_amount,
    ROUND(AVG(o.amount_recovered)::numeric, 2) AS avg_recovered_per_intervention
FROM recovery_outcomes o
JOIN recovery_actions a ON o.recovery_action_id = a.id
GROUP BY a.action_type
ORDER BY total_recovered_amount DESC;


-- -----------------------------------------------------------------------------
-- Query H: Number of Successful vs Unsuccessful Interventions
-- -----------------------------------------------------------------------------
SELECT 
    action_type,
    COUNT(id) AS total_interventions,
    COUNT(id) FILTER (WHERE label = 1) AS successful_interventions,
    COUNT(id) FILTER (WHERE label = 0) AS unsuccessful_interventions,
    ROUND((COUNT(id) FILTER (WHERE label = 1)::numeric / NULLIF(COUNT(id), 0)) * 100.0, 2) AS success_percentage
FROM learning_examples
WHERE is_finalized = TRUE
GROUP BY action_type
ORDER BY total_interventions DESC;


-- -----------------------------------------------------------------------------
-- Query I: Top-Performing Actions (Ranked by Total Revenue Recovered)
-- -----------------------------------------------------------------------------
SELECT 
    action_type,
    COUNT(id) AS times_selected,
    COUNT(id) FILTER (WHERE label = 1) AS successful_recoveries,
    COALESCE(SUM(amount_recovered), 0.00) AS total_revenue_recovered,
    ROUND(AVG(recovery_percentage)::numeric, 2) AS avg_recovery_percentage
FROM learning_examples
WHERE is_finalized = TRUE
GROUP BY action_type
ORDER BY total_revenue_recovered DESC;


-- -----------------------------------------------------------------------------
-- Query J: Cases Where Intervention Was Blocked by Policy
-- -----------------------------------------------------------------------------
SELECT 
    e.recovery_case_id,
    e.action_type,
    e.error_code AS blocking_rule,
    e.error_message AS policy_reason,
    e.requested_at
FROM recovery_executions e
WHERE e.status = 'BLOCKED'
ORDER BY e.requested_at DESC;


-- -----------------------------------------------------------------------------
-- Query K: Organic vs Attributable Recovery Breakdown
-- -----------------------------------------------------------------------------
SELECT 
    attribution,
    COUNT(id) AS recovery_event_count,
    COALESCE(SUM(amount_recovered), 0.00) AS total_recovered_amount,
    ROUND(
        (COALESCE(SUM(amount_recovered), 0.00) / (SELECT NULLIF(SUM(amount_recovered), 0.00) FROM recovery_outcomes)) * 100.0,
        2
    ) AS percentage_of_total_recovered
FROM recovery_outcomes
GROUP BY attribution
ORDER BY total_recovered_amount DESC;


-- -----------------------------------------------------------------------------
-- Query L: Learning Dataset Size & Training Data Health
-- -----------------------------------------------------------------------------
SELECT 
    COUNT(id) AS total_examples,
    COUNT(id) FILTER (WHERE is_finalized = TRUE) AS finalized_examples,
    COUNT(id) FILTER (WHERE is_finalized = FALSE) AS pending_examples,
    COUNT(id) FILTER (WHERE label = 1) AS positive_labels_count,
    COUNT(id) FILTER (WHERE label = 0) AS negative_labels_count,
    ROUND(
        (COUNT(id) FILTER (WHERE label = 1)::numeric / NULLIF(COUNT(id) FILTER (WHERE is_finalized = TRUE), 0)) * 100.0,
        2
    ) AS positive_class_balance_percentage
FROM learning_examples;
