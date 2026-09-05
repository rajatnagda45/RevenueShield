"""Parametric customer behaviour model.

The model answers one question per case per tick: given what happened to this customer since the
last tick, do they pay now? Organic recovery (they were going to pay anyway) is modelled
separately from intervention uplift, which is what makes the holdout comparison meaningful. The
parameters are opinions, not measurements; they are in one place so a merchant can replace them
with their own data.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional

# Daily organic pay probability by diagnosis category (customer resolves it themselves).
ORGANIC_DAILY = {
    "INSUFFICIENT_FUNDS": 0.045,
    "AUTHENTICATION_FAILURE": 0.040,
    "BANK_TECHNICAL_FAILURE": 0.060,
    "BANK_DECLINE": 0.025,
    "PAYMENT_METHOD_FAILURE": 0.012,
    "USER_FRICTION": 0.020,
    "POSSIBLE_FRAUD_OR_SECURITY": 0.003,
    "MERCHANT_CONFIGURATION": 0.010,
    "RECEIVABLE_OVERDUE": 0.030,
    "MANDATE_REVOKED_OR_EXPIRED": 0.008,
    "SYSTEMIC_ISSUER_DEGRADATION": 0.080,   # once the issuer is back, retries clear on their own
    "UNKNOWN": 0.025,
}

# Probability of paying within ~2 days after a touch, by category (added on top of organic).
UPLIFT_BY_CATEGORY = {
    "INSUFFICIENT_FUNDS": 0.22,
    "AUTHENTICATION_FAILURE": 0.32,
    "BANK_TECHNICAL_FAILURE": 0.18,
    "BANK_DECLINE": 0.14,
    "PAYMENT_METHOD_FAILURE": 0.24,
    "USER_FRICTION": 0.28,
    "POSSIBLE_FRAUD_OR_SECURITY": 0.02,
    "MERCHANT_CONFIGURATION": 0.05,
    "RECEIVABLE_OVERDUE": 0.18,
    "MANDATE_REVOKED_OR_EXPIRED": 0.26,
    "SYSTEMIC_ISSUER_DEGRADATION": 0.02,
    "UNKNOWN": 0.15,
}

CHANNEL_MULTIPLIER = {"WHATSAPP": 1.00, "EMAIL": 0.55, "VOICE": 1.35, "PAYMENT_LINK": 0.90, "SMS": 0.80, "GATEWAY": 0.70, "NONE": 0.0}
SEGMENT_MULTIPLIER = {"STANDARD": 1.0, "SMB": 0.95, "MID_MARKET": 1.05, "ENTERPRISE": 1.10}
LANGUAGE_MATCH_BONUS = 0.05  # message in the customer's preferred language


@dataclass
class TouchEvent:
    channel: str
    personalised: bool = False
    language: Optional[str] = None
    payment_plan: bool = False


class CustomerBehaviourModel:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed * 7919)

    def organic_probability(self, *, category: str, hours: float) -> float:
        daily = ORGANIC_DAILY.get(category, ORGANIC_DAILY["UNKNOWN"])
        return 1.0 - (1.0 - daily) ** (hours / 24.0)

    def uplift_probability(self, *, category: str, segment: str, touches: List[TouchEvent], touches_before: int, preferred_language: str) -> float:
        if not touches:
            return 0.0
        base = UPLIFT_BY_CATEGORY.get(category, UPLIFT_BY_CATEGORY["UNKNOWN"])
        total = 0.0
        for idx, t in enumerate(touches):
            n_prior = touches_before + idx
            fatigue = 0.6 ** max(0, n_prior - 1)          # third touch onwards is much less effective
            mult = CHANNEL_MULTIPLIER.get(t.channel, 0.8) * SEGMENT_MULTIPLIER.get(segment, 1.0) * fatigue
            p = base * mult
            if t.personalised:
                p *= 1.10
            if t.language and t.language == preferred_language:
                p += LANGUAGE_MATCH_BONUS
            if t.payment_plan:
                p += 0.10                                   # instalments help cash-constrained customers
            total = 1.0 - (1.0 - total) * (1.0 - min(max(p, 0.0), 0.95))
        return total

    def pays(self, *, category: str, segment: str, hours: float, touches: List[TouchEvent], touches_before: int, preferred_language: str, systemic_active: bool) -> bool:
        if systemic_active:
            # Nobody can pay while the issuer is down; organic clears once it recovers.
            return False
        p = 1.0 - (1.0 - self.organic_probability(category=category, hours=hours)) * (1.0 - self.uplift_probability(category=category, segment=segment, touches=touches, touches_before=touches_before, preferred_language=preferred_language))
        return self.rng.random() < p

    def opts_out(self, *, touches_total: int) -> bool:
        """Probability of a STOP after a touch rises with fatigue."""
        if touches_total <= 0:
            return False
        p = 0.012 * touches_total ** 1.5
        return self.rng.random() < min(p, 0.25)

    def keeps_promise(self) -> bool:
        return self.rng.random() < 0.70

    def approves(self) -> bool:
        """Simulated operator on the approval queue."""
        return self.rng.random() < 0.80
