"""Expected Recovered Value (ERV) Calculation and Validation Module."""
from typing import Any, Dict


def calculate_expected_recovered_value(
    probability: float,
    amount_at_risk: float,
) -> Dict[str, Any]:
    """Calculate expected monetary recovery value: ERV = probability * amount_at_risk.

    Args:
        probability: Float between 0.0 and 1.0 (predicted recovery probability).
        amount_at_risk: Non-negative monetary amount at risk.

    Returns:
        Dict with probability, amount_at_risk, and expected_recovered_value.

    Raises:
        ValueError: If probability not in [0.0, 1.0] or amount_at_risk < 0.0.
    """
    prob_float = float(probability)
    amount_float = float(amount_at_risk)

    if prob_float < 0.0 or prob_float > 1.0:
        raise ValueError(
            f"Invalid recovery probability: {prob_float}. Must be between 0.0 and 1.0."
        )

    if amount_float < 0.0:
        raise ValueError(
            f"Invalid amount at risk: {amount_float}. Must be non-negative."
        )

    prob_clean = round(prob_float, 4)
    expected_value = round(prob_clean * amount_float, 2)

    return {
        "probability": prob_clean,
        "amount_at_risk": amount_float,
        "expected_recovered_value": expected_value,
    }
