"""Conservative, deterministic attribution classifier for revenue recovery interventions."""
from datetime import datetime, timezone
import logging
from typing import Optional

from app.models.execution import RecoveryExecution
from app.outcomes.base import AttributionType, ObservationWindows

logger = logging.getLogger(__name__)


class AttributionClassifier:
    """Determines the causal attribution relationship between a recovery intervention and payment capture."""

    @classmethod
    def classify(
        cls,
        execution: Optional[RecoveryExecution],
        captured_at: datetime,
    ) -> AttributionType:
        """Classify attribution based on execution state, timing, and observation windows.

        Conservative deterministic logic:
        1. If NO successful execution exists ➔ ORGANIC
        2. If execution completed AFTER payment capture (impossible timing) ➔ UNCERTAIN
        3. If payment captured within 24h of execution ➔ DIRECT
        4. If payment captured within the configured observation window (e.g. 72h) ➔ LIKELY
        5. If payment captured after observation window expiration ➔ ORGANIC
        """
        if not execution or execution.status != "SUCCEEDED":
            logger.info("[ATTRIBUTION] No succeeded recovery execution found. Classifying as ORGANIC.")
            return AttributionType.ORGANIC

        exec_time = execution.completed_at or execution.started_at
        if not exec_time:
            return AttributionType.UNCERTAIN

        # Ensure timezone-aware comparison
        if exec_time.tzinfo is None:
            exec_time = exec_time.replace(tzinfo=timezone.utc)
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = (captured_at - exec_time).total_seconds()

        # Allow up to 10 seconds of clock skew / sub-second epoch resolution difference
        if elapsed_seconds < -10.0:
            logger.warning(
                f"[ATTRIBUTION_ANOMALY] Captured time {captured_at} precedes execution time {exec_time} by {abs(elapsed_seconds):.1f}s. "
                "Classifying as UNCERTAIN."
            )
            return AttributionType.UNCERTAIN

        if elapsed_seconds < 0:
            elapsed_seconds = 0.0

        window = ObservationWindows.get_window_seconds(execution.action_type)

        if elapsed_seconds <= 24 * 3600:
            logger.info(f"[ATTRIBUTION] Payment captured {elapsed_seconds:.1f}s after execution. Classifying as DIRECT.")
            return AttributionType.DIRECT

        if elapsed_seconds <= window:
            logger.info(f"[ATTRIBUTION] Payment captured {elapsed_seconds:.1f}s after execution (within {window}s window). Classifying as LIKELY.")
            return AttributionType.LIKELY

        logger.info(f"[ATTRIBUTION] Payment captured {elapsed_seconds:.1f}s after execution (exceeded {window}s window). Classifying as ORGANIC.")
        return AttributionType.ORGANIC
