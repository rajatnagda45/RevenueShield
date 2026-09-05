# Database Architecture & Core Data Model

The **Revenue Recovery AI** platform uses PostgreSQL as its primary relational store, managed via SQLAlchemy 2.x and Alembic migrations.

---

## Core Lifecycle Flow

```
[Customer]
    │
    ▼
 [Event] ───────────────► [RecoveryCase] ────────► [Diagnosis]
 (Webhook / Ingestion)    (Central Business Object)     │
                                 │                      ▼
                                 ├────────────────► [RecoveryAction]
                                 │                      │
                                 │                      ▼
                                 ├────────────────► [ActionOutcome]
                                 │
                                 ├────────────────► [PromiseToPay]
                                 ├────────────────► [CommunicationLog]
                                 └────────────────► [AuditLog]
```

### Flow Explanation

1. **Customer**: Represents the business or consumer account holder. Tracks segments, communication preferences, and DND status.
2. **Event**: Ingests raw state changes (e.g. `payment.failed`, `invoice.overdue`) from upstream billing/gateway systems. Stores unconstrained JSONB payloads with `external_event_id` idempotency.
3. **RecoveryCase**: The **central business object** of the entire platform. When an event signifies revenue at risk, a `RecoveryCase` is opened. It encapsulates the financial exposure, current state (`OPEN`, `IN_PROGRESS`, `PTP`, `RECOVERED`, etc.), and orchestrates downstream diagnosis and actions.
4. **Diagnosis**: Explains *why* the revenue is at risk (e.g. `TECHNICAL_FAILURE`, `INSUFFICIENT_FUNDS`, `RECEIVABLE_DELAY`) with confidence metrics and root cause breakdown.
5. **RecoveryAction**: Represents planned or dispatched recovery interventions (e.g. `SEND_WHATSAPP`, `PAYMENT_RETRY`, `SEND_PAYMENT_LINK`) along selected channels.
6. **ActionOutcome**: Captures feedback and telemetry from executed actions (e.g. `MESSAGE_DELIVERED`, `LINK_CLICKED`, `PROMISE_TO_PAY`).
7. **PromiseToPay**: Tracks customer payment commitments, promised dates, and fulfillment states.
8. **CommunicationLog**: Privacy-conscious interaction history across all communication channels.
9. **AuditLog**: Immutable audit trail documenting every system, AI, and user action for strict compliance and explainability.
10. **ModelVersion**: Registry for tracking AI/ML model versions, algorithms, training datasets, and validation metrics.

---

## Entity Relationship Overview

| Entity | Primary Key | Foreign Keys | Key Purpose |
| :--- | :--- | :--- | :--- |
| **`customers`** | UUID | - | Account profiles, segmentation, channel preferences |
| **`payments`** | UUID | `customer_id` | Historical & active payment transactions |
| **`subscriptions`** | UUID | `customer_id` | Recurring subscription billing contracts |
| **`invoices`** | UUID | `customer_id` | Accounts receivable invoices & due dates |
| **`events`** | UUID | `customer_id`, `payment_id`, `subscription_id`, `invoice_id` | Idempotent incoming webhook/cron event stream with JSONB |
| **`recovery_cases`** | UUID | `customer_id`, `event_id`, `payment_id`, `subscription_id`, `invoice_id` | Central recovery orchestration record |
| **`diagnoses`** | UUID | `recovery_case_id` | Failure attribution & confidence scoring |
| **`recovery_actions`** | UUID | `recovery_case_id` | Multi-channel recovery steps (WhatsApp, SMS, Retry, etc.) |
| **`action_outcomes`** | UUID | `action_id`, `recovery_case_id` | Recovery action feedback & conversion telemetry |
| **`promise_to_pays`** | UUID | `recovery_case_id`, `customer_id` | Customer commitment schedules |
| **`communication_logs`** | UUID | `customer_id`, `recovery_case_id` | Channel delivery & interaction audit trail |
| **`audit_logs`** | UUID | `recovery_case_id` | Immutable compliance and AI action tracing |
| **`model_versions`** | UUID | - | ML model registry & evaluation metadata |

---

## Why Event and RecoveryCase are Separate Entities

- **Events are immutable facts**: An `Event` represents an external point-in-time observation (e.g. Stripe webhook, cron trigger). It records *what happened*, including raw payloads, headers, and arrival timestamps.
- **RecoveryCases are stateful business processes**: A `RecoveryCase` represents an ongoing recovery workflow. A single recovery case may span multiple events (e.g., initial failure event, subsequent retry failures, customer webhook replies) and aggregates diagnoses, actions, promises to pay, and audits over days or weeks until resolution.
