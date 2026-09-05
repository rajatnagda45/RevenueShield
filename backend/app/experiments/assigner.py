"""Deterministic experiment arm assignment.

Why a control group: the Track 3 bar is "measured money recovered across a batch". Without a
holdout we can only report "captured after we intervened", which conflates organic payments with
recovery. Every case is hashed into a bucket; buckets below the holdout threshold are observed but
never contacted. The assignment is a pure function of (experiment_key, salt, case_id), so it is
reproducible from the audit trail alone.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.experiment_assignment import ExperimentAssignment
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)

HOLDOUT_BLOCKING_RULE = "HOLDOUT_ARM_OBSERVE_ONLY"
BUCKET_SPACE = 10_000  # basis points


class ExperimentArm(str, Enum):
    TREATMENT = "TREATMENT"
    HOLDOUT = "HOLDOUT"


class ExperimentAssigner:
    """Assigns and reads experiment arms for recovery cases."""

    @staticmethod
    def _holdout_bps_for_surface(surface: Optional[str]) -> int:
        """Resolve holdout basis points, honouring per-surface overrides."""
        pct = float(settings.HOLDOUT_PERCENT or 0.0)
        raw = (settings.HOLDOUT_PERCENT_BY_SURFACE or "").strip()
        if raw and surface:
            try:
                overrides = json.loads(raw)
                if surface in overrides:
                    pct = float(overrides[surface])
            except (ValueError, TypeError):
                logger.warning("[EXPERIMENT_CONFIG] HOLDOUT_PERCENT_BY_SURFACE is not valid JSON; ignoring.")
        pct = max(0.0, min(100.0, pct))
        return int(round(pct * 100))

    @classmethod
    def compute_bucket(cls, case_id: uuid.UUID | str, experiment_key: str, salt: str) -> int:
        """Stable bucket in [0, 10000) derived from sha256(experiment_key:salt:case_id)."""
        digest = hashlib.sha256(f"{experiment_key}:{salt}:{case_id}".encode("utf-8")).hexdigest()
        return int(digest[:12], 16) % BUCKET_SPACE

    @classmethod
    def compute_arm(
        cls,
        case_id: uuid.UUID | str,
        surface: Optional[str] = None,
        experiment_key: Optional[str] = None,
        salt: Optional[str] = None,
    ) -> Tuple[ExperimentArm, int, int]:
        """Return (arm, bucket, holdout_bps) without touching the database."""
        key = experiment_key or settings.EXPERIMENT_KEY
        salt_val = salt or settings.EXPERIMENT_SALT
        bucket = cls.compute_bucket(case_id, key, salt_val)
        holdout_bps = cls._holdout_bps_for_surface(surface)
        if not settings.EXPERIMENTS_ENABLED:
            holdout_bps = 0
        arm = ExperimentArm.HOLDOUT if bucket < holdout_bps else ExperimentArm.TREATMENT
        return arm, bucket, holdout_bps

    @classmethod
    def assign(
        cls,
        db: Session,
        case: RecoveryCase,
        surface: Optional[str] = None,
        force_arm: Optional[ExperimentArm] = None,
    ) -> ExperimentAssignment:
        """Idempotently assign a case to an arm, denormalise it on the case, and audit it.

        `force_arm` exists for tests and operator overrides; it is recorded in metadata so the
        scorecard can exclude forced assignments from the randomised comparison.
        """
        key = settings.EXPERIMENT_KEY
        existing = db.scalar(
            select(ExperimentAssignment).where(
                ExperimentAssignment.recovery_case_id == case.id,
                ExperimentAssignment.experiment_key == key,
            )
        )
        if existing:
            if case.experiment_arm != existing.arm:
                case.experiment_arm = existing.arm
            return existing

        leak_surface = surface or case.leak_surface or case.case_type or "PAYMENT_FAILURE"
        arm, bucket, holdout_bps = cls.compute_arm(case.id, surface=leak_surface)
        forced = False
        if force_arm is not None:
            arm = force_arm
            forced = True

        assignment = ExperimentAssignment(
            recovery_case_id=case.id,
            experiment_key=key,
            arm=arm.value,
            bucket=bucket,
            holdout_bps=holdout_bps,
            salt_version=settings.EXPERIMENT_SALT,
            leak_surface=leak_surface,
            assignment_metadata={"forced": forced, "experiments_enabled": bool(settings.EXPERIMENTS_ENABLED)},
        )
        db.add(assignment)
        case.experiment_arm = arm.value
        if not case.leak_surface:
            case.leak_surface = leak_surface
        db.flush()

        db.add(
            AuditLog(
                recovery_case_id=case.id,
                actor_type="SYSTEM",
                actor_id="experiment_assigner_v1",
                action="EXPERIMENT_ARM_ASSIGNED",
                entity_type="ExperimentAssignment",
                entity_id=str(assignment.id),
                audit_metadata={
                    "experiment_key": key,
                    "arm": arm.value,
                    "bucket": bucket,
                    "holdout_bps": holdout_bps,
                    "leak_surface": leak_surface,
                    "forced": forced,
                },
            )
        )
        db.flush()
        logger.info(f"[EXPERIMENT_ASSIGNED] Case={case.id} Arm={arm.value} Bucket={bucket} HoldoutBps={holdout_bps}")
        return assignment

    @classmethod
    def is_holdout(cls, case: Optional[RecoveryCase]) -> bool:
        """True when the case must be observed only (no outreach, no retries)."""
        return bool(case is not None and case.experiment_arm == ExperimentArm.HOLDOUT.value)

    @classmethod
    def holdout_block(cls, case: Optional[RecoveryCase]) -> Optional[Dict[str, Any]]:
        """Standard blocking payload for services that must refuse to touch holdout cases."""
        if not cls.is_holdout(case):
            return None
        return {
            "allowed": False,
            "blocking_rule": HOLDOUT_BLOCKING_RULE,
            "reason": (
                "Case is in the experiment HOLDOUT arm: it is observed for measurement and receives "
                "no outreach, retries, or payment links."
            ),
        }
