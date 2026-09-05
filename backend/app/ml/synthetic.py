"""Synthetic Demo Dataset Generator for Sandbox Testing of the ML Pipeline."""
from datetime import datetime, timedelta, timezone
import random
from typing import Any, Dict, List
import uuid



def generate_synthetic_demo_dataset(
    n_samples: int = 120,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    """Generate a clean synthetic dataset labeled SYNTHETIC_DEMO for pipeline verification.

    Contains realistic features for candidate actions and known recovery outcomes
    with reproducible class distribution (~60% positive, ~40% negative).
    """
    rng = random.Random(seed)
    categories = [
        "INSUFFICIENT_FUNDS",
        "AUTHENTICATION_FAILED",
        "BANK_TECHNICAL_FAILURE",
        "PAYMENT_METHOD_INVALID",
        "USER_FRICTION",
        "FRAUD_OR_SECURITY",
    ]
    actions = [
        "RETRY_PAYMENT",
        "SEND_PAYMENT_LINK",
        "SEND_WHATSAPP_REMINDER",
        "SEND_EMAIL_NOTIFICATION",
        "OFFER_DISCOUNT",
    ]
    methods = ["CARD", "UPI", "NETBANKING", "WALLET"]
    banks = ["HDFC", "ICICI", "SBIN", "UTIB", "KKBK", None]

    base_time = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
    records = []

    for i in range(n_samples):
        cat = rng.choice(categories)
        action = rng.choice(actions)
        method = rng.choice(methods)
        bank = rng.choice(banks)
        amount = round(rng.uniform(200.0, 15000.0), 2)
        risk = round(rng.uniform(10.0, 90.0), 1)
        prev_attempts = rng.randint(0, 3)
        succ_count = rng.randint(0, 15)
        fail_count = rng.randint(0, 5)
        tot = succ_count + fail_count
        succ_rate = round(succ_count / tot if tot > 0 else 0.0, 2)
        diag_conf = round(rng.uniform(0.60, 0.98), 2)
        heuristic_prob = round(rng.uniform(0.30, 0.85), 2)
        decision_score = round(rng.uniform(0.40, 0.90), 2)
        decision_conf = round(rng.uniform(0.60, 0.95), 2)
        age_hours = round(rng.uniform(0.1, 72.0), 1)

        # Realistic ground truth probability logic
        base_recovery_score = 0.50
        if cat == "AUTHENTICATION_FAILED" and action == "SEND_PAYMENT_LINK":
            base_recovery_score += 0.25
        elif cat == "INSUFFICIENT_FUNDS" and action == "SEND_WHATSAPP_REMINDER":
            base_recovery_score += 0.20
        elif cat == "BANK_TECHNICAL_FAILURE" and action == "RETRY_PAYMENT":
            base_recovery_score += 0.30
        elif action == "OFFER_DISCOUNT":
            base_recovery_score += 0.15

        if prev_attempts >= 2:
            base_recovery_score -= 0.20
        if succ_rate > 0.70:
            base_recovery_score += 0.15

        prob = min(0.95, max(0.05, base_recovery_score + rng.uniform(-0.15, 0.15)))
        label = 1 if prob >= 0.50 else 0

        created_at = base_time + timedelta(hours=i * 2)

        features = {
            "amount_at_risk": amount,
            "case_age_at_decision_hours": age_hours,
            "diagnosis_category": cat,
            "diagnosis_confidence": diag_conf,
            "risk_score": risk,
            "heuristic_recovery_probability": heuristic_prob,
            "customer_success_rate": succ_rate,
            "customer_success_count": succ_count,
            "customer_failure_count": fail_count,
            "previous_recovery_attempts": prev_attempts,
            "payment_method": method,
            "bank": bank or "UNKNOWN",
            "error_code": f"ERR_{cat[:4]}",
            "error_source": "ISSUER_BANK" if cat != "USER_FRICTION" else "CUSTOMER",
            "error_step": "PAYMENT_AUTHORIZATION",
            "error_reason": f"simulated_{cat.lower()}",
            "action_type": action,
            "decision_score": decision_score,
            "decision_confidence": decision_conf,
        }

        records.append({
            "id": str(uuid.uuid4()),
            "dataset_type": "SYNTHETIC_DEMO",
            "features": features,
            "label": label,
            "created_at": created_at.isoformat(),
        })

    return records
