"""Protocols, Enums, and Dataclasses for the Bounded Recovery Execution Engine."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Protocol


class ExecutionStatus(str, Enum):
    """Lifecycle status of a recovery execution attempt."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


@dataclass
class ExecutionRequest:
    """Structured request payload dispatched to a RecoveryExecutor."""

    execution_id: str
    case_id: str
    action_id: str
    action_type: str
    customer_id: str
    amount: Decimal
    currency: str
    idempotency_key: str
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    engine_version: str = "decision_engine_v1"
    policy_version: str = "policy_engine_v1"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    """Normalized internal result returned by any RecoveryExecutor."""

    status: ExecutionStatus
    provider: str
    provider_reference: Optional[str] = None
    provider_url: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    execution_metadata: Dict[str, Any] = field(default_factory=dict)


class RecoveryExecutor(Protocol):
    """Protocol for recovery action executors."""

    provider_name: str

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute the recovery action and return a normalized ExecutionResult."""
        ...
