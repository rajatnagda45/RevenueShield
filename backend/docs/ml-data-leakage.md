# Anti-Data-Leakage Rules & Point-in-Time Boundary

## 1. Core Principle

A predictive machine learning model in the revenue recovery domain must **never consume information that was not available at the exact decision timestamp**. Including future information—even indirectly—creates artificial inflation of model accuracy during offline training and severe degradation during live deployment.

---

## 2. Forbidden Post-Decision Features

The following attributes are strictly **forbidden** from being included in feature extraction pipelines:

| Forbidden Attribute | Description | Why It Leaks |
| :--- | :--- | :--- |
| `amount_recovered` / `recovered_amount` | The actual amount collected from customer | Only known post-payment capture |
| `payment_captured_at` / `captured_at` | Timestamp of successful payment settlement | Post-decision event |
| `recovery_percentage` | Percentage of invoice recovered | Direct target leakage |
| `time_to_recovery_seconds` | Elapsed duration until resolution | Only known after resolution |
| `final_payment_status` | Status `CAPTURED` / `RECOVERED` / `SETTLED` | Direct label leakage |
| `attribution_type` | Whether recovery was organic or direct | Generated post-outcome |
| `email_opened` / `link_clicked` (Future) | Telemetry collected *after* current action | Leaks post-outreach signals |
| `chargeback_status` / `refund_processed` | Post-payment dispute signals | Future settlement outcome |

---

## 3. Allowed Point-in-Time Features

All training features must be constructed strictly using state up to decision timestamp $T_{\text{decision}}$:

1. **Transaction & Failure Context**:
   - `amount_at_risk`, `log_amount`, `currency`
   - `failure_category`, `error_code`, `error_source`, `error_reason`
   - `payment_method` (CARD, UPI, NETBANKING)
   - `bank` (HDFC, ICICI, SBI)

2. **Customer Historical Baseline (Strictly Prior Transactions)**:
   - `customer_previous_payment_count`
   - `customer_previous_failed_count`
   - `customer_previous_recovered_count`
   - `customer_historical_recovery_rate`
   - `customer_average_payment_amount`
   - `days_since_customer_first_payment`

3. **Current Case & Sequencer Progression**:
   - `case_age_at_decision_hours`
   - `number_of_previous_recovery_attempts`
   - `previous_email_attempts`
   - `has_prior_engagement` (link clicks/opens on *previous* steps only)

4. **Temporal Context**:
   - `hour_of_day`, `day_of_week`, `is_business_hour`

5. **Action Feature**:
   - `action_type` (The candidate intervention being evaluated: `EMAIL_PAYMENT_RECOVERY`, `EMAIL_FOLLOWUP`, etc.)

---

## 4. Enforcement Mechanism

The system enforces anti-leakage programmatically via [`validate_point_in_time_features()`](file:///c:/Users/Ankit%20Kumar/OneDrive/Desktop/RevenueShield/revenue-recovery/backend/app/ml/features.py). If any feature dictionary or DataFrame column matches a forbidden key, an immediate `ValueError` is raised and training/inference is halted.
