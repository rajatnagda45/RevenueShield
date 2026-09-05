"""Run a batch replay from the API (internal) and fetch the rendered report."""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.simulation.replay import BatchReplay, ChaosConfig, ReplayConfig
from app.simulation.report import render_markdown

router = APIRouter(prefix="/simulation", tags=["Batch Replay Harness"])


class ReplayRequest(BaseModel):
    n_cases: int = Field(100, ge=5, le=5000)
    days: int = Field(7, ge=1, le=60)
    tick_hours: int = Field(12, ge=1, le=48)
    seed: int = 7
    holdout_percent: float = Field(10.0, ge=0, le=90)
    agent_enabled: bool = True
    provider: str = "auto"
    degradation_episode: bool = True
    duplicate_webhook_rate: float = Field(0.10, ge=0, le=1)
    llm_outage: bool = True
    batch_id: Optional[str] = None
    include_markdown: bool = True


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


@router.post("/replay", summary="Replay a seeded batch through the real pipeline and return the scorecard")
def replay(body: ReplayRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    cfg = ReplayConfig(
        n_cases=body.n_cases, days=body.days, tick_hours=body.tick_hours, seed=body.seed, holdout_percent=body.holdout_percent,
        agent_enabled=body.agent_enabled, provider=body.provider, degradation_episode=body.degradation_episode, batch_id=body.batch_id,
        chaos=ChaosConfig(duplicate_webhook_rate=body.duplicate_webhook_rate, llm_outage_hours=(48.0, 60.0) if body.llm_outage else None),
    )
    report = BatchReplay(db, cfg).run()
    db.commit()
    if body.include_markdown:
        report["markdown"] = render_markdown(report)
    return report
