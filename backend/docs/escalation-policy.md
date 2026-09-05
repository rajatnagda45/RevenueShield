# Intelligent Escalation Policy

## 1. Overview

The **Intelligent Escalation Policy** maps recovery case urgency, amount at risk, failure reasons, and customer interaction telemetry into hierarchical escalation tiers.

$$\text{Case Context} \longrightarrow \text{Escalation Tier} \longrightarrow \text{Policy Filter} \longrightarrow \text{Next Best Action Selection}$$

---

## 2. Escalation Levels

| Level | Name | Trigger Condition | Primary Recovery Action |
| :--- | :--- | :--- | :--- |
| `LEVEL_0` | **Passive / Wait** | Low risk ($< ₹1,000$), organic recovery likely | Background retry / No proactive outreach |
| `LEVEL_1` | **Primary Outreach** | Standard failure, Amount $< ₹10,000$ | `EMAIL_PAYMENT_RECOVERY` |
| `LEVEL_2` | **Follow-up Outreach** | Overdue $> 24\text{h}$, Prior link opened/clicked | `EMAIL_FOLLOWUP` |
| `LEVEL_3` | **High-Value / Promise** | High value ($\ge ₹10,000$), Overdue $> 24\text{h}$ | `Promise-to-Pay` agreement / Human Operator Escalation |

---

## 3. High-Value Promise Eligibility

`PromiseEligibilityEngine` evaluates if an open recovery case qualifies for structured Promise-to-Pay negotiation based on:
1. `amount_at_risk >= PROMISE_MIN_AMOUNT` (₹10,000)
2. `overdue_hours >= PROMISE_MIN_OVERDUE_HOURS` (24h)
3. Historical fulfillment reliability $> 60\%$
