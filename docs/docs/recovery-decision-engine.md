# Recovery Decision / Next Best Action Engine Architecture

This document describes the architectural design, candidate action scoring, explanation models, alternative action tracking, and future ML integration path for the **Recovery Decision Engine** (`decision_engine_v1`) implemented in **Step 5**.

---

## 1. High-Level Architecture

```
[RecoveryCase (OPEN)] + [Diagnosis (v1)] + [Customer Intelligence Features]
                                │
                                ▼
              [Recovery Decision Engine (v1)]
                (decision_engine_v1)
      ├── Evaluates Controlled Action Vocabulary
      ├── Multi-Factor Scoring for all Candidates
      ├── Selects Top Action + Ranks Alternatives
      └── Generates Deterministic Supporting Factors & Reasons
                                │
                                ▼
                [Policy Engine (policy_engine_v1)]
      ├── Evaluates 7 Hard Safety & Regulatory Rules
      └── Returns Structured Policy Evaluation Result
                                │
                                ▼
            [Persist RecoveryAction (APPROVED / BLOCKED)]
                                │
                                ▼
            [Immutable AuditLog] ("RECOVERY_ACTION_RECOMMENDED")
```

---

## 2. Distinction: Diagnosis vs Decision vs Execution

- **Diagnosis (Step 4)**: *What happened and why?* Identifies the root cause category (e.g. `BANK_TECHNICAL_FAILURE`), calculates confidence, and measures revenue risk.
- **Decision (Step 5)**: *What is the next best action to take?* Given the diagnosis, customer history, and channels, which candidate action is optimal?
- **Policy Validation (Step 5)**: *Is this action currently allowed by compliance and safety rules?*
- **Execution (Future Steps)**: *Dispatches the action to external systems (e.g. WhatsApp, Voice, Gateway).* Zero external actions are executed in Step 5.

---

## 3. Controlled Action Vocabulary

| Action Type | Primary Channel | Description |
| :--- | :--- | :--- |
| `RETRY_PAYMENT` | `GATEWAY` | Automated payment transaction retry on the payment gateway. |
| `SEND_PAYMENT_LINK` | `EMAIL` / `SMS` | Hosted payment link prompt delivered to customer. |
| `SEND_WHATSAPP_REMINDER` | `WHATSAPP` | Interactive chat reminder on WhatsApp channel. |
| `VOICE_OUTREACH` | `VOICE` | Personalized phone call outreach for high-value / urgent accounts. |
| `WAIT` | `SYSTEM` | Hold intervention for observation (e.g. low diagnostic confidence). |
| `ESCALATE` | `MANUAL` | Route case to human specialist or B2B account manager. |
| `NO_ACTION` | `SYSTEM` | Suppress outreach (e.g. unrecoverable or permanent fraud). |

---

## 4. Multi-Factor Decision Scoring

For every recovery case, the decision engine computes a normalized decision score ($0.0 \dots 1.0$) across **all** candidate actions:
- **Diagnosis Compatibility**: Matching failure taxonomy (e.g. OTP failures have high affinity for payment links; technical downtime has high affinity for gateway retries).
- **Recovery Probability**: Weighs likely success before choosing heavy interventions.
- **Amount & Risk Profile**: High ticket values ($\ge ₹50,000$) prioritize voice outreach; low values prioritize self-service links.
- **Customer Relationship**: Success rate $> 80\%$ boosts automated retry and reminder scoring.
- **Channel Availability**: Verifies customer has valid email or phone on file.

---

## 5. Alternatives Tracking for Future Self-Learning

Every recommendation stores the top 3 ranked candidate alternatives with their scores and justifications in the `alternatives` JSONB column. 

When outcomes (payment collected, customer response, failure) are eventually logged in later steps, the dataset $(Context, RecommendedAction, AlternativeActions, Outcome)$ will serve as the labeled training set for offline Reinforcement Learning (RL) and ML policy optimization.

---

## 6. API Endpoint

```http
GET /recovery-cases/{case_id}/recommendation
```

Response:
```json
{
  "case_id": "fc138641-274e-45f0-a993-96266ac941d1",
  "action_id": "be008274-6362-4d0a-9d27-0638b07cd377",
  "recommended_action": "SEND_WHATSAPP_REMINDER",
  "channel": "WHATSAPP",
  "status": "APPROVED",
  "confidence": 0.8,
  "decision_score": 0.8,
  "reason": "Interactive WhatsApp reminder is optimal for quick customer re-engagement.",
  "supporting_factors": [
    "diagnosis_category=INSUFFICIENT_FUNDS",
    "has_phone=True"
  ],
  "alternatives": [
    {
      "action_type": "SEND_PAYMENT_LINK",
      "channel": "EMAIL",
      "score": 0.77,
      "confidence": 0.78,
      "reason": "Payment link allows customer to complete payment from alternate account..."
    },
    {
      "action_type": "RETRY_PAYMENT",
      "channel": "GATEWAY",
      "score": 0.76,
      "confidence": 0.85,
      "reason": "Transient bank or balance failure suitable for automated gateway retry..."
    }
  ],
  "policy": {
    "allowed": true,
    "reason": "Action satisfies all current recovery policies and compliance constraints.",
    "blocking_rule": null,
    "evaluated_at": "2026-08-21T19:31:34.461310+00:00"
  },
  "decision_engine_version": "decision_engine_v1",
  "policy_engine_version": "policy_engine_v1",
  "created_at": "2026-08-21T19:31:34.425099+00:00"
}
```
