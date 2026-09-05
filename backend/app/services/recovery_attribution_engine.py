"""Deterministic Recovery Attribution Engine implementing window-based last-touch attribution."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import List, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recovery_attribution import RecoveryAttribution
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep

logger = logging.getLogger(__name__)


class RecoveryAttributionEngine:
    """Attributes monetary recovery credit to specific recovery interventions."""

    @classmethod
    def attribute_recovery(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        amount_recovered: Decimal,
        captured_at: Optional[datetime] = None,
        attribution_window_hours: Optional[int] = None,
    ) -> Optional[RecoveryAttribution]:
        """Determine which recovery plan step gets primary or uncertain attribution for the captured payment."""
        settle_time = captured_at or datetime.now(timezone.utc)
        if settle_time.tzinfo is None:
            settle_time = settle_time.replace(tzinfo=timezone.utc)

        window_hours = attribution_window_hours or settings.ATTRIBUTION_WINDOW_HOURS
        window_start = settle_time - timedelta(hours=window_hours)

        # 1. Fetch Recovery Case & Plan Steps
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            logger.warning(f"[ATTRIBUTION_SKIPPED] Case {recovery_case_id} not found.")
            return None

        plan = case.recovery_plan or db.scalar(
            select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == recovery_case_id)
        )

        steps: List[RecoveryPlanStep] = []
        if plan and plan.steps:
            # Sort chronologically by step number descending (most recent first)
            steps = sorted(
                [s for s in plan.steps if s.status in ["COMPLETED", "RUNNING", "SCHEDULED"]],
                key=lambda s: s.step_number,
                reverse=True,
            )

        if not steps:
            # Organic recovery with zero recorded outreach
            attr = RecoveryAttribution(
                recovery_case_id=case.id,
                recovery_step_id=None,
                amount_recovered=amount_recovered,
                attribution_type="UNCERTAIN",
                attribution_weight=0.0,
            )
            db.add(attr)
            db.commit()
            db.refresh(attr)
            logger.info(f"[ATTRIBUTION_ORGANIC] Case={case.id} labeled UNCERTAIN/ORGANIC (no outreach steps).")
            return attr

        # 2. Check for last eligible step executed within the attribution window
        last_step = steps[0]
        step_time = last_step.executed_at or last_step.completed_at or last_step.created_at
        if step_time and step_time.tzinfo is None:
            step_time = step_time.replace(tzinfo=timezone.utc)

        is_within_window = bool(step_time and step_time >= window_start)

        if is_within_window:
            attr_type = "PRIMARY"
            weight = 1.0
            logger.info(
                f"[ATTRIBUTION_PRIMARY] Attributed Rs. {amount_recovered} to Step {last_step.step_number} ({last_step.action_type}) within {window_hours}h window."
            )
        else:
            attr_type = "UNCERTAIN"
            weight = 0.5
            logger.info(
                f"[ATTRIBUTION_UNCERTAIN] Step {last_step.step_number} occurred outside {window_hours}h window. Tagged UNCERTAIN."
            )

        # 3. Persist Attribution Record
        attr = RecoveryAttribution(
            recovery_case_id=case.id,
            recovery_step_id=last_step.id,
            amount_recovered=amount_recovered,
            attribution_type=attr_type,
            attribution_weight=weight,
        )
        db.add(attr)
        db.commit()
        db.refresh(attr)
        return attr
